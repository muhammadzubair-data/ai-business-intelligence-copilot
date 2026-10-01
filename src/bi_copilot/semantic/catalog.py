"""Loads and validates config/metrics.yaml."""
from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Optional

import yaml

from ..config import settings


@dataclass
class Metric:
    name: str
    label: str
    type: str
    definition: str
    format: str = "number"
    direction: str = "up"
    owner: str = ""
    grain: str = ""
    model: Optional[str] = None
    expression: Optional[str] = None
    filter: Optional[str] = None
    time_column: Optional[str] = None
    numerator: Optional[str] = None
    denominator: Optional[str] = None
    formula: Optional[str] = None
    components: list[str] = field(default_factory=list)
    template: Optional[str] = None
    allowed_dimensions: Optional[list[str]] = None
    synonyms: list[str] = field(default_factory=list)
    hidden: bool = False
    additive: bool = True

    @property
    def is_ratio(self) -> bool:
        return self.type == "ratio"


@dataclass
class Model:
    name: str
    view: str
    description: str
    dimensions: dict[str, str]


@dataclass
class Dimension:
    name: str
    label: str
    synonyms: list[str]


class Catalog:
    def __init__(self, path: Path | None = None):
        raw = yaml.safe_load(Path(path or settings.metrics_yaml).read_text())
        self.models = {k: Model(k, v["view"], v.get("description", ""), v["dimensions"]) for k, v in raw["models"].items()}
        self.dimensions = {k: Dimension(k, v["label"], v.get("synonyms", [])) for k, v in raw["dimensions"].items()}
        self.metrics: dict[str, Metric] = {}
        for k, v in raw["metrics"].items():
            self.metrics[k] = Metric(name=k, **v)
        self.unsupported: dict[str, str] = raw.get("unsupported_concepts", {})
        self._resolve()
        self.validate()

    # ratio metrics inherit model, filter and time column from their components
    def _resolve(self) -> None:
        for m in self.metrics.values():
            if m.type == "ratio":
                num = self.metrics[m.numerator]
                m.model, m.time_column, m.filter = num.model, num.time_column, num.filter
                m.additive = False
            elif m.type in ("derived", "custom"):
                m.additive = m.type == "derived" and m.format == "currency"
            if m.format == "percent" or m.format == "decimal":
                m.additive = False

    def validate(self) -> None:
        for m in self.metrics.values():
            if m.type == "simple":
                assert m.model in self.models, f"{m.name}: unknown model {m.model}"
                assert m.expression and m.time_column, f"{m.name}: expression/time_column required"
            elif m.type == "ratio":
                num, den = self.metrics[m.numerator], self.metrics[m.denominator]
                assert num.type == den.type == "simple", f"{m.name}: ratio components must be simple"
                assert num.model == den.model, f"{m.name}: components from different models"
                assert num.time_column == den.time_column, f"{m.name}: components use different time columns"
                assert num.filter == den.filter, f"{m.name}: components use different row filters"
            elif m.type == "derived":
                for c in m.components:
                    assert c in self.metrics, f"{m.name}: unknown component {c}"
            elif m.type == "custom":
                assert m.template, f"{m.name}: template required"
            else:
                raise AssertionError(f"{m.name}: unknown type {m.type}")
        for mod in self.models.values():
            for d in mod.dimensions:
                assert d in self.dimensions, f"model {mod.name}: undeclared dimension {d}"

    # ------------------------------------------------------------------ helpers
    def visible_metrics(self) -> list[Metric]:
        return [m for m in self.metrics.values() if not m.hidden]

    def models_of(self, metric: str) -> set[str]:
        m = self.metrics[metric]
        if m.type == "derived":
            return set().union(*(self.models_of(c) for c in m.components))
        return {m.model}

    def allowed_dimensions(self, metric: str) -> set[str]:
        m = self.metrics[metric]
        if m.type == "custom":
            return set(m.allowed_dimensions or [])
        if m.type == "derived":
            return set()
        return set(self.models[m.model].dimensions)

    def column(self, metric: str, dimension: str) -> str:
        return self.models[self.metrics[metric].model].dimensions[dimension]

    def describe_for_llm(self) -> str:
        lines = ["METRICS (name | label | definition | allowed dimensions):"]
        for m in self.visible_metrics():
            dims = ", ".join(sorted(self.allowed_dimensions(m.name))) or "time only"
            lines.append(f"- {m.name} | {m.label} | {m.definition} | dims: {dims}")
        lines.append("\nDIMENSIONS:")
        for d in self.dimensions.values():
            lines.append(f"- {d.name} ({d.label}); synonyms: {', '.join(d.synonyms)}")
        lines.append("\nUNSUPPORTED CONCEPTS (must be declined): " + ", ".join(self.unsupported))
        return "\n".join(lines)


@lru_cache(maxsize=1)
def get_catalog() -> Catalog:
    return Catalog()
