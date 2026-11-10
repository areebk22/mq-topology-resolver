"""App -> queue manager assignment.

Louvain community detection is the core. Around it: hard policies are applied
before clustering, oversized communities are split, stragglers are packed, and a
local search moves apps and merges QMs while the Topology Complexity Index falls.
The objective is the same index that is reported (see score.py).
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Optional

import networkx as nx

from .config import Config
from .model import Model
from .policy import Compiled, PolicyError, compile_policies
from .score import CROSS_HOPS, FlowBase, ScoreInputs, tci_of

EPS = 1e-9


@dataclass
class Unit:
    """Apps that move together: one app, or a hard co-located group."""
    id: int
    apps: list[str]
    qweight: int = 0
    fixed: Optional[str] = None      # QM id this unit can never leave (isolated or hard-pinned)
    closed: bool = False             # nothing else may join its QM
    reason: str = "clustered with the apps it exchanges most messages with"

    @property
    def size(self) -> int:
        return len(self.apps)


@dataclass
class Result:
    assignment: dict[str, str]                 # app -> target QM name
    reasons: dict[str, str]
    inputs: ScoreInputs
    score: float
    candidates: list[dict] = field(default_factory=list)
    reused_qms: list[str] = field(default_factory=list)
    new_qms: list[str] = field(default_factory=list)
    gateway_map: dict[str, str] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


class State:
    """Assignment of units to QMs with incrementally maintained score inputs."""

    def __init__(self, base: FlowBase, comp: Compiled, units: list[Unit], weights: dict[str, float],
                 gateway_map: dict[str, str]):
        self.base, self.comp, self.units, self.weights = base, comp, units, weights
        self.n_apps = len(base.apps)
        unit_of = {a: u.id for u in units for a in u.apps}
        self.unit_of = unit_of

        # ---- static flow structure between units ---------------------------
        between: dict[int, dict[int, list[int]]] = defaultdict(lambda: defaultdict(lambda: [0, 0]))
        self.paths_total = 0
        for (p, c), n in base.pair_paths.items():
            self.paths_total += n
            up, uc = unit_of[p], unit_of[c]
            if up != uc:
                between[up][uc][0] += n
                between[uc][up][1] += n
        self.adj: list[list[tuple[int, int, int]]] = [
            sorted((v, w[0], w[1]) for v, w in between[u.id].items()) for u in units]
        self.ext_out: list[list[tuple[str, int]]] = [[] for _ in units]
        self.ext_out_total = 0
        gw_load: Counter = Counter()
        self.uload = [0] * len(units)
        for (p, c), n in base.pair_paths.items():
            self.uload[unit_of[p]] += n
            self.uload[unit_of[c]] += n
        for p, gws in base.ext_out.items():
            merged: Counter = Counter()
            for g, n in gws.items():
                merged[gateway_map.get(g, g)] += n
            for g, n in sorted(merged.items()):
                self.ext_out[unit_of[p]].append((g, n))
                self.ext_out_total += n
                gw_load[g] += n
                self.uload[unit_of[p]] += n
        for c, n in base.ext_in.items():
            self.paths_total += n
            self.uload[unit_of[c]] += n
        self.paths_total += self.ext_out_total
        self.total_load = sum(self.uload) + sum(gw_load.values())
        self.gateways = set(gw_load)

        # ---- policies ------------------------------------------------------
        self.max_apps = comp.limit("max_apps_per_qm")
        self.max_queues = comp.limit("max_queues_per_qm")
        self.rules = comp.label_rules
        self.rule_hard = [r.policy.mode == "hard" for r in self.rules]
        self.ulabels: list[list[Counter]] = [
            [Counter(r.labels[a] for a in u.apps if r.labels.get(a) is not None) for r in self.rules]
            for u in units]
        self.soft_iso = [(p, sel) for p, sel in comp.isolate if p.mode == "soft"]
        self.uiso = [[sum(1 for a in u.apps if a in sel) for _, sel in self.soft_iso] for u in units]
        self.soft_cap = [p for p in comp.capacity if p.mode == "soft"]
        self.soft_groups = [(p, sel, "colocate") for p, sel in comp.colocate if p.mode == "soft"] + \
                           [(p, sel, "pin") for p, sel in comp.pins if p.mode == "soft"]
        self.ugroup = [[sum(1 for a in u.apps if a in sel) for _, sel, _ in self.soft_groups] for u in units]
        self.group_on: list[Counter] = [Counter() for _ in self.soft_groups]
        self.soft_count = [p for p in comp.qm_count if p.mode == "soft"]
        self.weight_sum = sum(p.weight for p in comp.policies)
        self.total_qweight = max(sum(u.qweight for u in units), 1)

        # ---- dynamic state -------------------------------------------------
        self.qm_of: list[Optional[str]] = [None] * len(units)
        self.members: dict[str, set[int]] = defaultdict(set)
        self.size: Counter = Counter()
        self.qw: Counter = Counter()
        self.load: Counter = Counter(gw_load)
        self.sumsq = float(sum(v * v for v in gw_load.values()))
        self.lab: list[dict[str, Counter]] = [defaultdict(Counter) for _ in self.rules]
        self.iso_on: list[Counter] = [Counter() for _ in self.soft_iso]
        self.link: Counter = Counter()
        self.pair: Counter = Counter()
        self.nlinks = self.npairs = self.cross = 0
        self.n_hosting = 0
        self.closed: set[str] = set()
        self.contrib: dict[str, list[float]] = {}
        self.totals = [0.0] * (len(self.rules) + len(self.soft_iso) + 2 * len(self.soft_cap))

    # ---- link bookkeeping --------------------------------------------------
    def _link(self, a: str, b: str, n: int) -> None:
        if n == 0:
            return
        k = (a, b)
        old = self.link[k]
        new = old + n
        if new:
            self.link[k] = new
        else:
            del self.link[k]
        self.nlinks += (new > 0) - (old > 0)
        pk = (a, b) if a < b else (b, a)
        pold = self.pair[pk]
        pnew = pold + n
        if pnew:
            self.pair[pk] = pnew
        else:
            del self.pair[pk]
        self.npairs += (pnew > 0) - (pold > 0)

    def _qm_contrib(self, q: str) -> list[float]:
        out = []
        size = self.size[q]
        for i, r in enumerate(self.rules):
            if self.rule_hard[i]:
                out.append(0.0)
                continue
            cnt = self.lab[i].get(q)
            if not cnt or len(cnt) <= r.max_distinct:
                out.append(0.0)
            else:
                vals = sorted(cnt.values(), reverse=True)
                out.append(float(sum(vals[r.max_distinct:])))
        for i in range(len(self.soft_iso)):
            out.append(float(self.iso_on[i][q]) if size > 1 else 0.0)
        for p in self.soft_cap:
            out.append(float(max(0, size - p.max_apps_per_qm)) if p.max_apps_per_qm else 0.0)
            out.append(float(max(0, self.qw[q] - p.max_queues_per_qm))
                       if p.max_queues_per_qm and len(self.members[q]) > 1 else 0.0)
        return out

    def _refresh(self, q: str) -> None:
        if not self.totals:
            return
        old = self.contrib.get(q)
        new = self._qm_contrib(q)
        for i, v in enumerate(new):
            self.totals[i] += v - (old[i] if old else 0.0)
        self.contrib[q] = new

    def _shift(self, u: int, q: str, sign: int) -> None:
        unit = self.units[u]
        if sign > 0:
            if not self.members[q]:
                self.n_hosting += 1
            self.members[q].add(u)
        else:
            self.members[q].discard(u)
            if not self.members[q]:
                self.n_hosting -= 1
        self.size[q] += sign * unit.size
        self.qw[q] += sign * unit.qweight
        old = self.load[q]
        new = old + sign * self.uload[u]
        self.load[q] = new
        self.sumsq += new * new - old * old
        for i, cnt in enumerate(self.ulabels[u]):
            tgt = self.lab[i][q]
            for lab, n in cnt.items():
                tgt[lab] += sign * n
                if not tgt[lab]:
                    del tgt[lab]
        for i, n in enumerate(self.uiso[u]):
            if n:
                self.iso_on[i][q] += sign * n
        for i, n in enumerate(self.ugroup[u]):
            if n:
                self.group_on[i][q] += sign * n
                if not self.group_on[i][q]:
                    del self.group_on[i][q]
        for v, n_out, n_in in self.adj[u]:
            qv = self.qm_of[v]
            if qv is None or qv == q:
                continue
            self._link(q, qv, sign * n_out)
            self._link(qv, q, sign * n_in)
            self.cross += sign * (n_out + n_in)
        for g, n in self.ext_out[u]:
            self._link(q, g, sign * n)

    def place(self, u: int, q: str) -> None:
        old = self.qm_of[u]
        if old == q:
            return
        if old is not None:
            self._shift(u, old, -1)
            self.qm_of[u] = None
        self._shift(u, q, +1)
        self.qm_of[u] = q
        if old is not None:
            self._refresh(old)
        self._refresh(q)

    # ---- feasibility ---------------------------------------------------------
    def can_place(self, u: int, q: str, extra_size: int = 0, extra_qw: int = 0,
                  extra_labels: Optional[list[Counter]] = None) -> bool:
        unit = self.units[u]
        if unit.fixed is not None or q in self.closed or q in self.gateways:
            return False
        size, qw = self.size[q] + extra_size, self.qw[q] + extra_qw
        if size:
            if self.max_apps and size + unit.size > self.max_apps:
                return False
            if self.max_queues and qw + unit.qweight > self.max_queues:
                return False
        for i, r in enumerate(self.rules):
            if not self.rule_hard[i]:
                continue
            have = set(self.lab[i].get(q, ()))
            if extra_labels:
                have |= set(extra_labels[i])
            if len(have | set(self.ulabels[u][i])) > r.max_distinct:
                return False
        return True

    def can_merge(self, a: str, b: str) -> bool:
        """May every unit on `a` move to `b`?"""
        if a == b or a in self.closed or b in self.closed or a in self.gateways or b in self.gateways:
            return False
        if any(self.units[u].fixed is not None for u in self.members[a]):
            return False
        if self.max_apps and self.size[a] + self.size[b] > self.max_apps:
            return False
        if self.max_queues and self.qw[a] + self.qw[b] > self.max_queues:
            return False
        for i, r in enumerate(self.rules):
            if self.rule_hard[i] and len(set(self.lab[i].get(a, ())) | set(self.lab[i].get(b, ()))) > r.max_distinct:
                return False
        return True

    # ---- score -----------------------------------------------------------------
    def policy_violation(self) -> float:
        if not self.weight_sum:
            return 0.0
        acc = 0.0
        k = 0
        for i, r in enumerate(self.rules):
            acc += r.policy.weight * min(1.0, self.totals[k] / max(self.n_apps, 1))
            k += 1
        for p, sel in self.soft_iso:
            acc += p.weight * (min(1.0, self.totals[k] / len(sel)) if sel else 0.0)
            k += 1
        for p in self.soft_cap:
            frac = max(self.totals[k] / max(self.n_apps, 1), self.totals[k + 1] / self.total_qweight)
            acc += p.weight * min(1.0, frac)
            k += 2
        for i, (p, sel, kind) in enumerate(self.soft_groups):
            if not sel:
                continue
            on = self.group_on[i]
            ok = on.get(p.qm, 0) if kind == "pin" else (max(on.values()) if on else 0)
            acc += p.weight * (len(sel) - ok) / len(sel)
        for p in self.soft_count:
            n = self.n_hosting
            over = max(0, n - p.max) / p.max if p.max else 0.0
            under = max(0, p.min - n) / p.min if p.min else 0.0
            acc += p.weight * min(1.0, max(over, under))
        return acc / self.weight_sum

    def inputs(self) -> ScoreInputs:
        cross = self.cross + self.ext_out_total
        return ScoreInputs(
            n_apps=self.n_apps, n_connections=self.n_apps, path_weight=self.paths_total,
            hop_sum=self.paths_total + (CROSS_HOPS - 1) * cross, qm_pairs=self.npairs,
            comm_pairs=self.base.comm_pairs, logical_queues=self.base.logical_queues,
            objects=self.base.logical_queues + cross + 3 * self.nlinks,
            hhi=self.sumsq / (self.total_load ** 2) if self.total_load else 0.0,
            policy=self.policy_violation(), n_qms=self.n_hosting)

    def score(self) -> float:
        return tci_of(self.inputs(), self.weights)

    def assignment(self) -> dict[int, str]:
        return {u.id: self.qm_of[u.id] for u in self.units}


# ----------------------------------------------------------------------------
def build_units(model: Model, base: FlowBase, comp: Compiled) -> list[Unit]:
    parent = {a: a for a in base.apps}

    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    for p, sel in comp.colocate:
        if p.mode == "hard" and len(sel) > 1:
            members = sorted(sel)
            for a in members[1:]:
                parent[find(a)] = find(members[0])
    groups: dict[str, list[str]] = defaultdict(list)
    for a in base.apps:
        groups[find(a)].append(a)
    iso: dict[str, str] = {}
    for p, sel in comp.isolate:
        if p.mode == "hard":
            for a in sel:
                iso.setdefault(a, p.id)
    pin: dict[str, tuple[str, str]] = {}
    for p, sel in comp.pins:
        if p.mode == "hard":
            for a in sel:
                pin[a] = (p.qm, p.id)
    colo = {a: p.id for p, sel in comp.colocate if p.mode == "hard" and len(sel) > 1 for a in sel}
    units = []
    for i, root in enumerate(sorted(groups)):
        apps = sorted(groups[root])
        u = Unit(i, apps, qweight=sum(base.app_queue_weight.get(a, 0) for a in apps))
        pinned = sorted({pin[a] for a in apps if a in pin})
        if len(apps) > 1:
            u.reason = f"kept together by policy '{colo[apps[0]]}'"
        if pinned:
            u.fixed = pinned[0][0]
            u.reason = f"pinned to {pinned[0][0]} by policy '{pinned[0][1]}'"
        if apps[0] in iso:
            u.fixed = u.fixed or f"~iso:{apps[0]}"
            u.closed = True
            u.reason = f"dedicated queue manager required by policy '{iso[apps[0]]}'"
        units.append(u)
    return units


def _graph(state: State, base: FlowBase, units: list[int]) -> nx.Graph:
    inside = set(units)
    g = nx.Graph()
    g.add_nodes_from(sorted(units))
    for u in sorted(units):
        for v, n_out, n_in in state.adj[u]:
            if v in inside and u < v:
                g.add_edge(u, v, weight=n_out + n_in)
    for (a, b), n in base.co_consumers.items():      # apps reading one queue should stay together
        ua, ub = state.unit_of[a], state.unit_of[b]
        if ua != ub and ua in inside and ub in inside:
            w = g.get_edge_data(ua, ub, {"weight": 0})["weight"]
            g.add_edge(ua, ub, weight=w + 3 * n)
    return g


def _communities(g: nx.Graph, resolution: float, seed: int) -> list[list[int]]:
    if g.number_of_edges() == 0:
        return [[n] for n in sorted(g.nodes)]
    cs = nx.community.louvain_communities(g, weight="weight", resolution=resolution, seed=seed)
    return sorted((sorted(c) for c in cs), key=lambda c: c[0])


def _split(state: State, base: FlowBase, nodes: list[int], resolution: float, seed: int, depth: int = 0):
    """Split a community until it fits the hard capacity limits."""
    size = sum(state.units[u].size for u in nodes)
    qw = sum(state.units[u].qweight for u in nodes)
    fits = (not state.max_apps or size <= state.max_apps) and (not state.max_queues or qw <= state.max_queues)
    if fits or len(nodes) == 1:
        return [nodes]
    parts = _communities(_graph(state, base, nodes), resolution * 2, seed) if depth < 8 else [nodes]
    if len(parts) == 1:                                  # no structure left: balanced chunks
        parts, cur, cs, cq = [], [], 0, 0
        for u in sorted(nodes, key=lambda x: (-state.units[x].size, x)):
            us, uq = state.units[u].size, state.units[u].qweight
            if cur and ((state.max_apps and cs + us > state.max_apps) or (state.max_queues and cq + uq > state.max_queues)):
                parts.append(cur)
                cur, cs, cq = [], 0, 0
            cur.append(u)
            cs, cq = cs + us, cq + uq
        parts.append(cur)
        return parts
    out = []
    for part in parts:
        out.extend(_split(state, base, part, resolution * 2, seed, depth + 1))
    return out


def _block_key(state: State, u: int) -> tuple:
    return tuple(tuple(sorted(state.ulabels[u][i])) for i, hard in enumerate(state.rule_hard) if hard)


def _initial(state: State, base: FlowBase, resolution: float, seed: int) -> None:
    free = []
    for u in state.units:
        if u.fixed is not None:
            state.place(u.id, u.fixed)
            if u.closed:
                state.closed.add(u.fixed)
        else:
            free.append(u.id)
    blocks: dict[tuple, list[int]] = defaultdict(list)
    for u in free:
        blocks[_block_key(state, u)].append(u)
    n = 0
    for key in sorted(blocks, key=repr):
        for comm in _communities(_graph(state, base, blocks[key]), resolution, seed):
            for part in _split(state, base, comm, resolution, seed):
                n += 1
                for u in part:
                    state.place(u, f"~g{n}")


def _pack(state: State) -> None:
    """Give stragglers a home: join the QM they talk to most, else share a pool QM."""
    small = sorted(q for q in state.members if state.members[q] and state.size[q] <= 2
                   and q not in state.closed and not any(state.units[u].fixed for u in state.members[q]))
    for q in small:
        for u in sorted(state.members[q]):
            want: Counter = Counter()
            for v, n_out, n_in in state.adj[u]:
                qv = state.qm_of[v]
                if qv != q:
                    want[qv] += n_out + n_in
            for cand, _ in sorted(want.items(), key=lambda kv: (-kv[1], kv[0])):
                if state.can_place(u, cand):
                    state.place(u, cand)
                    state.units[u].reason = "placed with the apps it exchanges messages with"
                    break
    lonely = sorted(q for q in state.members if state.members[q] and state.size[q] <= 2
                    and q not in state.closed and not any(state.units[u].fixed for u in state.members[q])
                    and not any(state.qm_of[v] != q for u in state.members[q] for v, _, _ in state.adj[u]))
    pools: dict[tuple, list[str]] = defaultdict(list)
    for q in lonely:
        for u in sorted(state.members[q]):
            key = _block_key(state, u)
            for pool in pools[key]:
                if state.can_place(u, pool):
                    state.place(u, pool)
                    break
            else:
                pools[key].append(state.qm_of[u])
            state.units[u].reason = "no flows to other apps, so it shares a queue manager with similar apps"


def _refine(state: State, passes: int, k: int) -> int:
    moves = 0
    for _ in range(passes):
        changed = False
        for u in range(len(state.units)):
            if state.units[u].fixed is not None or not state.adj[u]:
                continue
            q0 = state.qm_of[u]
            want: Counter = Counter()
            for v, n_out, n_in in state.adj[u]:
                qv = state.qm_of[v]
                if qv != q0:
                    want[qv] += n_out + n_in
            if not want:
                continue
            best_q, best_s = None, state.score() - EPS
            for cand, _ in sorted(want.items(), key=lambda kv: (-kv[1], kv[0]))[:k]:
                if not state.can_place(u, cand):
                    continue
                state.place(u, cand)
                s = state.score()
                if s < best_s:
                    best_q, best_s = cand, s
                state.place(u, q0)
            if best_q is not None:
                state.place(u, best_q)
                state.units[u].reason = "moved next to its main partners because that lowered the complexity score"
                moves += 1
                changed = True
        for (a, b), _ in sorted(state.pair.items(), key=lambda kv: (-kv[1], kv[0]))[:400]:
            if not state.members.get(a) or not state.members.get(b):
                continue
            src, dst = (a, b) if (state.size[a], a) <= (state.size[b], b) else (b, a)
            if not state.can_merge(src, dst):
                src, dst = dst, src
                if not state.can_merge(src, dst):
                    continue
            before = state.score()
            moved = sorted(state.members[src])
            for u in moved:
                state.place(u, dst)
            if state.score() < before - EPS:
                moves += len(moved)
                changed = True
            else:
                for u in moved:
                    state.place(u, src)
        if _consolidate(state):
            changed = True
        if not changed:
            break
    return moves


def _consolidate(state: State, tries: int = 6) -> bool:
    """Fold small QMs into compatible ones, linked or not, when that lowers the score."""
    changed = False
    order = sorted((q for q in state.members if state.members[q] and q not in state.closed),
                   key=lambda q: (state.size[q], q))
    for src in order[: len(order) // 2 + 1]:
        if not state.members.get(src):
            continue
        cands = []
        for dst in order:
            if dst != src and state.members.get(dst) and state.can_merge(src, dst):
                cands.append(dst)
                if len(cands) == tries:
                    break
        if not cands:
            continue
        moved = sorted(state.members[src])
        best_dst, best_s = None, state.score() - EPS
        for dst in cands:
            for u in moved:
                state.place(u, dst)
            s = state.score()
            if s < best_s:
                best_dst, best_s = dst, s
            for u in moved:
                state.place(u, src)
        if best_dst is not None:
            for u in moved:
                state.place(u, best_dst)
                if not state.adj[u]:
                    state.units[u].reason = "no flows to other apps, so it shares a queue manager with similar apps"
            changed = True
    return changed


def _enforce_qm_count(state: State, base: FlowBase, seed: int) -> None:
    hard = [p for p in state.comp.qm_count if p.mode == "hard"]
    if not hard:
        return
    maxes = [p.max for p in hard if p.max]
    hi = min(maxes) if maxes else None
    lo = max([p.min for p in hard if p.min] or [0])
    while hi and state.n_hosting > hi:
        best = None
        for src in sorted((q for q in state.members if state.members[q]), key=lambda q: (state.size[q], q)):
            for dst in sorted(q for q in state.members if state.members[q]):
                if not state.can_merge(src, dst):
                    continue
                moved = sorted(state.members[src])
                for u in moved:
                    state.place(u, dst)
                s = state.score()
                for u in moved:
                    state.place(u, src)
                if best is None or s < best[0]:
                    best = (s, src, dst)
            if best:
                break
        if not best and _dissolve_smallest(state):
            continue
        if not best:
            fixed = sum(1 for q in state.members if state.members[q] and (q in state.closed))
            raise PolicyError(
                f"cannot reach the hard limit of {hi} queue managers: {state.n_hosting} are needed "
                f"({fixed} are dedicated by isolate policies; the rest cannot merge without breaking "
                "capacity or grouping policies). Raise the limit or make one of those policies soft.")
        for u in sorted(state.members[best[1]]):
            state.place(u, best[2])
    n = 0
    while state.n_hosting < lo:
        cands = sorted((q for q in state.members if len(state.members[q]) > 1 and q not in state.closed),
                       key=lambda q: (-state.size[q], q))
        if not cands:
            raise PolicyError(f"cannot reach the hard minimum of {lo} queue managers: only "
                              f"{state.n_hosting} groups of apps can be formed.")
        q = cands[0]
        nodes = sorted(state.members[q])
        parts = _communities(_graph(state, base, nodes), 4.0, seed)
        if len(parts) < 2:
            parts = [nodes[: len(nodes) // 2], nodes[len(nodes) // 2:]]
        movable = [u for u in sorted(parts, key=len)[0] if state.units[u].fixed is None]
        if not movable or len(movable) == len(nodes):
            movable = [u for u in nodes if state.units[u].fixed is None][: max(1, len(nodes) // 2)]
            if not movable or len(movable) == len(nodes):
                raise PolicyError(f"cannot reach the hard minimum of {lo} queue managers.")
        n += 1
        for u in movable:
            state.place(u, f"~s{n}")


def _dissolve_smallest(state: State) -> bool:
    """Empty the smallest QM by re-homing its units one by one. Undo if any unit has nowhere to go."""
    open_qms = sorted((q for q in state.members if state.members[q] and q not in state.closed
                       and not any(state.units[u].fixed is not None for u in state.members[q])),
                      key=lambda q: (state.size[q], q))
    for src in open_qms[:5]:
        done = []
        for u in sorted(state.members[src], key=lambda x: (-state.units[x].size, x)):
            best = None
            for dst in sorted(q for q in state.members if state.members[q] and q != src):
                if state.can_place(u, dst):
                    state.place(u, dst)
                    s = state.score()
                    state.place(u, src)
                    if best is None or s < best[0]:
                        best = (s, dst)
            if best is None:
                break
            state.place(u, best[1])
            done.append(u)
        else:
            return True
        for u in done:
            state.place(u, src)
    return False


def _name_qms(state: State, model: Model, config: Config) -> tuple[dict[str, str], list[str], list[str]]:
    """Give every target QM a real name, reusing the existing QM its apps already use most."""
    ids = sorted(q for q in state.members if state.members[q])
    names: dict[str, str] = {q: q for q in ids if not q.startswith("~")}
    taken = set(names.values()) | state.gateways
    reused: list[str] = []
    if config.target.reuse_qm_names:
        overlap = []
        for q in ids:
            if q in names:
                continue
            cnt: Counter = Counter()
            for u in state.members[q]:
                for a in state.units[u].apps:
                    for old in model.app_qms.get(a, ()):
                        cnt[old] += 1
            overlap += [(-n, old, q) for old, n in cnt.items()]
        for _, old, q in sorted(overlap):
            if q not in names and old not in taken:
                names[q] = old
                taken.add(old)
                reused.append(old)
    existing = set(model.qms)
    created, n = [], 0
    for q in ids:
        if q in names:
            continue
        while True:
            n += 1
            name = config.target.new_qm_template.format(n=n)
            if name not in taken and name not in existing:
                break
        names[q] = name
        taken.add(name)
        created.append(name)
    return names, sorted(reused), created


def gateway_mapping(model: Model, config: Config) -> dict[str, str]:
    targets = config.target.gateway_qms
    if not targets:
        return {}
    return {g: targets[i % len(targets)] for i, g in enumerate(sorted(model.external_gateways()))}


def _run(base: FlowBase, model: Model, comp: Compiled, weights, gw, adv, res: float) -> State:
    st = State(base, comp, build_units(model, base, comp), weights, gw)
    _initial(st, base, res, adv.seed)
    return st


def _strict_variant(config: Config, model: Model) -> Optional[Compiled]:
    """The same policies with soft placement rules made hard. Used only to seed the search."""
    promotable = ("isolate", "homogeneous", "separate", "capacity")
    if not any(p.mode == "soft" and p.type in promotable for p in config.policies):
        return None
    strict = config.model_copy(deep=True)
    for p in strict.policies:
        if p.mode == "soft" and p.type in promotable:
            p.mode = "hard"
    try:
        return compile_policies(strict, model.apps, model.attr_names)
    except PolicyError:
        return None


def optimise(model: Model, base: FlowBase, comp: Compiled, config: Config) -> Result:
    weights = config.score_weights()
    gw = gateway_mapping(model, config)
    adv = config.advanced
    best: Optional[State] = None
    candidates = []
    strict = _strict_variant(config, model)
    for start in ("configured", "strict"):
        if start == "strict" and strict is None:
            continue
        for res in adv.resolutions:
            if start == "configured":
                st = _run(base, model, comp, weights, gw, adv, res)
            else:
                # start from the solution that obeys the soft rules fully, then let the score relax it
                seed = _run(base, model, strict, weights, gw, adv, res)
                _pack(seed)
                _refine(seed, adv.refine_passes, adv.candidates)
                home = {a: seed.qm_of[u.id] for u in seed.units for a in u.apps}
                st = State(base, comp, build_units(model, base, comp), weights, gw)
                for u in st.units:
                    st.place(u.id, u.fixed if u.fixed is not None else home[u.apps[0]])
                    if u.closed:
                        st.closed.add(u.fixed)
            begin = st.score()
            if start == "configured":
                _pack(st)
            moves = _refine(st, adv.refine_passes, adv.candidates)
            s = st.score()
            candidates.append({"start": start, "resolution": res, "after_clustering": round(begin, 3),
                               "final": round(s, 3), "queue_managers": st.n_hosting, "moves": moves})
            if best is None or s < best.score() - EPS:
                best = st
    assert best is not None
    _enforce_qm_count(best, base, adv.seed)
    names, reused, created = _name_qms(best, model, config)
    assignment, reasons = {}, {}
    for u in best.units:
        for a in u.apps:
            assignment[a] = names[best.qm_of[u.id]]
            reasons[a] = u.reason
    if len(assignment) != len(base.apps):
        raise AssertionError("every application must be assigned exactly one queue manager")
    notes = []
    if best.max_queues:
        over = sorted(names[q] for q in best.members if best.members[q] and best.qw[q] > best.max_queues)
        if over:
            notes.append(f"{len(over)} queue manager(s) hold a single app or group whose queues alone exceed "
                         f"the queue limit and cannot be split: {over[:10]}")
    if best.max_apps:
        over = sorted(names[q] for q in best.members if best.size[q] > best.max_apps)
        if over:
            notes.append(f"pinned or co-located groups exceed the app limit on: {over[:10]}")
    return Result(assignment=assignment, reasons=reasons, inputs=best.inputs(), score=best.score(),
                  candidates=candidates, reused_qms=reused, new_qms=created, gateway_map=gw, notes=notes)
