"""Queue-manager-level graphs for the UI.

Applications are aggregated into their queue manager, so the picture stays a few
hundred nodes however many applications the estate has. Positions are computed
here once, so the browser only has to draw.
"""
from __future__ import annotations

import math
from collections import Counter
from typing import Optional

import networkx as nx

from .model import Model


def qm_graph(model: Model, seed: int = 42, init: Optional[dict] = None, status: Optional[dict] = None) -> dict:
    weight: Counter = Counter()
    for p in model.paths:
        if p.producer_qm and p.consumer_qm and p.producer_qm != p.consumer_qm:
            weight[(p.producer_qm, p.consumer_qm)] += 1
    for a, b in model.links:
        weight.setdefault((a, b), 1)
    pairs: dict[tuple[str, str], list[int]] = {}
    for (a, b), n in weight.items():
        k = (a, b) if a < b else (b, a)
        pairs.setdefault(k, [0, 0])[0 if (a, b) == k else 1] += n

    queues = model.queues_per_qm()
    nodes = sorted(model.qms)
    g = nx.Graph()
    g.add_nodes_from(nodes)
    for (a, b), (f, r) in pairs.items():
        g.add_edge(a, b, weight=math.log1p(f + r))
    pos = _layout(g, seed, init)

    out_nodes = []
    for q in nodes:
        apps = model.qm_apps.get(q, set())
        attrs = {}
        for name in model.attr_names:
            cnt = Counter(model.apps[a]["attrs"].get(name) or "(blank)" for a in apps)
            if cnt and len(cnt) <= 60:
                attrs[name] = sorted(cnt.items(), key=lambda kv: (-kv[1], kv[0]))[:4]
        out_nodes.append({
            "id": q, "apps": len(apps), "queues": queues.get(q, 0),
            "x": round(pos[q][0], 4), "y": round(pos[q][1], 4),
            "kind": "internal" if apps else "gateway",
            "status": (status or {}).get(q, ""), "attrs": attrs,
        })
    edges = [{"s": a, "t": b, "w": f + r, "fwd": f, "rev": r} for (a, b), (f, r) in sorted(pairs.items())]
    return {"nodes": out_nodes, "edges": edges}


def _layout(g: nx.Graph, seed: int, init: Optional[dict]) -> dict:
    n = g.number_of_nodes()
    if n == 0:
        return {}
    if n == 1:
        return {next(iter(g.nodes)): (0.5, 0.5)}
    start = None
    if init:
        start = {q: init[q] for q in g.nodes if q in init}
        if len(start) < 2:
            start = None
        else:                       # nodes without a previous position start near the centre
            for i, q in enumerate(sorted(set(g.nodes) - set(start))):
                ang = 2 * math.pi * i / max(1, n - len(start))
                start[q] = (0.5 + 0.05 * math.cos(ang), 0.5 + 0.05 * math.sin(ang))
    pos = nx.spring_layout(g, weight="weight", seed=seed, pos=start, k=1.6 / math.sqrt(n),
                           iterations=60 if n <= 600 else 30)
    xs = [p[0] for p in pos.values()]
    ys = [p[1] for p in pos.values()]
    w, h = (max(xs) - min(xs)) or 1.0, (max(ys) - min(ys)) or 1.0
    return {q: ((p[0] - min(xs)) / w, (p[1] - min(ys)) / h) for q, p in pos.items()}
