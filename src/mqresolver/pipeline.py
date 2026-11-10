"""End-to-end run: inventory in, target topology and full report out."""
from __future__ import annotations

import io
import time
from collections import Counter
from typing import Any, Optional

import pandas as pd

from . import __version__
from .config import PROFILES, Config, load_config
from .generate import generate
from .graph import qm_graph
from .ingest import Inventory, load_inventory, propose_mapping, read_table, to_original_format, TOPOLOGY
from .model import Model, build_model, quality_report
from .optimise import optimise
from .policy import compile_policies, evaluate, policy_dimension, suggest_policies
from .score import (describe, flow_base, inputs_from_assignment, inputs_from_model, sensitivity, tci_of)
from .validate import validate


def inspect(source: Any, filename: Optional[str] = None, mapping: Optional[dict] = None) -> dict:
    """Look at a file before running: columns, proposed mapping, attributes, suggested policies."""
    raw = source if isinstance(source, pd.DataFrame) else read_table(source, filename)
    columns = list(raw.columns)
    proposed = propose_mapping(columns)
    out: dict[str, Any] = {"columns": columns, "rows": int(len(raw)), "proposed_mapping": proposed,
                           "canonical_columns": TOPOLOGY, "ready": False}
    try:
        inv = load_inventory(raw, mapping=mapping)
    except ValueError as exc:
        out["problem"] = str(exc)
        return out
    model = build_model(inv.df, inv.attribute_columns)
    attrs = {}
    for name in model.attr_names:
        cnt = Counter(i["attrs"].get(name) or "(blank)" for i in model.apps.values())
        if len(cnt) <= 40:
            attrs[name] = sorted(cnt.items(), key=lambda kv: (-kv[1], kv[0]))
    out.update(ready=True, quality={**inv.quality, **quality_report(model)}, attributes=attrs,
               suggested_policies=suggest_policies(model.apps, [a for a in model.attr_names if a in attrs],
                                                   [len(v) for v in model.qm_apps.values()]))
    return out


def _summary(model: Model) -> dict:
    pairs = {tuple(sorted(l)) for l in model.links}
    sizes = [len(v) for v in model.qm_apps.values()] or [0]
    same = sum(1 for p in model.paths if p.producer_qm and p.producer_qm == p.consumer_qm)
    internal = sum(1 for p in model.paths if p.producer is not None and p.consumer is not None)
    return {
        "queue_managers": len(model.qm_apps),
        "external_gateways": len(model.external_gateways()),
        "channel_pairs": len(pairs),
        "directed_links": len(model.links),
        "queue_definitions": len(model.definitions),
        "alias_queues": sum(1 for t in model.definitions.values() if t == "Alias"),
        "remote_queues": sum(1 for t in model.definitions.values() if t == "Remote"),
        "max_apps_per_qm": max(sizes),
        "apps_on_multiple_qms": sum(1 for q in model.app_qms.values() if len(q) > 1),
        "same_qm_flow_pct": round(100 * same / internal, 1) if internal else 0.0,
    }


def _csv(df: pd.DataFrame) -> str:
    buf = io.StringIO()
    df.to_csv(buf, index=False, lineterminator="\n")
    return buf.getvalue()


def run(source: Any, filename: Optional[str] = None, mapping: Optional[dict] = None,
        config: Any = None, include_graph: bool = True) -> dict:
    t0 = time.time()
    cfg: Config = load_config(config)
    inv: Inventory = load_inventory(source, filename, mapping)
    model = build_model(inv.df, inv.attribute_columns)
    if not model.apps:
        raise ValueError("the inventory has no rows tied to an application, so there is nothing to place")
    comp = compile_policies(cfg, model.apps, model.attr_names)
    base = flow_base(model)
    weights = cfg.score_weights()

    result = optimise(model, base, comp, cfg)
    target = generate(inv, model, result, cfg)
    gateways = {result.gateway_map.get(g, g) for g in model.external_gateways()}
    report = validate(model, target.df, target.channels, comp, cfg, gateways)
    tmodel = build_model(target.df, model.attr_names)

    # ---- scores: same formula for every topology ------------------------------
    pol_as_is = evaluate(comp, model.qm_apps, model.queues_per_qm(), model.app_qms)
    pol_target = evaluate(comp, tmodel.qm_apps, tmodel.queues_per_qm(), tmodel.app_qms)
    si_as_is = inputs_from_model(model, policy_dimension(pol_as_is))
    si_target = inputs_from_model(tmodel, policy_dimension(pol_target))
    s_as_is, s_target = tci_of(si_as_is, weights), tci_of(si_target, weights)

    def anchor(name: str, assign: dict[str, str]) -> dict:
        qa: dict[str, set] = {}
        for a, q in assign.items():
            qa.setdefault(q, set()).add(a)
        est = {q: sum(base.app_queue_weight.get(a, 0) for a in m) for q, m in qa.items()}
        si = inputs_from_assignment(base, assign, policy_dimension(evaluate(comp, qa, est)), result.gateway_map)
        return {"name": name, **describe(si, weights)}

    anchors = [anchor("Single queue manager", {a: "ONE" for a in base.apps}),
               anchor("One queue manager per app", {a: f"QM:{a}" for a in base.apps})]
    anchors_ok = all(s_target < x["tci"] for x in anchors) and s_target < s_as_is

    # ---- migration effort and decisions -----------------------------------------
    partners: dict[str, Counter] = {a: Counter() for a in model.apps}
    for (p, c), n in model.flow_weights().items():
        if p != c:
            partners[p][c] += n
            partners[c][p] += n
    decisions = []
    for a in sorted(model.apps):
        to = result.assignment[a]
        frm = sorted(model.app_qms.get(a, ()))
        ps = partners[a]
        decisions.append({
            "app": a, "name": model.apps[a]["name"], "attrs": model.apps[a]["attrs"],
            "from": frm, "to": to, "stays": to in frm, "reason": result.reasons[a],
            "partners": len(ps), "partners_on_same_qm": sum(1 for b in ps if result.assignment[b] == to),
            "top_partners": [b for b, _ in sorted(ps.items(), key=lambda kv: (-kv[1], kv[0]))[:5]],
        })
    target_qms = set(tmodel.qm_apps)
    as_is_qms = set(model.qm_apps)
    migration = {
        "apps_keeping_a_current_qm": sum(1 for d in decisions if d["stays"]),
        "apps_moving_to_another_qm": sum(1 for d in decisions if not d["stays"]),
        "app_connections_removed": sum(len(q) for q in model.app_qms.values()) - len(model.apps),
        "qms_reused": len(target_qms & as_is_qms),
        "qms_created": len(target_qms - as_is_qms),
        "qms_retired": len(as_is_qms - target_qms),
        "queue_definitions_before": len(model.definitions),
        "queue_definitions_after": len(tmodel.definitions),
        "channel_definitions_after": len(target.channels),
        "transmission_queues_after": len(tmodel.xmitqs),
    }

    out_df = to_original_format(inv, target.df)
    columns_ok = list(out_df.columns) == inv.original_columns
    report["checks"].append({"id": "format", "rule": "Target file has the input's columns in the input's order",
                             "passed": columns_ok, "count": 0 if columns_ok else 1, "examples": []})
    report["passed"] = report["passed"] and columns_ok

    bundle: dict[str, Any] = {
        "version": __version__,
        "seconds": 0.0,
        "config": cfg.model_dump(),
        "summary": {"apps": len(model.apps), "as_is": _summary(model), "target": _summary(tmodel)},
        "score": {
            "weights": weights, "profile": cfg.profile if cfg.weights is None else "custom",
            "as_is": describe(si_as_is, weights), "target": describe(si_target, weights),
            "change_points": round(s_target - s_as_is, 2),
            "change_pct": round(100 * (s_target - s_as_is) / s_as_is, 1) if s_as_is else 0.0,
            "anchors": anchors, "anchors_ok": anchors_ok,
            "sensitivity": sensitivity(si_as_is, si_target),
        },
        "policies": {"as_is": [r.as_dict() for r in pol_as_is], "target": [r.as_dict() for r in pol_target]},
        "validation": report,
        "quality": {**inv.quality, **quality_report(model)},
        "migration": migration,
        "decisions": decisions,
        "optimiser": {"candidates": result.candidates, "notes": result.notes,
                      "gateway_map": result.gateway_map},
        "warnings": result.notes + target.warnings,
        "renamed_queues": target.renamed,
        "files": {"target_topology.csv": _csv(out_df),
                  "target_channels.csv": _csv(pd.DataFrame(target.channels)),
                  "target.mqsc": target.mqsc},
    }
    if include_graph:
        status_t = {q: ("reused" if q in as_is_qms else "new") for q in tmodel.qms}
        status_a = {q: ("kept" if q in target_qms else "retired") for q in model.qm_apps}
        g_as_is = qm_graph(model, cfg.advanced.seed, status=status_a)
        init = {n["id"]: (n["x"], n["y"]) for n in g_as_is["nodes"]}
        bundle["graph"] = {"as_is": g_as_is,
                           "target": qm_graph(tmodel, cfg.advanced.seed, init=init, status=status_t),
                           "attributes": [a for a in model.attr_names
                                          if len({i["attrs"].get(a) for i in model.apps.values()}) <= 40]}
    bundle["seconds"] = round(time.time() - t0, 2)
    return bundle


__all__ = ["run", "inspect", "PROFILES"]
