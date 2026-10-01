"""Forecasting module.

The LLM routes the question; Python models compute the numbers.

Candidate models on a monthly series:
  * seasonal naive with drift: last year's month x recent YoY growth
  * log-linear regression: trend + month-of-year effects (ridge-regularised)
  * ensemble: average of the two
Each is backtested on the last 6 months (fit on everything before). The model
with the lowest backtest MAPE is used. The 80% interval comes from backtest
errors, widened with the horizon.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd
from sklearn.linear_model import Ridge

from ..db import Database
from ..plan import SQLRecord, TimeRange
from ..semantic.catalog import Catalog
from ..semantic.compiler import Compiler, MetricRequest


@dataclass
class ForecastResult:
    metric: str
    history: pd.DataFrame            # period, value
    forecast: pd.DataFrame           # period, yhat, lo, hi
    model: str
    backtest_mape: dict[str, float]
    sql: list[SQLRecord] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def total(self) -> float:
        return float(self.forecast["yhat"].sum())


def _seasonal_naive(y: np.ndarray, h: int) -> np.ndarray:
    n = len(y)
    if n < 24:
        return np.repeat(y[-3:].mean(), h)
    growth = y[-12:].sum() / max(y[-24:-12].sum(), 1e-9)
    growth = float(np.clip(growth, 0.7, 1.4))
    return np.array([y[n - 12 + (i % 12)] * growth ** (1 + i // 12) for i in range(h)])


def _loglinear(y: np.ndarray, months: np.ndarray, h: int, positive: bool) -> np.ndarray:
    n = len(y)
    t = np.arange(n + h)
    mo = np.concatenate([months, [(months[-1] + i) % 12 + 1 for i in range(1, h + 1)]])
    X = np.column_stack([t / 12.0] + [(mo == m).astype(float) for m in range(2, 13)])
    target = np.log(np.maximum(y, 1e-9)) if positive else y
    model = Ridge(alpha=0.3).fit(X[:n], target)
    pred = model.predict(X[n:])
    return np.exp(pred) if positive else pred


def _mape(a: np.ndarray, f: np.ndarray) -> float:
    m = np.abs(a) > 1e-9
    return float(np.mean(np.abs((a[m] - f[m]) / a[m])) * 100) if m.any() else float("nan")


class Forecaster:
    def __init__(self, db: Database, catalog: Catalog, compiler: Compiler):
        self.db, self.cat, self.comp = db, catalog, compiler

    def monthly(self, metric: str, tr: TimeRange, filters: dict, rec: list) -> pd.DataFrame:
        df, r = self.db.run(self.comp.compile(MetricRequest([metric], tr, [], filters, grain="month")),
                            purpose=f"monthly {metric} history")
        rec.append(r)
        df["period"] = pd.to_datetime(df["period"])
        return df.rename(columns={metric: "value"})[["period", "value"]].fillna(0.0)

    def forecast(self, metric: str, history_range: TimeRange, horizon: int = 3, filters: dict | None = None,
                 holdout: int = 6) -> ForecastResult:
        rec: list[SQLRecord] = []
        hist = self.monthly(metric, history_range, filters or {}, rec)
        met = self.cat.metrics[metric]
        notes = []
        # drop an incomplete final month
        last = hist["period"].max()
        if last is not pd.NaT and (last + pd.offsets.MonthEnd(0)).date() > history_range.end:
            hist = hist[hist["period"] < last]
            notes.append("The latest month is incomplete and was excluded from model fitting.")
        y = hist["value"].to_numpy(dtype=float)
        months = hist["period"].dt.month.to_numpy()
        positive = bool((y > 0).all())
        cands = {
            "seasonal naive with drift": lambda yy, mm, hh: _seasonal_naive(yy, hh),
            "log-linear trend + seasonality": lambda yy, mm, hh: _loglinear(yy, mm, hh, positive),
        }
        mape, errs = {}, {}
        if len(y) > holdout + 12:
            for name, fn in cands.items():
                f = fn(y[:-holdout], months[:-holdout], holdout)
                mape[name] = _mape(y[-holdout:], f)
                errs[name] = (y[-holdout:] - f) / np.where(np.abs(f) > 1e-9, f, 1)
            ens = np.mean([cands[n](y[:-holdout], months[:-holdout], holdout) for n in cands], axis=0)
            mape["ensemble"] = _mape(y[-holdout:], ens)
            errs["ensemble"] = (y[-holdout:] - ens) / np.where(np.abs(ens) > 1e-9, ens, 1)
            best = min(mape, key=mape.get)
        else:
            best = "seasonal naive with drift"
            notes.append("History is too short for a backtest; using the seasonal naive model.")
            errs[best] = np.array([0.1])
        if best == "ensemble":
            yhat = np.mean([fn(y, months, horizon) for fn in cands.values()], axis=0)
        else:
            yhat = cands[best](y, months, horizon)
        sigma = float(np.std(errs[best])) if len(errs[best]) > 1 else 0.1
        sigma = max(sigma, 0.02)
        steps = np.arange(1, horizon + 1)
        band = 1.2816 * sigma * np.sqrt(steps)
        periods = pd.date_range(hist["period"].max() + pd.offsets.MonthBegin(1), periods=horizon, freq="MS")
        fc = pd.DataFrame({"period": periods, "yhat": yhat, "lo": yhat * (1 - band), "hi": yhat * (1 + band)})
        if met.format == "percent":
            fc[["yhat", "lo", "hi"]] = fc[["yhat", "lo", "hi"]].clip(0, 1)
        notes.append(f"Model chosen by lowest backtest error on the last {holdout} months: {best}.")
        return ForecastResult(metric, hist, fc, best, mape, rec, notes)
