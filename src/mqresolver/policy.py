"""Policy engine: compile configured policies against the applications, evaluate any topology."""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Optional

from .config import Config, Policy, matches


class PolicyError(ValueError):
    """A policy cannot be applied (unknown attribute, contradiction, infeasible). The message says why."""


@dataclass
class LabelRule:
    """Apps on one QM may carry at most `max_distinct` different labels. A None label is unconstrained."""
    policy: Policy
    labels: dict[str, Optional[str]]
    max_distinct: int


@dataclass
class Compiled:
    policies: list[Policy]
    label_rules: list[LabelRule] = field(default_factory=list)
    isolate: list[tuple[Policy, set[str]]] = field(default_factory=list)
    colocate: list[tuple[Policy, set[str]]] = field(default_factory=list)
    pins: list[tuple[Policy, set[str]]] = field(default_factory=list)
    capacity: list[Policy] = field(default_factory=list)
    qm_count: list[Policy] = field(default_factory=list)

    def limit(self, what: str, mode: str = "hard") -> Optional[int]:
        vals = [getattr(p, what) for p in self.capacity if p.mode == mode and getattr(p, what)]
        return min(vals) if vals else None


@dataclass
class PolicyResult:
    id: str
    type: str
    mode: str
    weight: float
    violation: float          # 0..1
    violating: int
    scope: int
    detail: str
    offenders: list[str]

    def as_dict(self) -> dict:
        return self.__dict__.copy()


def _select(policy: Policy, apps: dict[str, dict]) -> set[str]:
    return {a for a, info in apps.items() if matches(policy.when, info["attrs"])}


def _check_attrs(policy: Policy, names: set[str], attr_names: list[str]) -> None:
    unknown = sorted(n for n in names if n not in attr_names)
    if unknown:
        raise PolicyError(
            f"policy '{policy.id}' refers to attribute(s) {unknown} that are not in the inventory. "
            f"Available attributes: {sorted(attr_names)}")


def compile_policies(config: Config, apps: dict[str, dict], attr_names: list[str]) -> Compiled:
    out = Compiled(policies=[p for p in config.policies if p.mode != "off"])
    for p in out.policies:
        if p.type in ("isolate", "colocate", "pin"):
            _check_attrs(p, set(p.when), attr_names + ["app_id"])
            sel = {a for a, info in apps.items() if matches(p.when, {**info["attrs"], "app_id": a})}
            {"isolate": out.isolate, "colocate": out.colocate, "pins": out.pins}[
                "pins" if p.type == "pin" else p.type].append((p, sel))
        elif p.type == "homogeneous":
            _check_attrs(p, {p.attribute}, attr_names)
            labels = {a: (info["attrs"].get(p.attribute) or "(blank)") for a, info in apps.items()}
            out.label_rules.append(LabelRule(p, labels, p.max_distinct))
        elif p.type == "separate":
            if p.attribute:
                _check_attrs(p, {p.attribute}, attr_names)
                labels = {a: (info["attrs"].get(p.attribute) or "(blank)") for a, info in apps.items()}
            else:
                for g in p.groups:
                    _check_attrs(p, set(g), attr_names + ["app_id"])
                labels = {}
                for a, info in apps.items():
                    hit = [i for i, g in enumerate(p.groups) if matches(g, {**info["attrs"], "app_id": a})]
                    if len(hit) > 1:
                        raise PolicyError(f"policy '{p.id}': app {a} matches more than one group")
                    labels[a] = f"group {hit[0] + 1}" if hit else None
            out.label_rules.append(LabelRule(p, labels, 1))
        elif p.type == "capacity":
            out.capacity.append(p)
        elif p.type == "qm_count":
            out.qm_count.append(p)
    check_conflicts(out)
    return out


def check_conflicts(c: Compiled) -> None:
    """Fail early, naming the rules and apps, when hard policies contradict each other."""
    iso = set().union(*[s for p, s in c.isolate if p.mode == "hard"]) if c.isolate else set()
    pinned: dict[str, tuple[str, str]] = {}
    for p, sel in c.pins:
        if p.mode != "hard":
            continue
        for a in sel:
            if a in pinned and pinned[a][0] != p.qm:
                raise PolicyError(f"app {a} is pinned to both {pinned[a][0]} ('{pinned[a][1]}') and {p.qm} ('{p.id}')")
            pinned[a] = (p.qm, p.id)
    by_qm: dict[str, set[str]] = {}
    for a, (qm, _) in pinned.items():
        by_qm.setdefault(qm, set()).add(a)
    for qm, members in by_qm.items():
        clash = sorted(members & iso)
        if clash and len(members) > 1:
            raise PolicyError(f"apps {clash[:5]} must be isolated but share pinned QM {qm} with other apps")
    max_apps = c.limit("max_apps_per_qm")
    for p, sel in c.colocate:
        if p.mode != "hard" or len(sel) < 2:
            continue
        clash = sorted(sel & iso)
        if clash:
            raise PolicyError(f"policy '{p.id}' co-locates apps {clash[:5]} that an isolate policy keeps alone")
        targets = {pinned[a][0] for a in sel if a in pinned}
        if len(targets) > 1:
            raise PolicyError(f"policy '{p.id}' co-locates apps pinned to different QMs: {sorted(targets)}")
        if max_apps and len(sel) > max_apps:
            raise PolicyError(f"policy '{p.id}' co-locates {len(sel)} apps but the capacity limit is {max_apps} per QM")
        for rule in c.label_rules:
            if rule.policy.mode != "hard":
                continue
            labs = {rule.labels[a] for a in sel if rule.labels.get(a) is not None}
            if len(labs) > rule.max_distinct:
                raise PolicyError(
                    f"policy '{p.id}' co-locates apps that policy '{rule.policy.id}' keeps apart "
                    f"(values: {sorted(labs)[:5]})")
    for qm, members in by_qm.items():
        for rule in c.label_rules:
            if rule.policy.mode != "hard":
                continue
            labs = {rule.labels[a] for a in members if rule.labels.get(a) is not None}
            if len(labs) > rule.max_distinct:
                raise PolicyError(f"apps pinned to {qm} break policy '{rule.policy.id}' (values: {sorted(labs)[:5]})")
        if max_apps and len(members) > max_apps:
            raise PolicyError(f"{len(members)} apps are pinned to {qm} but the capacity limit is {max_apps}")


def evaluate(c: Compiled, qm_apps: dict[str, set[str]], queues_per_qm: dict[str, int],
             app_qms: Optional[dict[str, set[str]]] = None) -> list[PolicyResult]:
    """Measure every enabled policy on a topology given as QM -> apps."""
    if app_qms is None:
        app_qms = {}
        for q, members in qm_apps.items():
            for a in members:
                app_qms.setdefault(a, set()).add(q)
    n_apps = max(len(app_qms), 1)
    hosting = {q: m for q, m in qm_apps.items() if m}
    res: list[PolicyResult] = []

    def add(p, bad: set, scope: int, detail: str):
        res.append(PolicyResult(p.id, p.type, p.mode, p.weight, min(1.0, len(bad) / scope) if scope else 0.0,
                                len(bad), scope, detail, sorted(bad)[:25]))

    for p, sel in c.isolate:
        present = sel & set(app_qms)
        bad = {a for a in present if any(len(hosting[q]) > 1 for q in app_qms[a])}
        add(p, bad, len(present), f"{len(bad)} of {len(present)} matching apps share a QM")
    for rule in c.label_rules:
        bad: set[str] = set()
        mixed = 0
        for q, members in hosting.items():
            cnt = Counter(rule.labels[a] for a in members if rule.labels.get(a) is not None)
            if len(cnt) > rule.max_distinct:
                mixed += 1
                keep = {k for k, _ in sorted(cnt.items(), key=lambda kv: (-kv[1], kv[0]))[:rule.max_distinct]}
                bad |= {a for a in members if rule.labels.get(a) is not None and rule.labels[a] not in keep}
        add(rule.policy, bad, n_apps, f"{mixed} QMs mix more than {rule.max_distinct} value(s)")
    for p, sel in c.colocate:
        present = sel & set(app_qms)
        cnt = Counter(q for a in present for q in app_qms[a])
        modal = sorted(cnt.items(), key=lambda kv: (-kv[1], kv[0]))[0][0] if cnt else None
        bad = {a for a in present if app_qms[a] != {modal}}
        add(p, bad, len(present), f"{len(bad)} of {len(present)} apps are not together")
    for p, sel in c.pins:
        present = sel & set(app_qms)
        bad = {a for a in present if p.qm not in app_qms[a]}
        add(p, bad, len(present), f"{len(bad)} of {len(present)} apps are not on {p.qm}")
    for p in c.capacity:
        over_apps = {q: len(m) - p.max_apps_per_qm for q, m in hosting.items()
                     if p.max_apps_per_qm and len(m) > p.max_apps_per_qm}
        over_q = {q: n - p.max_queues_per_qm for q, n in queues_per_qm.items()
                  if p.max_queues_per_qm and n > p.max_queues_per_qm}
        frac = max(sum(over_apps.values()) / n_apps,
                   sum(over_q.values()) / max(sum(queues_per_qm.values()), 1))
        offenders = sorted(set(over_apps) | set(over_q))
        res.append(PolicyResult(p.id, p.type, p.mode, p.weight, min(1.0, frac), len(offenders), len(hosting),
                                f"{len(offenders)} QMs over the limit", offenders[:25]))
    for p in c.qm_count:
        n = len(hosting)
        over = max(0, n - p.max) / p.max if p.max else 0.0
        under = max(0, p.min - n) / p.min if p.min else 0.0
        frac = min(1.0, max(over, under))
        res.append(PolicyResult(p.id, p.type, p.mode, p.weight, frac, int(frac > 0), 1,
                                f"{n} QMs (allowed {p.min or 1}..{p.max or 'any'})", []))
    return res


def policy_dimension(results: list[PolicyResult]) -> float:
    total = sum(r.weight for r in results)
    return sum(r.weight * r.violation for r in results) / total if total else 0.0


def suggest_policies(apps: dict[str, dict], attr_names: list[str], apps_per_qm: list[int]) -> list[dict]:
    """Policies worth offering, derived from the data. All start switched off except capacity."""
    out: list[dict] = []
    sizes = sorted(apps_per_qm) or [1]
    p90 = sizes[min(len(sizes) - 1, int(len(sizes) * 0.9))]
    out.append({"id": "capacity", "type": "capacity", "mode": "hard",
                "max_apps_per_qm": int(min(60, max(20, round(p90 * 2 / 5) * 5)))})
    n = max(len(apps), 1)
    for attr in attr_names:
        cnt = Counter(info["attrs"].get(attr, "") for info in apps.values())
        cnt.pop("", None)
        if not 2 <= len(cnt) <= 25:
            continue
        slug = attr.replace("app_", "").replace("_", "-").lower()
        if len(cnt) >= 3:
            out.append({"id": f"one-{slug}-per-qm", "type": "homogeneous", "attribute": attr, "mode": "off"})
        else:
            out.append({"id": f"separate-{slug}", "type": "separate", "attribute": attr, "mode": "off"})
        for value, k in sorted(cnt.items(), key=lambda kv: (kv[1], kv[0])):
            if k / n <= 0.10 and k >= 2:
                vslug = "".join(ch.lower() if ch.isalnum() else "-" for ch in value).strip("-")
                out.append({"id": f"isolate-{slug}-{vslug}", "type": "isolate", "when": {attr: value}, "mode": "off"})
                break
    return out
