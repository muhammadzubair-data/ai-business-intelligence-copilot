"""Parse time expressions in business questions.

Relative expressions resolve against the latest complete period in the data
(the data ends 2025-12-31): "last month" = December 2025, "last quarter" =
Q4 2025, "last year" = 2025, "this week" = the last complete Monday-Sunday
week. Every relative resolution is recorded as an assumption so it shows up
in the answer's evidence.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Optional

from ..config import AS_OF_DATE, DATA_START
from ..plan import TimeRange

MONTHS = {m: i for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august", "september", "october",
     "november", "december"], 1)}
MONTHS.update({k[:3]: v for k, v in list(MONTHS.items())})
MONTHS["sept"] = 9
MONTH_RE = r"(jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|may|june?|july?|aug(?:ust)?|sep(?:t(?:ember)?)?|oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)"
ORD = {"first": 1, "1st": 1, "second": 2, "2nd": 2, "third": 3, "3rd": 3, "fourth": 4, "4th": 4, "last": 4}
NUM = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
       "ten": 10, "eleven": 11, "twelve": 12, "eighteen": 18, "twenty-four": 24, "twenty four": 24}


def month_end(y: int, m: int) -> date:
    return (date(y + (m == 12), m % 12 + 1, 1) - timedelta(days=1))


def quarter_range(y: int, qn: int) -> TimeRange:
    s = date(y, 3 * (qn - 1) + 1, 1)
    return TimeRange(start=s, end=month_end(y, 3 * qn), label=f"Q{qn} {y}")


def year_range(y: int) -> TimeRange:
    return TimeRange(start=date(y, 1, 1), end=date(y, 12, 31), label=str(y))


def month_range(y: int, m: int) -> TimeRange:
    return TimeRange(start=date(y, m, 1), end=month_end(y, m), label=date(y, m, 1).strftime("%B %Y"))


def fiscal_year_range(fy: int) -> TimeRange:
    return TimeRange(start=date(fy - 1, 2, 1), end=date(fy, 1, 31), label=f"FY{fy}")


def last_complete_week(ref: date = AS_OF_DATE) -> TimeRange:
    end = ref - timedelta(days=(ref.weekday() + 1) % 7) if ref.weekday() != 6 else ref
    start = end - timedelta(days=6)
    return TimeRange(start=start, end=end, label=f"week of {start:%d %b %Y}")


def shift_years(tr: TimeRange, years: int = -1) -> TimeRange:
    def sh(d: date) -> date:
        try:
            return d.replace(year=d.year + years)
        except ValueError:
            return d.replace(year=d.year + years, day=28)
    label = re.sub(r"\d{4}", lambda m: str(int(m.group()) + years), tr.label)
    if label == tr.label:
        label = f"{tr.label} (prior year)"
    return TimeRange(start=sh(tr.start), end=sh(tr.end), label=label)


def previous_period(tr: TimeRange) -> TimeRange:
    """Immediately preceding period of the same kind (month, quarter, year, week or equal length)."""
    s, e = tr.start, tr.end
    if s.day == 1 and e == month_end(e.year, e.month):
        months = (e.year - s.year) * 12 + e.month - s.month + 1
        ps_m = s.month - months
        ps = date(s.year + (ps_m - 1) // 12, (ps_m - 1) % 12 + 1, 1)
        pe = s - timedelta(days=1)
        if months == 1:
            return month_range(ps.year, ps.month)
        if months == 3 and ps.month in (1, 4, 7, 10):
            return quarter_range(ps.year, (ps.month - 1) // 3 + 1)
        if months == 12 and ps.month == 1:
            return year_range(ps.year)
        return TimeRange(start=ps, end=pe, label=f"{ps:%b %Y} – {pe:%b %Y}")
    n = (e - s).days + 1
    pe = s - timedelta(days=1)
    ps = pe - timedelta(days=n - 1)
    lab = f"week of {ps:%d %b %Y}" if n == 7 else f"{ps:%d %b %Y} – {pe:%d %b %Y}"
    return TimeRange(start=ps, end=pe, label=lab)


@dataclass
class TimeParse:
    time_range: Optional[TimeRange] = None
    comparison: Optional[TimeRange] = None
    grain: Optional[str] = None
    compare_mode: Optional[str] = None        # "yoy", "previous", "explicit"
    horizon: Optional[int] = None             # forecast months
    assumptions: list[str] = field(default_factory=list)
    spans: list[tuple[int, int]] = field(default_factory=list)


def _num(tok: str) -> int:
    return int(tok) if tok.isdigit() else NUM.get(tok, 3)


def _find_ranges(t: str, res: TimeParse) -> list[tuple[int, TimeRange]]:
    """Return absolute time ranges found in the text, with their positions."""
    found: list[tuple[int, TimeRange]] = []

    def add(m, tr):
        found.append((m.start(), tr)); res.spans.append(m.span())

    for m in re.finditer(r"\b(?:fiscal|fy)\s*(?:year\s*)?'?(20\d{2}|\d{2})\b", t):
        y = int(m.group(1)); y = y + 2000 if y < 100 else y
        add(m, fiscal_year_range(y))
        res.assumptions.append(f"FY{y} is the fiscal year 1 Feb {y - 1} to 31 Jan {y}.")
    for m in re.finditer(r"\bq([1-4])\s*(?:of\s*)?(?:fy|calendar\s*)?'?(20\d{2})\b", t):
        if any(s <= m.start() < e for s, e in res.spans):
            continue
        add(m, quarter_range(int(m.group(2)), int(m.group(1))))
    for m in re.finditer(r"\b(first|second|third|fourth|1st|2nd|3rd|4th)\s+quarter\s+(?:of\s+)?(20\d{2})\b", t):
        add(m, quarter_range(int(m.group(2)), ORD[m.group(1)]))
    for m in re.finditer(r"\b(?:h([12])|(first|second)\s+half\s+(?:of\s+)?)\s*(?:of\s+)?(20\d{2})\b", t):
        h = int(m.group(1)) if m.group(1) else (1 if m.group(2) == "first" else 2)
        y = int(m.group(3))
        s, e = (date(y, 1, 1), date(y, 6, 30)) if h == 1 else (date(y, 7, 1), date(y, 12, 31))
        add(m, TimeRange(start=s, end=e, label=f"H{h} {y}"))
    # month ranges: "october to november 2024", "oct-nov 2024", "between october and november 2024"
    for m in re.finditer(rf"\b{MONTH_RE}\s*(?:-|–|to|through|and|until)\s*{MONTH_RE}\s*,?\s*(20\d{{2}})\b", t):
        if any(s <= m.start() < e for s, e in res.spans):
            continue
        y = int(m.group(3)); a, b = MONTHS[m.group(1)[:3]], MONTHS[m.group(2)[:3]]
        if b >= a:
            add(m, TimeRange(start=date(y, a, 1), end=month_end(y, b),
                             label=f"{date(y, a, 1):%b}–{date(y, b, 1):%b %Y}"))
    for m in re.finditer(rf"\b{MONTH_RE}\s*,?\s*(20\d{{2}})\b", t):
        if any(s <= m.start() < e for s, e in res.spans):
            continue
        add(m, month_range(int(m.group(2)), MONTHS[m.group(1)[:3]]))
    for m in re.finditer(r"(?<![\w-])(20[12]\d)(?![\w-])", t):
        if any(s <= m.start() < e for s, e in res.spans):
            continue
        add(m, year_range(int(m.group(1))))
    return sorted(found, key=lambda x: x[0])


def parse_time(text: str, ref: date = AS_OF_DATE) -> TimeParse:
    t = text.lower().replace("’", "'")
    res = TimeParse()
    rel_note = f"Relative dates use the latest complete period in the data (data ends {ref:%d %b %Y})."

    # ---------------- grain
    grain_pats = [("day", r"\b(daily|by day|per day|each day|day by day)\b"),
                  ("week", r"\b(weekly|by week|per week|each week|week over week|wow)\b"),
                  ("month", r"\b(monthly|by month|per month|each month|month over month|mom|month by month)\b"),
                  ("quarter", r"\b(quarterly|by quarter|per quarter|each quarter|quarter over quarter|qoq)\b"),
                  ("year", r"\b(yearly|annually|by year|per year|each year|year by year|annual trend)\b")]
    for g, p in grain_pats:
        if re.search(p, t):
            res.grain = g
            break
    if res.grain is None and re.search(r"\b(trend|over time|trajectory|history|evolution|time series|look like over)\b", t):
        res.grain = "month"

    # ---------------- forecast horizon
    m = re.search(r"\bnext\s+(\d+|one|two|three|four|five|six|twelve|eighteen)\s+(month|quarter|week|year)s?\b", t)
    if m:
        n, unit = _num(m.group(1)), m.group(2)
        res.horizon = {"month": n, "quarter": 3 * n, "year": 12 * n, "week": max(1, round(n / 4.3))}[unit]
    elif re.search(r"\bnext\s+quarter\b", t):
        res.horizon = 3
    elif re.search(r"\bnext\s+(year|12 months)\b", t):
        res.horizon = 12
    elif re.search(r"\bnext\s+month\b", t):
        res.horizon = 1
    elif re.search(r"\b(rest of|remainder of) (the )?year\b", t):
        res.horizon = 12

    # ---------------- absolute ranges
    found = _find_ranges(t, res)

    # ---------------- relative ranges
    rel: Optional[TimeRange] = None
    lcw = last_complete_week(ref)
    m = re.search(r"\b(?:last|past|previous|trailing)\s+(\d+|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|eighteen|twenty-four)\s+(day|week|month|quarter|year)s?\b", t)
    if m:
        n, unit = _num(m.group(1)), m.group(2)
        if unit == "day":
            rel = TimeRange(start=ref - timedelta(days=n - 1), end=ref, label=f"last {n} days")
        elif unit == "week":
            rel = TimeRange(start=lcw.end - timedelta(days=7 * n - 1), end=lcw.end, label=f"last {n} weeks")
        else:
            months = {"month": n, "quarter": 3 * n, "year": 12 * n}[unit]
            e = month_end(ref.year, ref.month)
            sm = ref.month - months + 1
            s = date(ref.year + (sm - 1) // 12, (sm - 1) % 12 + 1, 1)
            rel = TimeRange(start=s, end=min(e, ref), label=f"last {n} {unit}s")
        res.spans.append(m.span())
    elif re.search(r"\b(this|last|past|previous|the latest)\s+week\b", t):
        rel = lcw
        if re.search(r"\b(last|previous|past)\s+week\b", t) and re.search(r"\bthis\s+week\b", t):
            rel = previous_period(lcw)
    elif re.search(r"\b(year to date|ytd|this year|current year)\b", t):
        rel = year_range(ref.year)
    elif re.search(r"\b(last|previous|past) year\b", t):
        rel = year_range(ref.year)
    elif re.search(r"\b(this|current|last|previous|past|the latest|most recent) quarter\b", t):
        rel = quarter_range(ref.year, (ref.month - 1) // 3 + 1)
    elif re.search(r"\b(this|current|last|previous|past|most recent) month\b", t):
        rel = month_range(ref.year, ref.month)
    elif re.search(r"\b(all time|since the beginning|since launch|full history|entire history|ever)\b", t):
        rel = TimeRange(start=DATA_START, end=ref, label="all history")
    ms = re.search(rf"\bsince\s+(?:{MONTH_RE}\s+)?(20\d{{2}})\b", t)
    if ms:
        mon = MONTHS[ms.group(1)[:3]] if ms.group(1) else 1
        found = [(p, tr) for p, tr in found if not (ms.start() <= p < ms.end())]
        rel = TimeRange(start=date(int(ms.group(2)), mon, 1), end=ref,
                        label=f"since {date(int(ms.group(2)), mon, 1):%b %Y}")
    if rel is not None and not ms and rel.label != "all history":
        res.assumptions.append(rel_note)

    # ---------------- comparison cues
    cmp_words = r"(?:vs\.?|versus|compared (?:to|with)|against|relative to|than)"
    explicit = None
    mc = re.search(cmp_words, t)
    if mc and len(found) >= 2:
        before = [tr for p, tr in found if p < mc.start()]
        after = [tr for p, tr in found if p > mc.start()]
        if before and after:
            res.time_range, explicit = before[-1], after[0]
    if res.time_range is None:
        if found:
            res.time_range = found[0][1]
            if len(found) >= 2 and re.search(r"\b(from|between)\b", t) and not mc:
                # "from Q1 2025 to Q2 2025" / "between 2023 and 2024": compare second to first
                res.time_range, explicit = found[-1][1], found[0][1]
        elif rel is not None:
            res.time_range = rel
    if rel is not None and found and ms is None and res.time_range is rel:
        pass
    if explicit is not None:
        res.comparison, res.compare_mode = explicit, "explicit"
    elif re.search(r"\b(yoy|year over year|year-over-year|vs\.? last year|versus last year|compared (?:to|with) last year|same (?:period|quarter|month) last year|prior year)\b", t):
        res.compare_mode = "yoy"
    elif re.search(r"\b(vs\.? (?:the )?(?:previous|prior|last) (?:month|quarter|week|period)|(?:month|quarter|week) over (?:month|quarter|week)|mom|qoq|wow|sequential)\b", t):
        res.compare_mode = "previous"
    if res.time_range and res.compare_mode == "yoy":
        res.comparison = shift_years(res.time_range, -1)
    elif res.time_range and res.compare_mode == "previous":
        res.comparison = previous_period(res.time_range)
    return res


def default_comparison(tr: TimeRange) -> tuple[TimeRange, str]:
    """Year-over-year for months/quarters/years (avoids seasonality); previous period for weeks and short ranges."""
    if tr.days() <= 14:
        return previous_period(tr), "previous period"
    return shift_years(tr, -1), "same period last year"
