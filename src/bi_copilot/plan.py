"""Structured objects that flow through the Copilot.

A QueryPlan is what the planner (LLM or offline) produces from a question.
It is validated before anything touches the database. An Answer is what the
UI renders, and every Answer carries its Evidence.
"""
from __future__ import annotations

from datetime import date
from typing import Any, Literal, Optional

from pydantic import BaseModel, Field

Intent = Literal[
    "metric",        # a number, a breakdown or a trend
    "root_cause",    # why did X change
    "forecast",      # what will X be
    "anomaly",       # anything unusual
    "brief",         # executive summary for a period
    "definition",    # what does X mean / why do two numbers differ (RAG)
    "investigate",   # bounded multi-step agent investigation
    "unsupported",   # cannot be answered from the data
    "clarify",       # ambiguous; ask the user
]
Grain = Literal["day", "week", "month", "quarter", "year"]


class TimeRange(BaseModel):
    start: date
    end: date
    label: str

    def days(self) -> int:
        return (self.end - self.start).days + 1


class QueryPlan(BaseModel):
    question: str
    intent: Intent
    metrics: list[str] = Field(default_factory=list)
    dimensions: list[str] = Field(default_factory=list)
    filters: dict[str, list[str]] = Field(default_factory=dict)
    time_range: Optional[TimeRange] = None
    comparison: Optional[TimeRange] = None
    time_grain: Optional[Grain] = None
    order: Literal["desc", "asc"] = "desc"
    limit: Optional[int] = None
    horizon: Optional[int] = None
    reason: Optional[str] = None
    assumptions: list[str] = Field(default_factory=list)
    planner: str = "offline"


class SQLRecord(BaseModel):
    purpose: str
    sql: str
    rows: int = 0
    tables: list[str] = Field(default_factory=list)
    ms: float = 0.0


class Evidence(BaseModel):
    sql: list[SQLRecord] = Field(default_factory=list)
    metrics: dict[str, str] = Field(default_factory=dict)       # name -> definition
    filters: dict[str, list[str]] = Field(default_factory=dict)
    periods: list[str] = Field(default_factory=list)
    data_as_of: Optional[str] = None
    documents: list[str] = Field(default_factory=list)           # retrieved knowledge-base chunks
    limitations: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    agent_trace: list[dict[str, Any]] = Field(default_factory=list)

    def tables(self) -> list[str]:
        return sorted({t for s in self.sql for t in s.tables})


class KPI(BaseModel):
    label: str
    value: float
    fmt: str = "number"
    delta_pct: Optional[float] = None
    delta_abs: Optional[float] = None
    direction: str = "up"   # which way is good


class Driver(BaseModel):
    dimension: str
    member: str
    delta: float
    share_of_change: float
    current: Optional[float] = None
    previous: Optional[float] = None


class Answer(BaseModel):
    question: str
    status: Literal["ok", "unsupported", "clarify", "error"] = "ok"
    intent: str = "metric"
    headline: str = ""
    narrative: str = ""
    kpis: list[KPI] = Field(default_factory=list)
    drivers: list[Driver] = Field(default_factory=list)
    table: Optional[list[dict[str, Any]]] = None
    chart: Optional[dict[str, Any]] = None       # plotly figure as dict
    chart_type: Optional[str] = None
    followups: list[str] = Field(default_factory=list)
    evidence: Evidence = Field(default_factory=Evidence)
    plan: Optional[QueryPlan] = None
    facts: dict[str, float] = Field(default_factory=dict)  # every number the narrative may use
    grounded: Optional[bool] = None
    confidence: Literal["high", "medium", "low"] = "high"
