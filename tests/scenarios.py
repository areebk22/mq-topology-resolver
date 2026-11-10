"""Contrasting enterprise estates and policy setups, shared by the tests and scripts/run_scenarios.py.

Each estate stresses something different: size, how consolidated or fragmented the
as-is estate already is, data quality, missing attributes, and a foreign export format.
"""
from __future__ import annotations

import io

import pandas as pd

from mqresolver import synth

ATTRS = [c for c in synth.COLS if c.startswith("app_") and c not in ("app_id", "app_name", "app_role")]

ESTATES = {
    # name: (description, generator arguments)
    "startup": ("30 apps on 8 QMs: a small estate", dict(apps=30, qms=8, flows=50, seed=1)),
    "sample": ("400 apps on 200 QMs: the bundled sample", dict()),
    "consolidated": ("600 apps on 40 overloaded QMs", dict(apps=600, qms=40, flows=1100, seed=3)),
    "fragmented": ("300 apps spread over 450 QMs", dict(apps=300, qms=450, flows=520, seed=4)),
    "messy": ("250 apps, 25% external partners, 10% duplicates, 200 unknown roles, no xmitq rows",
              dict(apps=250, qms=120, flows=450, seed=5, external_rate=0.25, duplicate_rate=0.10,
                   unknown_roles=200, xmitq_inventory=0.0)),
    "no-attributes": ("200 apps, topology columns only", dict(apps=200, qms=90, flows=360, seed=6)),
    "foreign-format": ("200 apps, own headers and QLOCAL/QREMOTE spellings, delivered as XLSX",
                       dict(apps=200, qms=100, flows=380, seed=8)),
    "large": ("2,500 apps on 800 QMs", dict(apps=2500, qms=800, flows=4800, seed=9)),
}

FOREIGN_HEADERS = {
    "queue_name": "Queue", "queue_type": "Type", "queue_manager": "QMgr", "app_id": "App Code",
    "app_name": "Application", "app_role": "Application Role", "producer_app_name": "Sender App",
    "consumer_app_name": "Receiver App", "remote_queue_manager": "RQMNAME", "remote_queue_name": "RNAME",
    "xmit_queue_name": "XMITQ", "alias_target_queue": "TARGET", "queue_usage": "USAGE",
    "app_business_domain": "Portfolio", "app_criticality": "Tier",
}
FOREIGN_MAPPING = {"columns": {k: v for k, v in FOREIGN_HEADERS.items() if not k.startswith("app_business")
                               and k != "app_criticality"}}
FOREIGN_ATTR = {"app_business_domain": "Portfolio", "app_criticality": "Tier"}


def estate(name: str) -> tuple[object, str | None, dict | None]:
    """(source, filename, mapping) ready for pipeline.run / pipeline.inspect."""
    _, kw = ESTATES[name]
    cols, rows = synth.generate(**kw)
    df = pd.DataFrame(rows, columns=cols).astype(str)
    if name == "no-attributes":
        return df.drop(columns=ATTRS), None, None
    if name == "foreign-format":
        df = df.rename(columns=FOREIGN_HEADERS)
        df["Type"] = df["Type"].map({"Local": "QLOCAL", "Remote": "QREMOTE", "Alias": "QALIAS"})
        df["Application Role"] = df["Application Role"].map(
            {"Producer": "PUT", "Consumer": "GET"}).fillna("TBD")
        buf = io.BytesIO()
        df.to_excel(buf, index=False)
        return buf.getvalue(), "inventory.xlsx", FOREIGN_MAPPING
    return df, None, None


def attr(estate_name: str, canonical: str) -> str:
    """Attribute column name as it appears in that estate's own file."""
    return FOREIGN_ATTR.get(canonical, canonical) if estate_name == "foreign-format" else canonical


CAP = {"id": "capacity", "type": "capacity", "max_apps_per_qm": 40}


def configs(estate_name: str) -> dict[str, dict]:
    """Policy and option setups that make sense for an estate (attribute rules need attributes)."""
    a = lambda c: attr(estate_name, c)  # noqa: E731
    out = {
        "default": {},
        "no-policies": {"policies": []},
        "tight-capacity": {"policies": [{"type": "capacity", "max_apps_per_qm": 12, "max_queues_per_qm": 400}]},
        "qm-budget": {"policies": [CAP, {"type": "qm_count", "max": 60}]},
        "resilient": {"profile": "resilient", "policies": [CAP]},
        "consolidate": {"profile": "consolidate", "policies": [{**CAP, "max_apps_per_qm": 60}]},
        "keep-aliases": {"policies": [CAP], "target": {"eliminate_aliases": False}},
        "one-gateway": {"policies": [CAP], "target": {"gateway_qms": ["QMEXTGW"], "reuse_qm_names": False}},
    }
    if estate_name == "no-attributes":
        return out
    iso = {"id": "isolate-critical", "type": "isolate", "when": {a("app_criticality"): "Mission Critical"}}
    dom = {"id": "one-domain", "type": "homogeneous", "attribute": a("app_business_domain")}
    out.update({
        "isolate-hard": {"policies": [CAP, iso]},
        "domain-hard": {"policies": [CAP, dom]},
        "isolate+domain-hard": {"policies": [CAP, iso, dom]},
        "isolate-soft+domain-soft": {"policies": [CAP, {**iso, "mode": "soft"}, {**dom, "mode": "soft", "weight": 2}]},
        "compliance": {"profile": "compliance", "policies": [CAP, iso, {**dom, "mode": "soft"}]},
    })
    if estate_name != "foreign-format":
        out["separate-regulated"] = {"policies": [CAP, {"type": "separate", "attribute": "app_regulated"}]}
        out["custom-weights"] = {"weights": {"simplicity": 1, "resilience": 3, "policy": 1},
                                 "policies": [CAP, {"type": "separate", "groups": [
                                     {"app_internet_facing": "Yes"},
                                     {"app_internet_facing": "No", "app_data_classification": "Restricted"}]}]}
    return out
