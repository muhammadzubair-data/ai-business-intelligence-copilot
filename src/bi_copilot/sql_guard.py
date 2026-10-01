"""SQL guardrails.

Defence in depth for any SQL that reaches the database:
  1. parse with sqlglot (DuckDB dialect); unparseable SQL is rejected
  2. exactly one statement, and it must be a read-only query
  3. no DDL/DML/commands anywhere in the tree (INSERT, DROP, COPY, ATTACH, PRAGMA ...)
  4. no table functions that read files or the network (read_csv, glob, httpfs ...)
  5. every table referenced must be on the allow-list
  6. every column must exist in the referenced tables (schema validation)
  7. a row limit is enforced by wrapping the query
The connection itself is opened read-only (see db.py), so even a parser miss
cannot modify data.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError
from sqlglot.optimizer.qualify import qualify

FORBIDDEN_NODES = tuple(getattr(exp, n) for n in [
    "Insert", "Update", "Delete", "Drop", "Create", "Alter", "Command", "Merge", "Copy",
    "Pragma", "Set", "Use", "Attach", "Detach", "Grant", "Revoke", "TruncateTable", "Transaction",
    "Commit", "Rollback", "Install", "Load", "Export",
] if hasattr(exp, n))

FORBIDDEN_FUNCTIONS = {
    "read_csv", "read_csv_auto", "read_parquet", "read_json", "read_json_auto", "read_ndjson", "read_text",
    "read_blob", "glob", "parquet_scan", "csv_scan", "sniff_csv", "json_scan", "delta_scan", "iceberg_scan",
    "sqlite_scan", "postgres_scan", "mysql_scan", "httpfs", "query", "query_table", "getenv", "system",
}


class GuardError(ValueError):
    pass


@dataclass
class GuardResult:
    sql: str
    tables: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


class SQLGuard:
    def __init__(self, allowed_tables: set[str], schema: dict[str, dict[str, str]] | None = None, max_rows: int = 5000):
        self.allowed = {t.lower() for t in allowed_tables}
        self.schema = schema
        self.max_rows = max_rows

    def check(self, sql: str) -> GuardResult:
        sql = sql.strip().rstrip(";").strip()
        if not sql:
            raise GuardError("Empty query.")
        try:
            statements = sqlglot.parse(sql, read="duckdb")
        except ParseError as e:
            raise GuardError(f"SQL could not be parsed: {str(e).splitlines()[0]}") from e
        statements = [s for s in statements if s is not None]
        if len(statements) != 1:
            raise GuardError("Only a single statement is allowed.")
        tree = statements[0]
        if not isinstance(tree, (exp.Select, exp.Union, exp.Intersect, exp.Except, exp.Subquery)):
            raise GuardError(f"Only SELECT queries are allowed (got {tree.key.upper()}).")
        for node in tree.walk():
            if isinstance(node, FORBIDDEN_NODES):
                raise GuardError(f"Forbidden operation: {node.key.upper()}.")
            if isinstance(node, (exp.Anonymous, exp.Func)):
                fname = (node.name if isinstance(node, exp.Anonymous) else node.sql_name()).lower()
                if fname in FORBIDDEN_FUNCTIONS:
                    raise GuardError(f"Function '{fname}' is not allowed.")
            if isinstance(node, exp.Into):
                raise GuardError("SELECT INTO is not allowed.")

        cte_names = {c.alias_or_name.lower() for c in tree.find_all(exp.CTE)}
        tables = []
        for t in tree.find_all(exp.Table):
            name = t.name.lower()
            if not name or name in cte_names:
                continue
            if t.args.get("db") or t.args.get("catalog"):
                raise GuardError(f"Schema-qualified table '{t.sql()}' is not allowed.")
            if name not in self.allowed:
                raise GuardError(f"Table '{name}' is not on the allow-list.")
            tables.append(name)
            if isinstance(t.this, exp.Func) or isinstance(t.this, exp.Anonymous):
                raise GuardError("Table functions are not allowed.")

        warnings = []
        if self.schema:
            try:
                qualify(tree.copy(), schema=self.schema, dialect="duckdb", validate_qualify_columns=True,
                        identify=False)
            except Exception as e:  # unknown column / ambiguous reference
                msg = str(e).splitlines()[0]
                if "Unknown" in msg or "Column" in msg or "could not be resolved" in msg:
                    raise GuardError(f"Schema validation failed: {msg}") from e
                warnings.append(f"Schema validation skipped: {msg}")

        limited = f"SELECT * FROM (\n{tree.sql(dialect='duckdb')}\n) AS guarded_query LIMIT {self.max_rows}"
        return GuardResult(sql=limited, tables=sorted(set(tables)), warnings=warnings)


def referenced_tables(sql: str) -> list[str]:
    try:
        tree = sqlglot.parse_one(sql, read="duckdb")
    except Exception:
        return []
    ctes = {c.alias_or_name.lower() for c in tree.find_all(exp.CTE)}
    return sorted({t.name.lower() for t in tree.find_all(exp.Table) if t.name and t.name.lower() not in ctes})
