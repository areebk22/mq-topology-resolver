"""Independent check of a target topology.

Works only from the target rows, the channel list and the as-is model. It shares
no logic with generate.py, so a generator bug cannot hide itself.
"""
from __future__ import annotations

from collections import Counter

import pandas as pd

from .config import Config
from .model import Model, build_model
from .policy import Compiled, evaluate


def _flow_counts(model: Model) -> tuple[Counter, Counter, Counter]:
    """Verified flows as (producer, consumer) -> number of distinct queues; plus external out / in."""
    inner, out, inn = set(), set(), set()
    for p in model.paths:
        if not p.verified:
            continue
        if p.producer is not None and p.consumer is not None:
            inner.add((p.producer, p.consumer, p.queue))
        elif p.consumer is None:
            out.add((p.producer, p.queue))
        else:
            inn.add((p.consumer, p.queue))
    return (Counter((p, c) for p, c, _ in inner), Counter(p for p, _ in out), Counter(c for c, _ in inn))


def validate(as_is: Model, target_df: pd.DataFrame, channels: list[dict], comp: Compiled, config: Config,
             gateways: set[str], target_model: Model | None = None) -> dict:
    """`target_model` may be passed to save rebuilding it; it must come from build_model(target_df)."""
    tgt = target_model if target_model is not None else build_model(target_df, as_is.attr_names)
    checks: list[dict] = []

    def check(cid: str, rule: str, problems: list[str]) -> None:
        checks.append({"id": cid, "rule": rule, "passed": not problems, "count": len(problems),
                       "examples": problems[:15]})

    # S1 -------------------------------------------------------------------
    bad = [f"{a} is missing from the target" for a in sorted(set(as_is.apps) - set(tgt.apps))]
    bad += [f"{a} is on {len(q)} QMs: {sorted(q)}" for a, q in sorted(tgt.app_qms.items()) if len(q) != 1]
    check("S1", "Every application connects to exactly one queue manager", bad)

    # S2 -------------------------------------------------------------------
    rows = target_df.to_dict("records")
    local = {(r["queue_manager"], r["queue_name"]) for r in rows
             if r["queue_type"] == "Local" and r.get("queue_usage") != "Transmission"}
    xmit = {(r["queue_manager"], r["queue_name"]) for r in rows if r.get("queue_usage") == "Transmission"}
    hosting = set(tgt.qm_apps)
    bad = []
    for r in rows:
        if r["queue_type"] != "Remote":
            continue
        where = f"{r['queue_manager']}/{r['queue_name']}"
        rqm, rq, xq = r.get("remote_queue_manager", ""), r.get("remote_queue_name", ""), r.get("xmit_queue_name", "")
        if not rqm or not rq:
            bad.append(f"{where}: remote queue manager or remote queue name is empty")
        elif rqm == r["queue_manager"]:
            bad.append(f"{where}: points at its own queue manager")
        elif rqm in hosting and (rqm, rq) not in local:
            bad.append(f"{where}: no local queue {rq} on {rqm}")
        elif rqm not in hosting and rqm not in gateways:
            bad.append(f"{where}: {rqm} is not a queue manager in the target")
        if not xq or (r["queue_manager"], xq) not in xmit:
            bad.append(f"{where}: transmission queue '{xq}' is not defined on {r['queue_manager']}")
        elif xq != rqm:
            bad.append(f"{where}: transmission queue {xq} does not lead to {rqm}")
    check("S2", "Producers reach other queue managers through a remote queue and a transmission queue", bad)

    # S3 -------------------------------------------------------------------
    bad = []
    for r in rows:
        if r.get("app_role") != "Consumer":
            continue
        where = f"{r['app_id']} on {r['queue_manager']}/{r['queue_name']}"
        if r["queue_type"] == "Remote":
            bad.append(f"{where}: a consumer cannot read from a remote queue")
        elif r["queue_type"] == "Alias" and (r["queue_manager"], r.get("alias_target_queue", "")) not in local:
            bad.append(f"{where}: alias does not resolve to a local queue on the same queue manager")
    check("S3", "Consumers read from a local queue on their own queue manager", bad)

    # S4 -------------------------------------------------------------------
    bad = []
    template = config.target.channel_template
    have = Counter((c["channel_name"], c["channel_type"], c["queue_manager"]) for c in channels)
    bad += [f"channel {k[0]} ({k[1]}) is defined {n} times on {k[2]}" for k, n in sorted(have.items()) if n > 1]
    need = set()
    for src, dst in sorted(tgt.links):
        name = template.format(src=src, dst=dst)
        need |= {(name, "Sender", src), (name, "Receiver", dst)}
        if (name, "Sender", src) not in have:
            bad.append(f"no sender channel {name} on {src}")
        if (name, "Receiver", dst) not in have:
            bad.append(f"no receiver channel {name} on {dst}")
        if (src, dst) not in xmit:
            bad.append(f"no transmission queue {dst} on {src}")
    bad += [f"channel {k[0]} ({k[1]}) on {k[2]} serves no remote queue" for k in sorted(set(have) - need)]
    bad += [f"transmission queue {q} on {m} serves no remote queue" for m, q in sorted(xmit - tgt.links)]
    check("S4", "Queue managers are joined by sender/receiver channel pairs with deterministic names", bad)

    # S5 -------------------------------------------------------------------
    a_in, a_out, a_ext = _flow_counts(as_is)
    t_in, t_out, t_ext = _flow_counts(tgt)
    bad = [f"{p} -> {c}: {n} queue(s) as-is, {t_in.get((p, c), 0)} in the target"
           for (p, c), n in sorted(a_in.items()) if t_in.get((p, c), 0) < n]
    bad += [f"{p} -> external partner: {n} as-is, {t_out.get(p, 0)} in the target"
            for p, n in sorted(a_out.items()) if t_out.get(p, 0) < n]
    bad += [f"external partner -> {c}: {n} as-is, {t_ext.get(c, 0)} in the target"
            for c, n in sorted(a_ext.items()) if t_ext.get(c, 0) < n]
    check("S5", "Every flow the inventory proves is still deliverable", bad)

    # object hygiene -----------------------------------------------------------
    bad = list(tgt.issues.get("type_collisions", [])) + \
        [f"alias {x} has no target" for x in tgt.issues.get("dangling_aliases", [])]
    check("names", "No queue name is defined twice with different types on one queue manager", bad)
    if config.target.eliminate_aliases:
        bad = [f"{r['queue_manager']}/{r['queue_name']}" for r in rows if r["queue_type"] == "Alias"]
        check("aliases", "No alias queues remain", bad)

    # hard policies ------------------------------------------------------------
    queues = tgt.queues_per_qm()
    for res in evaluate(comp, tgt.qm_apps, queues, tgt.app_qms):
        if res.mode != "hard":
            continue
        problems = list(res.offenders) if res.violation > 0 else []
        if res.type == "capacity" and problems:
            pol = next(p for p in comp.capacity if p.id == res.id)
            # one app (or one co-located group) too big for the queue limit cannot be split further
            problems = [q for q in problems
                        if (pol.max_apps_per_qm and len(tgt.qm_apps.get(q, ())) > pol.max_apps_per_qm)
                        or len(tgt.qm_apps.get(q, ())) > 1 and not _single_group(comp, tgt.qm_apps[q])]
        check(f"policy:{res.id}", f"Hard policy '{res.id}' ({res.type}) holds",
              [f"{x} ({res.detail})" for x in problems])

    return {"passed": all(c["passed"] for c in checks), "checks": checks}


def _single_group(comp: Compiled, apps: set[str]) -> bool:
    return any(p.mode == "hard" and apps <= sel for p, sel in comp.colocate + comp.pins)
