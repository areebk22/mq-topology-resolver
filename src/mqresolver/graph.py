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

KK_MAX = 160

from .model import Model


def qm_graph(model: Model, seed: int = 42, status: Optional[dict] = None) -> dict:
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
    pos = _layout(g, seed)

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


def _component_layout(sub: nx.Graph, seed: int) -> dict:
    """Unit-square positions for one connected component."""
    m = sub.number_of_nodes()
    nodes = sorted(sub.nodes)
    if m == 1:
        return {nodes[0]: (0.5, 0.5)}
    if m == 2:
        return {nodes[0]: (0.0, 0.5), nodes[1]: (1.0, 0.5)}
    if m <= KK_MAX:
        pos = nx.kamada_kawai_layout(sub, weight=None)          # even spacing for small, dense groups
    else:
        pos = nx.spring_layout(sub, weight="weight", seed=seed, k=1.8 / math.sqrt(m),
                               iterations=60 if m <= 600 else 35)
    xs = [p[0] for p in pos.values()]
    ys = [p[1] for p in pos.values()]
    side = max(max(xs) - min(xs), max(ys) - min(ys)) or 1.0
    return {q: ((p[0] - min(xs)) / side, (p[1] - min(ys)) / side) for q, p in pos.items()}


def _layout(g: nx.Graph, seed: int) -> dict:
    """Lay out each connected component on its own, then pack them, isolated QMs last in a grid.

    Laying out the whole graph at once lets a few unconnected queue managers fly to the
    corners and squeeze the real estate into the middle; packing avoids that.
    """
    if g.number_of_nodes() == 0:
        return {}
    comps = sorted(nx.connected_components(g), key=lambda c: (-len(c), min(c)))
    blocks = []                                                  # (width, height, {node: (x, y)})
    singles = sorted(x for c in comps if len(c) == 1 for x in c)
    for c in comps:
        if len(c) == 1:
            continue
        side = 1.4 * math.sqrt(len(c))
        pos = _component_layout(g.subgraph(c), seed)
        w = max(p[0] for p in pos.values()) * side or side
        h = max(p[1] for p in pos.values()) * side or side
        blocks.append((w, h, {q: (x * side, y * side) for q, (x, y) in pos.items()}))
    if singles:
        cols = max(1, math.ceil(math.sqrt(len(singles)) * 1.6))
        rows = math.ceil(len(singles) / cols)
        grid = {q: (0.9 * (i % cols), 0.9 * (i // cols)) for i, q in enumerate(singles)}
        blocks.append((0.9 * max(cols - 1, 0), 0.9 * max(rows - 1, 0), grid))
    # shelf packing, largest block first
    width = max(max(b[0] for b in blocks), math.sqrt(sum((b[0] + 1) * (b[1] + 1) for b in blocks)) * 1.15)
    out: dict = {}
    x = y = shelf = 0.0
    gap = 1.2
    for w, h, pos in blocks:
        if x > 0 and x + w > width:
            x, y, shelf = 0.0, y + shelf + gap, 0.0
        for q, (px, py) in pos.items():
            out[q] = (x + px, y + py)
        x += w + gap
        shelf = max(shelf, h)
    xs = [p[0] for p in out.values()]
    ys = [p[1] for p in out.values()]
    span = max(max(xs) - min(xs), max(ys) - min(ys)) or 1.0
    ox = (span - (max(xs) - min(xs))) / 2
    oy = (span - (max(ys) - min(ys))) / 2
    return {q: ((p[0] - min(xs) + ox) / span, (p[1] - min(ys) + oy) / span) for q, p in out.items()}
