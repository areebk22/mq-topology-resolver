"""Topology model built from canonical inventory rows.

The same builder is used for the as-is inventory and for the generated target,
so both are measured and validated in exactly the same way.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Optional

import pandas as pd

HOPS = {"Local": 1, "Alias": 2, "Remote": 5}
EXTERNAL = None  # marker for an endpoint outside the inventory


@dataclass(slots=True)
class LogicalQueue:
    key: tuple[str, str]                    # (home QM, queue name): where messages land
    producers: dict[str, list[tuple[str, str]]] = field(default_factory=dict)  # app -> [(qm, type)]
    consumers: dict[str, list[tuple[str, str]]] = field(default_factory=dict)
    others: set[str] = field(default_factory=set)        # apps with an unknown role on this queue
    ext_producers: set[str] = field(default_factory=set)  # partner names not in the inventory
    ext_consumers: set[str] = field(default_factory=set)
    inferred_producers: set[str] = field(default_factory=set)  # known app, but no row of its own
    inferred_consumers: set[str] = field(default_factory=set)

    @property
    def name(self) -> str:
        return self.key[1]

    @property
    def home_qm(self) -> str:
        return self.key[0]


@dataclass(slots=True)
class Path:
    producer: Optional[str]
    consumer: Optional[str]
    producer_qm: Optional[str]
    consumer_qm: Optional[str]
    hops: int
    queue: tuple[str, str]
    verified: bool          # both ends have their own inventory row (or the missing end is external)


@dataclass
class Model:
    apps: dict[str, dict]                       # app_id -> {"name": str, "attrs": {col: value}}
    attr_names: list[str]
    queues: dict[tuple[str, str], LogicalQueue]
    paths: list[Path]
    app_qms: dict[str, set[str]]
    qm_apps: dict[str, set[str]]
    qms: set[str]                               # QMs that host definitions or are link targets
    links: set[tuple[str, str]]                 # directed QM -> QM, from remote queues
    definitions: dict[tuple[str, str], str]     # (qm, queue name) -> type, application queues only
    xmitqs: set[tuple[str, str]]                # inventoried transmission queues (qm, name)
    issues: dict[str, list]

    # ---- derived views -------------------------------------------------
    def flow_weights(self) -> dict[tuple[str, str], int]:
        """Directed app -> app weights: number of distinct queues carrying the flow."""
        seen: set = set()
        out: Counter = Counter()
        for p in self.paths:
            if p.producer is None or p.consumer is None:
                continue
            k = (p.producer, p.consumer, p.queue)
            if k not in seen:
                seen.add(k)
                out[(p.producer, p.consumer)] += 1
        return dict(out)

    def verified_flows(self) -> set[tuple]:
        """(producer, consumer, queue name) triples that the inventory proves deliverable."""
        return {(p.producer, p.consumer, p.queue[1]) for p in self.paths if p.verified}

    def queues_per_qm(self) -> dict[str, int]:
        c: Counter = Counter(qm for qm, _ in self.definitions)
        return dict(c)

    def external_gateways(self) -> set[str]:
        return {q for q in self.qms if q not in self.qm_apps}


def queue_key(r: dict) -> tuple[str, str]:
    """Where messages put to this row's queue really land: (QM, queue name)."""
    qm, qname, qtype = r["queue_manager"], r["queue_name"], r["queue_type"]
    if qtype == "Remote" and r.get("remote_queue_manager"):
        return (r["remote_queue_manager"], r.get("remote_queue_name") or qname)
    if qtype == "Alias":
        return (qm, r.get("alias_target_queue") or qname)
    return (qm, qname)


def _mode(values: list[str]) -> tuple[str, bool]:
    c = Counter(values)
    best = sorted(c.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
    return best, len(c) > 1


MODEL_COLUMNS = ["queue_name", "queue_type", "queue_manager", "app_id", "app_name", "app_role", "producer_app_name",
                 "consumer_app_name", "remote_queue_manager", "remote_queue_name", "alias_target_queue", "queue_usage"]


class Records:
    """Re-iterable rows of a frame as dicts, built one at a time.

    Equivalent to df.to_dict("records") for reading, without holding every row dict in memory.
    """

    def __init__(self, df: pd.DataFrame, cols: Optional[list[str]] = None, where=None):
        self.cols = list(cols if cols is not None else df.columns)
        self.data = [df[c].tolist() for c in self.cols]
        self.where = where

    def __iter__(self):
        cols = self.cols
        for t in zip(*self.data):
            r = dict(zip(cols, t))
            if self.where is None or self.where(r):
                yield r


def _modes(df: pd.DataFrame, col: str) -> tuple[dict[str, str], set[str]]:
    """Most frequent value of `col` per app (ties: smallest value), and the apps with more than one value."""
    if col not in df.columns:
        return {}, set()
    g = df.groupby(["app_id", col], sort=False).size().reset_index(name="__n")
    g = g.sort_values(["app_id", "__n", col], ascending=[True, False, True], kind="stable")
    modes = dict(zip(g.drop_duplicates("app_id")["app_id"], g.drop_duplicates("app_id")[col]))
    counts = g["app_id"].value_counts()
    return modes, set(counts[counts > 1].index)


def build_model(df: pd.DataFrame, attribute_columns: list[str]) -> Model:
    # Only the topology columns become per-row dicts; attributes are aggregated by pandas.
    # Converting every column of a large inventory to dicts costs hundreds of MB.
    cols = [c for c in MODEL_COLUMNS if c in df.columns]
    recs = Records(df, cols)
    issues: dict[str, list] = defaultdict(list)

    # ---- applications ---------------------------------------------------
    app_rows = df[df["app_id"] != ""]
    name_of, _ = _modes(app_rows, "app_name")
    apps: dict[str, dict] = {}
    per_attr = {col: _modes(app_rows, col) for col in attribute_columns}
    for a in sorted(set(app_rows["app_id"])):
        attrs = {}
        for col in attribute_columns:
            modes, conflicts = per_attr[col]
            attrs[col] = modes.get(a, "")
            if a in conflicts:
                issues["attribute_conflicts"].append(f"{a}: {col}")
        apps[a] = {"name": name_of.get(a, ""), "attrs": attrs}
    del app_rows
    name_to_ids: dict[str, list[str]] = defaultdict(list)
    for a, info in apps.items():
        if info["name"]:
            name_to_ids[info["name"]].append(a)
    name_to_id = {n: sorted(ids)[0] for n, ids in name_to_ids.items()}

    # ---- definitions ------------------------------------------------------
    definitions: dict[tuple[str, str], str] = {}
    xmitqs: set[tuple[str, str]] = set()
    for r in recs:
        k = (r["queue_manager"], r["queue_name"])
        if r.get("queue_usage") == "Transmission":
            xmitqs.add(k)
            continue
        prev = definitions.get(k)
        if prev and prev != r["queue_type"]:
            issues["type_collisions"].append(f"{k[0]}/{k[1]}: {prev} and {r['queue_type']}")
        definitions.setdefault(k, r["queue_type"])
    local_defs = {k for k, t in definitions.items() if t == "Local"}

    # ---- logical queues -----------------------------------------------------
    queues: dict[tuple[str, str], LogicalQueue] = {}
    app_qms: dict[str, set[str]] = defaultdict(set)
    qm_apps: dict[str, set[str]] = defaultdict(set)
    links: set[tuple[str, str]] = set()
    qms: set[str] = set()
    partner: dict[tuple[str, str], dict[str, set[str]]] = defaultdict(lambda: {"p": set(), "c": set()})
    alias_readers: dict[tuple[str, str], set[str]] = defaultdict(set)

    for r in recs:
        qm, qname, qtype, a = r["queue_manager"], r["queue_name"], r["queue_type"], r["app_id"]
        qms.add(qm)
        if r.get("queue_usage") == "Transmission":
            continue
        key = queue_key(r)
        if qtype == "Remote" and key[0] != qm:
            links.add((qm, key[0]))
            qms.add(key[0])
        elif qtype == "Alias" and r.get("alias_target_queue") and key not in definitions:
            issues["dangling_aliases"].append(f"{qm}/{qname} -> {key[1]}")
        if not a:
            continue
        app_qms[a].add(qm)
        qm_apps[qm].add(a)
        lq = queues.setdefault(key, LogicalQueue(key))
        role = r.get("app_role", "")
        if role == "Producer":
            lq.producers.setdefault(a, []).append((qm, qtype))
        elif role == "Consumer":
            lq.consumers.setdefault(a, []).append((qm, qtype))
            if qtype == "Alias":
                alias_readers[key].add(a)
        else:
            lq.others.add(a)
            issues["unknown_role_rows"].append(f"{a} on {qm}/{qname}")
        if r.get("producer_app_name"):
            partner[key]["p"].add(r["producer_app_name"])
        if r.get("consumer_app_name"):
            partner[key]["c"].add(r["consumer_app_name"])

    # ---- fill in the side that has no row of its own ---------------------------
    for key, lq in queues.items():
        if not lq.producers:
            for n in sorted(partner[key]["p"]):
                (lq.inferred_producers.add(name_to_id[n]) if n in name_to_id else lq.ext_producers.add(n))
        if not lq.consumers:
            for n in sorted(partner[key]["c"]):
                (lq.inferred_consumers.add(name_to_id[n]) if n in name_to_id else lq.ext_consumers.add(n))
        if lq.ext_producers or lq.ext_consumers:
            issues["external_partner_queues"].append(f"{key[0]}/{key[1]}")

    # ---- delivery paths -----------------------------------------------------
    paths: list[Path] = []
    for key in sorted(queues):
        lq = queues[key]
        home = lq.home_qm
        prods: list[tuple[Optional[str], Optional[str], int, bool]] = []
        for a in sorted(lq.producers):
            for qm, qtype in sorted(set(lq.producers[a])):
                hops = HOPS[qtype] if not (qtype == "Remote" and qm == home) else 2
                prods.append((a, qm, hops, True))
        for a in sorted(lq.inferred_producers):
            prods.append((a, home, 1, False))
        cons: list[tuple[Optional[str], bool]] = [(a, True) for a in sorted(lq.consumers)]
        cons += [(a, False) for a in sorted(lq.inferred_consumers)]
        if not prods and lq.ext_producers:
            prods = [(EXTERNAL, None, 0, True)]
        if not cons and lq.ext_consumers:
            cons = [(EXTERNAL, True)]
        if lq.producers and key not in local_defs and key[0] in qm_apps and not lq.ext_consumers:
            for a in lq.producers:
                if any(t == "Remote" for _, t in lq.producers[a]):
                    issues["dangling_remote_queues"].append(f"{a} -> {key[0]}/{key[1]}")
                    break
        for pa, pqm, hops, pv in prods:
            for ca, cv in cons:
                if pa is EXTERNAL and ca is EXTERNAL:
                    continue
                extra = 1 if ca in alias_readers[key] else 0
                base = hops if pa is not EXTERNAL else 1
                paths.append(Path(pa, ca, pqm, home if ca is not EXTERNAL else home,
                                  base + extra, key, pv and cv))

    return Model(apps=apps, attr_names=list(attribute_columns), queues=queues, paths=paths,
                 app_qms=dict(app_qms), qm_apps=dict(qm_apps), qms=qms, links=links,
                 definitions=definitions, xmitqs=xmitqs, issues=dict(issues))


def quality_report(model: Model) -> dict:
    """Data-quality facts a user should see before trusting any result."""
    ext = sorted({n for q in model.queues.values() for n in (q.ext_producers | q.ext_consumers)})
    needed = {(a, b) for a, b in model.links}
    return {
        "apps": len(model.apps),
        "queue_managers": len(model.qms),
        "queue_definitions": len(model.definitions),
        "logical_queues": len(model.queues),
        "unknown_role_rows": len(model.issues.get("unknown_role_rows", [])),
        "external_partners": len(ext),
        "external_partner_queues": len(model.issues.get("external_partner_queues", [])),
        "attribute_conflicts": sorted(set(model.issues.get("attribute_conflicts", [])))[:50],
        "type_collisions": model.issues.get("type_collisions", [])[:50],
        "dangling_aliases": model.issues.get("dangling_aliases", [])[:50],
        "dangling_remote_queues": model.issues.get("dangling_remote_queues", [])[:50],
        "transmission_queues_inventoried": len(model.xmitqs & needed),
        "transmission_queues_required": len(needed),
        "apps_on_multiple_qms": sum(1 for q in model.app_qms.values() if len(q) > 1),
    }
