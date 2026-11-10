"""Run configuration: scoring profile, policies, target options. Everything has a default."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Literal, Optional

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

PROFILES: dict[str, dict[str, float]] = {
    "balanced": {"simplicity": 0.50, "resilience": 0.30, "policy": 0.20},
    "consolidate": {"simplicity": 0.70, "resilience": 0.15, "policy": 0.15},
    "resilient": {"simplicity": 0.35, "resilience": 0.50, "policy": 0.15},
    "compliance": {"simplicity": 0.35, "resilience": 0.20, "policy": 0.45},
}

PolicyType = Literal["isolate", "homogeneous", "separate", "colocate", "pin", "capacity", "qm_count"]


class Policy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = ""
    type: PolicyType
    mode: Literal["hard", "soft", "off"] = "hard"
    weight: float = Field(1.0, gt=0)
    when: dict[str, Any] = {}            # isolate / colocate / pin: which apps
    attribute: Optional[str] = None      # homogeneous / separate
    max_distinct: int = Field(1, ge=1)   # homogeneous
    groups: list[dict[str, Any]] = []    # separate: selectors whose apps must not mix
    qm: Optional[str] = None             # pin
    max_apps_per_qm: Optional[int] = Field(None, ge=1)
    max_queues_per_qm: Optional[int] = Field(None, ge=1)
    min: Optional[int] = Field(None, ge=1)   # qm_count
    max: Optional[int] = Field(None, ge=1)

    @model_validator(mode="after")
    def _check(self):
        t = self.type
        if t in ("isolate", "colocate", "pin") and not self.when:
            raise ValueError(f"policy '{self.id or t}': '{t}' needs a 'when' selector")
        if t == "homogeneous" and not self.attribute:
            raise ValueError(f"policy '{self.id or t}': 'homogeneous' needs 'attribute'")
        if t == "separate" and not (self.attribute or len(self.groups) >= 2):
            raise ValueError(f"policy '{self.id or t}': 'separate' needs 'attribute' or two 'groups'")
        if t == "pin" and not self.qm:
            raise ValueError(f"policy '{self.id or t}': 'pin' needs 'qm'")
        if t == "capacity" and not (self.max_apps_per_qm or self.max_queues_per_qm):
            raise ValueError(f"policy '{self.id or t}': 'capacity' needs a limit")
        if t == "qm_count" and not (self.min or self.max):
            raise ValueError(f"policy '{self.id or t}': 'qm_count' needs 'min' or 'max'")
        if t == "qm_count" and self.min and self.max and self.min > self.max:
            raise ValueError(f"policy '{self.id or t}': min is greater than max")
        return self


class TargetOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    eliminate_aliases: bool = True
    gateway_qms: list[str] = []          # empty = keep the QMs that already face external partners
    reuse_qm_names: bool = True
    new_qm_template: str = "QM{n:03d}"
    channel_template: str = "{src}.{dst}"


class Advanced(BaseModel):
    model_config = ConfigDict(extra="forbid")

    seed: int = 42
    resolutions: list[float] = [0.5, 1.0, 2.0]
    refine_passes: int = Field(3, ge=0)
    candidates: int = Field(6, ge=1)     # neighbouring QMs tried per app in refinement


DEFAULT_POLICIES = [{"id": "capacity", "type": "capacity", "max_apps_per_qm": 40, "mode": "hard"}]


class Config(BaseModel):
    model_config = ConfigDict(extra="forbid")

    profile: Literal["balanced", "consolidate", "resilient", "compliance"] = "balanced"
    weights: Optional[dict[str, float]] = None
    policies: Optional[list[Policy]] = None   # None = default capacity policy; [] = no policies
    target: TargetOptions = TargetOptions()
    advanced: Advanced = Advanced()

    @model_validator(mode="after")
    def _finish(self):
        if self.policies is None:
            self.policies = [Policy(**p) for p in DEFAULT_POLICIES]
        seen: dict[str, int] = {}
        for p in self.policies:
            base = p.id or p.type
            seen[base] = seen.get(base, 0) + 1
            p.id = base if seen[base] == 1 else f"{base}-{seen[base]}"
        if self.weights is not None:
            unknown = set(self.weights) - {"simplicity", "resilience", "policy"}
            if unknown:
                raise ValueError(f"unknown weight(s): {sorted(unknown)}")
            if any(v < 0 for v in self.weights.values()) or sum(self.weights.values()) <= 0:
                raise ValueError("weights must be non-negative and not all zero")
        return self

    def score_weights(self) -> dict[str, float]:
        w = dict(PROFILES[self.profile]) if self.weights is None else {
            k: float(self.weights.get(k, 0.0)) for k in ("simplicity", "resilience", "policy")}
        total = sum(w.values())
        return {k: v / total for k, v in w.items()}


def load_config(source: Any = None) -> Config:
    """Accept None, a dict, a YAML string, or a path to a YAML file."""
    if source is None:
        return Config()
    if isinstance(source, Config):
        return source
    if isinstance(source, dict):
        return Config(**source)
    text = Path(source).read_text(encoding="utf-8") if Path(str(source)).exists() else str(source)
    return Config(**(yaml.safe_load(text) or {}))


def matches(when: dict[str, Any], attrs: dict[str, str]) -> bool:
    """Selector: {attr: value | [values] | {"not": value | [values]}}. All keys must match."""
    for key, want in when.items():
        have = attrs.get(key, "")
        if isinstance(want, dict):
            banned = want.get("not")
            banned = banned if isinstance(banned, list) else [banned]
            if have in [str(b) for b in banned]:
                return False
        elif isinstance(want, list):
            if have not in [str(w) for w in want]:
                return False
        elif have != str(want):
            return False
    return True
