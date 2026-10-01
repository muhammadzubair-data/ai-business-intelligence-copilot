"""Compile a validated metric request into SQL.

The LLM never writes SQL for catalog metrics. It chooses metric, dimensions,
filters and time range; this module writes the SQL. That is what keeps
"revenue" meaning the same thing in every answer.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, timedelta

from ..plan import TimeRange
from .catalog import Catalog, get_catalog

GRAINS = {"day", "week", "month", "quarter", "year"}


class CompileError(ValueError):
    pass


def q(value: str) -> str:
    """Quote a string literal safely."""
    return "'" + str(value).replace("'", "''") + "'"


@dataclass
class MetricRequest:
    metrics: list[str]
    time_range: TimeRange
    dimensions: list[str] = field(default_factory=list)
    filters: dict[str, list[str]] = field(default_factory=dict)
    grain: str | None = None
    order: str = "desc"
    limit: int | None = None


class Compiler:
    def __init__(self, catalog: Catalog | None = None):
        self.cat = catalog or get_catalog()

    # ------------------------------------------------------------------ validation
    def validate(self, req: MetricRequest) -> None:
        if not req.metrics:
            raise CompileError("No metric selected.")
        for m in req.metrics:
            if m not in self.cat.metrics:
                raise CompileError(f"Unknown metric '{m}'.")
            allowed = self.cat.allowed_dimensions(m)
            for d in req.dimensions:
                if d not in allowed:
                    raise CompileError(f"Metric '{m}' cannot be broken down by '{d}'. "
                                       f"Allowed: {', '.join(sorted(allowed)) or 'time only'}.")
            for d in req.filters:
                if d not in allowed:
                    raise CompileError(f"Metric '{m}' cannot be filtered by '{d}'.")
        if req.grain and req.grain not in GRAINS:
            raise CompileError(f"Unknown time grain '{req.grain}'.")

    # ------------------------------------------------------------------ public
    def compile(self, req: MetricRequest) -> str:
        self.validate(req)
        dims = list(req.dimensions)
        out_dims = (["period"] if req.grain else []) + dims
        ctes, selects = [], []

        simple_groups: dict[tuple, list[str]] = {}
        derived, custom = [], []
        for m in req.metrics:
            met = self.cat.metrics[m]
            if met.type in ("simple", "ratio"):
                simple_groups.setdefault((met.model, met.filter, met.time_column), []).append(m)
            elif met.type == "derived":
                derived.append(m)
                for c in met.components:
                    cm = self.cat.metrics[c]
                    grp = simple_groups.setdefault((cm.model, cm.filter, cm.time_column), [])
                    if c not in grp:
                        grp.append(c)
            else:
                custom.append(m)

        for i, ((model, flt, tcol), mets) in enumerate(simple_groups.items()):
            ctes.append((f"m{i}", self._simple_cte(model, flt, tcol, mets, req, dims), mets))
        for j, m in enumerate(custom):
            ctes.append((f"c{j}", self._custom_cte(m, req, dims), [m]))

        if len(ctes) == 1 and not derived:
            name, body, mets = ctes[0]
            sql = body
            if req.grain:
                sql += "\nORDER BY period"
            elif mets:
                sql += f"\nORDER BY {mets[0]} {req.order.upper()} NULLS LAST"
            if req.limit:
                sql += f"\nLIMIT {int(req.limit)}"
            return sql

        with_sql = "WITH " + ",\n".join(f"{n} AS (\n{b}\n)" for n, b, _ in ctes)
        first = ctes[0][0]
        from_sql = first
        for n, _, _ in ctes[1:]:
            from_sql += f"\nFULL OUTER JOIN {n} USING ({', '.join(out_dims)})" if out_dims else f"\nCROSS JOIN {n}"
        cols = list(out_dims)
        for m in req.metrics:
            met = self.cat.metrics[m]
            if met.type == "derived":
                expr = met.formula
                for c in sorted(met.components, key=len, reverse=True):
                    expr = _replace_word(expr, c, f"COALESCE({c}, 0)")
                cols.append(f"({expr}) AS {m}")
            else:
                cols.append(m)
        sql = f"{with_sql}\nSELECT {', '.join(cols)}\nFROM {from_sql}"
        if req.grain:
            sql += "\nORDER BY period"
        else:
            sql += f"\nORDER BY {req.metrics[0]} {req.order.upper()} NULLS LAST"
        if req.limit:
            sql += f"\nLIMIT {int(req.limit)}"
        return sql

    # ------------------------------------------------------------------ pieces
    def _where(self, model: str, flt: str | None, tcol: str, req: MetricRequest) -> str:
        cols = self.cat.models[model].dimensions
        parts = []
        if flt:
            parts.append(f"({flt})")
        parts.append(f"{tcol} BETWEEN DATE {q(req.time_range.start)} AND DATE {q(req.time_range.end)}")
        for d, vals in req.filters.items():
            parts.append(self._filter_sql(cols[d], vals))
        return " AND ".join(parts)

    @staticmethod
    def _filter_sql(col: str, vals: list[str]) -> str:
        if col == "is_key_account":
            v = str(vals[0]).lower() in ("true", "yes", "1", "key account", "key accounts")
            return f"{col} = {'TRUE' if v else 'FALSE'}"
        return f"{col} IN ({', '.join(q(v) for v in vals)})"

    def _simple_cte(self, model, flt, tcol, mets, req, dims) -> str:
        mod = self.cat.models[model]
        sel = []
        if req.grain:
            sel.append(f"date_trunc('{req.grain}', {tcol})::DATE AS period")
        for d in dims:
            sel.append(f"{mod.dimensions[d]} AS {d}")
        for m in mets:
            sel.append(f"{self.expression(m)} AS {m}")
        return (f"SELECT {', '.join(sel)}\nFROM {mod.view}\nWHERE {self._where(model, flt, tcol, req)}"
                + ("\nGROUP BY ALL" if (req.grain or dims) else ""))

    def expression(self, metric: str) -> str:
        met = self.cat.metrics[metric]
        if met.type == "simple":
            return met.expression
        if met.type == "ratio":
            num, den = self.cat.metrics[met.numerator], self.cat.metrics[met.denominator]
            return f"({num.expression})::DOUBLE / NULLIF({den.expression}, 0)"
        raise CompileError(f"{metric} has no single aggregate expression")

    def _custom_cte(self, metric, req, dims) -> str:
        met = self.cat.metrics[metric]
        cols = self.cat.models[met.model].dimensions
        where = "".join(" AND " + self._filter_sql(cols[d], v) for d, v in req.filters.items())
        periods = self.split_periods(req.time_range, req.grain) if req.grain else [(req.time_range.start, req.time_range.end)]
        parts = []
        for s, e in periods:
            body = met.template.format(
                start=s.isoformat(), end=e.isoformat(), where=where,
                dims_select="".join(f"{d}, " for d in dims),
                dims_raw="".join(f"{cols[d]} AS {d}, " for d in dims),
                dims_any="".join(f", ANY_VALUE({cols[d]}) AS {d}" for d in dims),
            ).strip()
            if req.grain:
                body = f"SELECT DATE {q(s)} AS period, * FROM (\n{body}\n)"
            parts.append(body)
        return "\nUNION ALL\n".join(parts)

    @staticmethod
    def split_periods(tr: TimeRange, grain: str) -> list[tuple[date, date]]:
        out, cur = [], _trunc(tr.start, grain)
        while cur <= tr.end:
            nxt = _add(cur, grain)
            out.append((max(cur, tr.start), min(nxt - timedelta(days=1), tr.end)))
            cur = nxt
        return out


def _replace_word(text: str, word: str, repl: str) -> str:
    import re
    return re.sub(rf"\b{re.escape(word)}\b", repl, text)


def _trunc(d: date, grain: str) -> date:
    if grain == "day":
        return d
    if grain == "week":
        return d - timedelta(days=d.weekday())
    if grain == "month":
        return d.replace(day=1)
    if grain == "quarter":
        return date(d.year, 3 * ((d.month - 1) // 3) + 1, 1)
    return date(d.year, 1, 1)


def _add(d: date, grain: str) -> date:
    if grain == "day":
        return d + timedelta(days=1)
    if grain == "week":
        return d + timedelta(days=7)
    months = {"month": 1, "quarter": 3, "year": 12}[grain]
    m = d.month - 1 + months
    return date(d.year + m // 12, m % 12 + 1, 1)
