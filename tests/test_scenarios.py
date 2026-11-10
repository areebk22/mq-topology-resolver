"""Every estate x configuration must produce a valid, better target. See tests/scenarios.py."""
import pytest

from mqresolver import pipeline
from mqresolver.policy import PolicyError

import scenarios

CASES = [(e, c) for e in scenarios.ESTATES if e != "large" for c in scenarios.configs(e)]
LARGE = [("large", c) for c in ("default", "isolate+domain-hard", "isolate-soft+domain-soft")]
_cache: dict = {}


def _estate(name):
    if name not in _cache:
        _cache[name] = scenarios.estate(name)
    return _cache[name]


def _run(est, cfg, **kw):
    src, fname, mapping = _estate(est)
    return pipeline.run(src, filename=fname, mapping=mapping, config=cfg, **kw)


def _assert_good(b, est, cname):
    failed = [c for c in b["validation"]["checks"] if not c["passed"]]
    assert b["validation"]["passed"], (est, cname, failed)
    for p in b["policies"]["target"]:
        if p["mode"] == "hard":
            assert p["violating"] == 0, (est, cname, p)
    sc = b["score"]
    assert sc["target"]["tci"] < sc["as_is"]["tci"], (est, cname)
    assert sc["anchors_ok"], (est, cname, sc["anchors"], sc["target"]["tci"])
    t = b["summary"]["target"]
    assert t["apps_on_multiple_qms"] == 0
    assert t["channel_pairs"] < b["summary"]["as_is"]["channel_pairs"]
    if b["config"]["target"]["eliminate_aliases"]:
        assert t["alias_queues"] == 0


@pytest.mark.parametrize("est,cname", CASES, ids=[f"{e}:{c}" for e, c in CASES])
def test_scenario(est, cname):
    b = _run(est, scenarios.configs(est)[cname], include_graph=False)
    _assert_good(b, est, cname)


@pytest.mark.slow
@pytest.mark.parametrize("est,cname", LARGE, ids=[f"{e}:{c}" for e, c in LARGE])
def test_large_scenario(est, cname):
    b = _run(est, scenarios.configs(est)[cname], include_graph=True)
    _assert_good(b, est, cname)
    assert b["seconds"] < 60
    assert len(b["graph"]["as_is"]["nodes"]) == b["summary"]["as_is"]["queue_managers"] + \
        b["summary"]["as_is"]["external_gateways"]


@pytest.mark.parametrize("est", ["messy", "foreign-format"])
def test_scenarios_are_deterministic(est):
    cfg = scenarios.configs(est)["isolate-soft+domain-soft"]
    a, b = _run(est, cfg, include_graph=False), _run(est, cfg, include_graph=False)
    assert a["files"] == b["files"]
    assert a["decisions"] == b["decisions"]


def test_foreign_format_round_trips():
    b = _run("foreign-format", {}, include_graph=False)
    head = b["files"]["target_topology.csv"].splitlines()[0]
    assert head.startswith("Queue,Type,QMgr,App Code")
    assert "QREMOTE" in b["files"]["target_topology.csv"] and ",Remote," not in b["files"]["target_topology.csv"]


def test_messy_estate_reports_quality_issues():
    b = _run("messy", {}, include_graph=False)
    q = b["quality"]
    assert q["duplicate_rows_removed"] > 0
    assert q["unknown_role_rows"] >= 150
    assert q["external_partners"] > 20
    assert q["transmission_queues_inventoried"] == 0 < q["transmission_queues_required"]


def test_attribute_policy_on_estate_without_attributes_is_explained():
    with pytest.raises(PolicyError, match="not in the inventory"):
        _run("no-attributes", {"policies": [{"type": "homogeneous", "attribute": "app_business_domain"}]})


def test_contradictory_hard_policies_are_explained():
    cfg = {"policies": [{"type": "isolate", "when": {"app_regulated": "Yes"}}, {"type": "qm_count", "max": 5}]}
    with pytest.raises(PolicyError, match="cannot reach the hard limit"):
        _run("sample", cfg, include_graph=False)
    # the same rules as soft are a trade-off, not an error
    soft = {"policies": [{**p, "mode": "soft"} for p in cfg["policies"]]}
    _assert_good(_run("sample", soft, include_graph=False), "sample", "soft-contradiction")


def test_hard_to_soft_never_worse():
    hard = scenarios.configs("sample")["isolate+domain-hard"]
    soft = {"policies": [hard["policies"][0]] + [{**p, "mode": "soft"} for p in hard["policies"][1:]]}
    h, s = _run("sample", hard, include_graph=False), _run("sample", soft, include_graph=False)
    assert s["score"]["target"]["tci"] <= h["score"]["target"]["tci"] + 1e-6
