"""Topology Complexity Index (TCI). See docs/SCORING.md.

One formula scores the as-is model, the generated target, the sanity anchors,
and (through `ScoreInputs`) every candidate the optimiser considers.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Iterable, Optional

from .config import PROFILES
from .model import Model

MAX_HOPS = 7          # alias + remote queue + transmission queue + sender + receiver + local queue + alias
CROSS_HOPS = 5        # remote queue, transmission queue, sender, receiver, local queue
SIMPLICITY = ("routing_depth", "app_sprawl", "channel_load", "object_overhead", "qm_footprint")


@dataclass
class ScoreInputs:
    n_apps: int               # applications
    n_connections: int        # (app, QM) connections
    path_weight: float        # delivery paths
    hop_sum: float            # MQ objects crossed, summed over paths
    qm_pairs: int             # QM pairs joined by channels
    comm_pairs: int           # distinct app pairs that communicate
    logical_queues: int
    objects: int              # queue definitions + transmission queues + channel definitions required
    hhi: float                # sum of squared endpoint shares per QM
    policy: float             # weighted policy violation, 0..1
    n_qms: int = 0            # queue managers hosting applications


def dimensions(si: ScoreInputs) -> dict[str, float]:
    mean_hops = si.hop_sum / si.path_weight if si.path_weight else 1.0
    return {
        "routing_depth": min(1.0, max(0.0, (mean_hops - 1) / (MAX_HOPS - 1))),
        "app_sprawl": 1 - si.n_apps / si.n_connections if si.n_connections else 0.0,
        "channel_load": min(1.0, si.qm_pairs / si.comm_pairs) if si.comm_pairs else 0.0,
        "object_overhead": max(0.0, 1 - si.logical_queues / si.objects) if si.objects else 0.0,
        "qm_footprint": min(1.0, si.n_qms / si.n_apps) if si.n_apps else 0.0,
        "blast_radius": min(1.0, si.hhi),
        "policy_violations": min(1.0, max(0.0, si.policy)),
    }


def tci(dims: dict[str, float], weights: dict[str, float]) -> float:
    simplicity = sum(dims[k] for k in SIMPLICITY) / len(SIMPLICITY)
    return 100 * (weights["simplicity"] * simplicity + weights["resilience"] * dims["blast_radius"]
                  + weights["policy"] * dims["policy_violations"])


def tci_of(si: ScoreInputs, weights: dict[str, float]) -> float:
    return tci(dimensions(si), weights)


def _hhi(loads: Iterable[float]) -> float:
    loads = list(loads)
    total = sum(loads)
    return sum((x / total) ** 2 for x in loads) if total else 0.0


def inputs_from_model(model: Model, policy_violation: float) -> ScoreInputs:
    load: Counter = Counter()
    for p in model.paths:
        if p.producer_qm:
            load[p.producer_qm] += 1
        if p.consumer_qm:
            load[p.consumer_qm] += 1
    pairs = {tuple(sorted(l)) for l in model.links}
    comm = {tuple(sorted((p, c))) for (p, c) in model.flow_weights() if p != c}
    return ScoreInputs(
        n_apps=len(model.apps),
        n_connections=sum(len(q) for q in model.app_qms.values()),
        path_weight=len(model.paths),
        hop_sum=sum(p.hops for p in model.paths),
        qm_pairs=len(pairs),
        comm_pairs=len(comm),
        logical_queues=len(model.queues),
        objects=len(model.definitions) + 3 * len(model.links),
        hhi=_hhi(load.values()),
        policy=policy_violation,
        n_qms=len(model.qm_apps),
    )


@dataclass
class FlowBase:
    """What an app -> QM assignment needs to be scored, extracted once from the as-is model."""
    apps: list[str]
    pair_paths: dict[tuple[str, str], int]      # (producer, consumer) -> queues, internal, may be self
    ext_out: dict[str, Counter]                 # producer -> {gateway QM: queues}
    ext_in: dict[str, int]                      # consumer -> queues fed by external producers
    logical_queues: int
    comm_pairs: int
    app_queue_weight: dict[str, int]            # upper bound of queue definitions an app brings to its QM
    co_consumers: dict[tuple[str, str], int]    # app pairs reading the same queue


def flow_base(model: Model) -> FlowBase:
    pair: Counter = Counter()
    ext_out: dict[str, Counter] = {}
    ext_in: Counter = Counter()
    qweight: Counter = Counter()
    co: Counter = Counter()
    for key, lq in model.queues.items():
        prods = sorted(set(lq.producers) | lq.inferred_producers)
        cons = sorted(set(lq.consumers) | lq.inferred_consumers)
        for a in set(prods) | set(cons) | lq.others:
            qweight[a] += 1
        for p in prods:
            for c in cons:
                pair[(p, c)] += 1
            if not cons and lq.ext_consumers:
                ext_out.setdefault(p, Counter())[lq.home_qm] += 1
        if not prods and lq.ext_producers:
            for c in cons:
                ext_in[c] += 1
        for i, a in enumerate(cons):
            for b in cons[i + 1:]:
                co[(a, b)] += 1
    comm = {tuple(sorted(k)) for k in pair if k[0] != k[1]}
    return FlowBase(apps=sorted(model.apps), pair_paths=dict(pair), ext_out=ext_out, ext_in=dict(ext_in),
                    logical_queues=len(model.queues), comm_pairs=len(comm),
                    app_queue_weight=dict(qweight), co_consumers=dict(co))


def inputs_from_assignment(base: FlowBase, assign: dict[str, str], policy_violation: float = 0.0,
                           gateway_map: Optional[dict[str, str]] = None) -> ScoreInputs:
    """Score inputs of the target implied by an assignment (one QM per app, no aliases)."""
    gw = gateway_map or {}
    load: Counter = Counter()
    links: set = set()
    paths = hops = remote_defs = 0
    for (p, c), n in base.pair_paths.items():
        a, b = assign[p], assign[c]
        paths += n
        load[a] += n
        load[b] += n
        if a == b:
            hops += n
        else:
            hops += CROSS_HOPS * n
            remote_defs += n
            links.add((a, b))
    for p, gws in base.ext_out.items():
        for g, n in gws.items():
            g = gw.get(g, g)
            paths += n
            hops += CROSS_HOPS * n
            remote_defs += n
            load[assign[p]] += n
            load[g] += n
            links.add((assign[p], g))
    for c, n in base.ext_in.items():
        paths += n
        hops += n
        load[assign[c]] += n
    return ScoreInputs(
        n_apps=len(base.apps), n_connections=len(base.apps), path_weight=paths, hop_sum=hops,
        qm_pairs=len({tuple(sorted(l)) for l in links}), comm_pairs=base.comm_pairs,
        logical_queues=base.logical_queues, objects=base.logical_queues + remote_defs + 3 * len(links),
        hhi=_hhi(load.values()), policy=policy_violation, n_qms=len(set(assign.values())))


def sensitivity(si_before: ScoreInputs, si_after: ScoreInputs) -> list[dict]:
    """The same comparison under every profile, so the result does not hinge on one weighting."""
    out = []
    for name, w in PROFILES.items():
        b, a = tci_of(si_before, w), tci_of(si_after, w)
        out.append({"profile": name, "as_is": round(b, 2), "target": round(a, 2),
                    "change_pct": round(100 * (a - b) / b, 1) if b else 0.0})
    return out


def describe(si: ScoreInputs, weights: dict[str, float]) -> dict:
    dims = dimensions(si)
    mean_hops = si.hop_sum / si.path_weight if si.path_weight else 1.0
    return {
        "tci": round(tci(dims, weights), 2),
        "dimensions": {k: round(v, 4) for k, v in dims.items()},
        "raw": {
            "mean_objects_per_message_path": round(mean_hops, 2),
            "qms_per_app": round(si.n_connections / si.n_apps, 2) if si.n_apps else 0,
            "qm_pairs_with_channels": si.qm_pairs,
            "communicating_app_pairs": si.comm_pairs,
            "logical_queues": si.logical_queues,
            "managed_objects": si.objects,
            "queue_managers": si.n_qms,
            "effective_qms": round(1 / si.hhi, 1) if si.hhi else 0,
            "policy_violation": round(si.policy, 4),
        },
    }
