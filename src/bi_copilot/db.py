"""Read-only access to the warehouse.

Every query runs on a connection opened with read_only=True, under a timeout
(DuckDB's interrupt()), and is recorded as a SQLRecord for the evidence panel.
"""
from __future__ import annotations

import threading
import time
from functools import lru_cache
from pathlib import Path

import duckdb
import pandas as pd

from .config import settings
from .plan import SQLRecord
from .sql_guard import SQLGuard, referenced_tables


class QueryTimeout(RuntimeError):
    pass


class Database:
    def __init__(self, path: Path | None = None, timeout_s: float | None = None, max_rows: int | None = None):
        self.path = Path(path or settings.db_path)
        if not self.path.exists():
            raise FileNotFoundError(f"Warehouse not found at {self.path}. Run: python -m bi_copilot.cli generate")
        self.con = duckdb.connect(str(self.path), read_only=True)
        self.con.execute("SET enable_progress_bar = false")
        self.timeout_s = timeout_s or settings.query_timeout_s
        self.max_rows = max_rows or settings.max_rows
        self._lock = threading.Lock()
        self.schema = self._load_schema()
        self.guard = SQLGuard(set(self.schema), self.schema, self.max_rows)

    def _load_schema(self) -> dict[str, dict[str, str]]:
        rows = self.con.execute("""SELECT table_name, column_name, data_type FROM information_schema.columns
                                   WHERE table_schema = 'main' ORDER BY table_name, ordinal_position""").fetchall()
        schema: dict[str, dict[str, str]] = {}
        for t, c, dt in rows:
            schema.setdefault(t, {})[c] = dt
        return schema

    # ------------------------------------------------------------------ execution
    def run(self, sql: str, purpose: str = "query", guarded: bool = False) -> tuple[pd.DataFrame, SQLRecord]:
        """Execute SQL. If guarded=True the SQL is validated first (use for any LLM-written SQL).
        Catalog-compiled SQL also passes through the guard's table allow-list via referenced_tables."""
        tables = referenced_tables(sql)
        exec_sql = sql
        if guarded:
            res = self.guard.check(sql)
            exec_sql, tables = res.sql, res.tables
        t0 = time.perf_counter()
        result: dict = {}

        def _target():
            try:
                with self._lock:
                    result["df"] = self.con.execute(exec_sql).df()
            except Exception as e:  # noqa: BLE001
                result["err"] = e

        th = threading.Thread(target=_target, daemon=True)
        th.start()
        th.join(self.timeout_s)
        if th.is_alive():
            self.con.interrupt()
            th.join(2)
            raise QueryTimeout(f"Query exceeded {self.timeout_s:.0f}s and was cancelled.")
        if "err" in result:
            raise result["err"]
        df = result["df"]
        if len(df) > self.max_rows:
            df = df.head(self.max_rows)
        rec = SQLRecord(purpose=purpose, sql=sql.strip(), rows=len(df), tables=tables,
                        ms=round((time.perf_counter() - t0) * 1000, 1))
        return df, rec

    def scalar(self, sql: str):
        with self._lock:
            return self.con.execute(sql).fetchone()[0]

    # ------------------------------------------------------------------ metadata
    @lru_cache(maxsize=1)
    def freshness(self) -> pd.DataFrame:
        with self._lock:
            return self.con.execute("SELECT * FROM meta_table_freshness ORDER BY table_name").df()

    def data_as_of(self) -> str:
        f = self.freshness()
        mx = pd.to_datetime(f[f.table_name.isin(["fact_orders", "fact_order_lines"])].max_event_date).max()
        loaded = pd.to_datetime(f.last_loaded_at).max()
        return f"data through {mx:%Y-%m-%d} (loaded {loaded:%Y-%m-%d %H:%M})"

    @lru_cache(maxsize=64)
    def distinct_values(self, view: str, column: str, limit: int = 2000) -> tuple[str, ...]:
        with self._lock:
            rows = self.con.execute(f"SELECT DISTINCT {column} FROM {view} WHERE {column} IS NOT NULL LIMIT {limit}").fetchall()
        return tuple(str(r[0]) for r in rows)

    def schema_text(self, tables: list[str] | None = None) -> str:
        out = []
        for t, cols in self.schema.items():
            if tables and t not in tables:
                continue
            out.append(f"{t}(" + ", ".join(f"{c} {d}" for c, d in cols.items()) + ")")
        return "\n".join(out)


@lru_cache(maxsize=4)
def get_db(path: str | None = None) -> Database:
    return Database(Path(path) if path else None)
