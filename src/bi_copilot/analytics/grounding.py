"""Formatting and the numeric grounding check.

The grounding check is the hallucination guard for narratives: every number
in the text must match a computed fact (allowing for rounding and unit
formatting such as $1.2M, 8.4% or 3.1 pp). Narratives that fail are replaced
by the deterministic template.
"""
from __future__ import annotations

import math
import re
from typing import Iterable

import numpy as np


def fmt_value(v: float | None, fmt: str) -> str:
    if v is None or (isinstance(v, float) and (math.isnan(v) or math.isinf(v))):
        return "n/a"
    if fmt == "currency":
        return fmt_money(v)
    if fmt == "percent":
        return f"{v * 100:.1f}%"
    if fmt == "decimal":
        return f"{v:,.2f}"
    if abs(v) >= 100_000:
        return f"{v / 1e6:,.2f}M" if abs(v) >= 1e6 else f"{v / 1e3:,.1f}K"
    return f"{v:,.0f}" if abs(v - round(v)) < 1e-9 or abs(v) >= 100 else f"{v:,.2f}"


def fmt_money(v: float) -> str:
    sign = "-" if v < 0 else ""
    a = abs(v)
    if a >= 1e9:
        return f"{sign}${a / 1e9:.2f}B"
    if a >= 1e6:
        return f"{sign}${a / 1e6:.2f}M"
    if a >= 1e4:
        return f"{sign}${a / 1e3:.0f}K"
    if a >= 100:
        return f"{sign}${a:,.0f}"
    return f"{sign}${a:,.2f}"


def fmt_delta(v: float, fmt: str) -> str:
    if fmt == "percent":
        return f"{v * 100:+.1f} pp"
    if fmt == "currency":
        s = fmt_money(abs(v))
        return ("+" if v >= 0 else "-") + s
    if fmt == "decimal":
        return f"{v:+.2f}"
    return ("+" if v >= 0 else "-") + fmt_value(abs(v), fmt)


def pct_change(cur: float, prev: float) -> float | None:
    if prev is None or cur is None or prev == 0 or (isinstance(prev, float) and math.isnan(prev)):
        return None
    return (cur / prev - 1) * 100


# ----------------------------------------------------------------------
NUM_RE = re.compile(r"(?<![\w.])([-+−]?)\$?\s?(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)\s?([kmb](?![a-z])|pp|%|percent|bn)?",
                    re.IGNORECASE)
MONTH_WORDS = r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec|q[1-4]|h[12]|fy|week|day|days|weeks|months|top|step)"


def extract_numbers(text: str) -> list[tuple[str, float]]:
    out = []
    for m in NUM_RE.finditer(text):
        sign, num, unit = m.group(1), m.group(2), (m.group(3) or "").lower()
        raw = m.group(0).strip()
        val = float(num.replace(",", ""))
        if unit == "k":
            val *= 1e3
        elif unit in ("m",):
            val *= 1e6
        elif unit in ("b", "bn"):
            val *= 1e9
        if sign in ("-", "−"):
            val = -val
        # ignore years, dates and small ordinals
        if not unit and 2000 <= abs(val) <= 2100 and "." not in num and "," not in num:
            continue
        before = text[max(0, m.start() - 12):m.start()].lower()
        after = text[m.end():m.end() + 12].lower()
        if re.search(MONTH_WORDS + r"\s*$", before) and not unit and abs(val) <= 53:
            continue
        if not unit and abs(val) <= 31 and re.match(r"\s*(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)", after):
            continue
        if not unit and abs(val) <= 10 and "." not in num:
            continue  # small counts ("3 accounts", "2 steps") are not checked
        if not unit and "." not in num and "," not in num and re.search(r"[A-Za-z&]\s$", text[max(0, m.start() - 2):m.start()]) \
                and (num.startswith("0") or len(num) >= 4) and re.search(r"[A-Z][A-Za-z&]+\s$", text[max(0, m.start() - 30):m.start()]):
            continue  # identifiers inside names ("United Corporation 06970")
        if not unit and re.match(r"[\s-]*(day|days|week|weeks|month|months|year|years)\b", after):
            continue  # window lengths and horizons ("90 days", "last 12 months")
        if unit == "%" and re.match(r"\s*(range|interval|prediction)", after):
            continue  # interval level ("80% range")
        out.append((raw, val))
    return out


def _variants(fact: float) -> Iterable[float]:
    yield fact
    yield abs(fact)
    yield fact * 100          # fraction -> percent
    yield abs(fact * 100)


def is_grounded(value: float, facts: Iterable[float], rel_tol: float = 0.02) -> bool:
    for f in facts:
        if f is None or (isinstance(f, float) and (math.isnan(f) or math.isinf(f))):
            continue
        for v in _variants(float(f)):
            if v == 0:
                if abs(value) < 0.05:
                    return True
                continue
            if abs(value - v) <= max(rel_tol * abs(v), 0.051 if abs(v) < 100 else 0):
                return True
            # rounding like $1.2M for 1,234,567 or 8% for 8.4%
            mag = 10 ** max(0, int(math.log10(abs(v))) - 1)
            if abs(value - v) <= 0.5 * mag and abs(v) >= 1000:
                return True
    return False


def check_grounding(text: str, facts: dict[str, float]) -> list[str]:
    """Return the numbers in `text` that do not match any computed fact."""
    vals = [v for v in facts.values() if v is not None and not (isinstance(v, float) and np.isnan(v))]
    return [raw for raw, val in extract_numbers(text) if not is_grounded(val, vals)]
