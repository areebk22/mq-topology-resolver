from collections import Counter

import pytest

from mqresolver.config import Config
from mqresolver.optimise import optimise
from mqresolver.policy import PolicyError, compile_policies, evaluate
from mqresolver.score import flow_base, inputs_from_assignment

CAP = {"type": "capacity", "max_apps_per_qm": 40}
ISO = {"type": "isolate", "when": {"app_criticality": "Mission Critical"}}
DOM = {"type": "homogeneous", "attribute": "app_business_domain"}
REG = {"type": "separate", "attribute": "app_regulated"}

CONFIGS = {
    "default": {},
    "no-policies": {"policies": []},
    "tight-capacity": {"policies": [{"type": "capacity", "max_apps_per_qm": 12, "max_queues_per_qm": 400}]},
    "isolate": {"policies": [CAP, ISO]},
    "domain": {"policies": [CAP, DOM]},
    "isolate+domain": {"policies": [CAP, ISO, DOM]},
    "separate-regulated": {"policies": [CAP, REG]},
    "two-domains-per-qm": {"policies": [CAP, {**DOM, "max_distinct": 2}]},
    "separate-groups": {"policies": [CAP, {"type": "separate", "groups": [
        {"app_internet_facing": "Yes", "app_data_classification": {"not": "Restricted"}},
        {"app_internet_facing": "No", "app_data_classification": "Restricted"}]}]},
    "all-soft": {"policies": [{**CAP, "mode": "soft"}, {**ISO, "mode": "soft"}, {**DOM, "mode": "soft", "weight": 2}]},
    "qm-budget": {"policies": [CAP, {"type": "qm_count", "max": 15}]},
    "resilient-profile": {"profile": "resilient", "policies": [CAP]},
    "compliance-profile": {"profile": "compliance", "policies": [CAP, ISO, {**DOM, "mode": "soft"}]},
    "custom-weights": {"weights": {"simplicity": 1, "resilience": 3, "policy": 0}},
}


def solve(model, cfg):
    config = Config(**cfg)
    comp = compile_policies(config, model.apps, model.attr_names)
    base = flow_base(model)
    return optimise(model, base, comp, config), comp, base


def by_qm(assignment):
    out = {}
    for a, q in assignment.items():
        out.setdefault(q, set()).add(a)
    return out


@pytest.mark.parametrize("name", list(CONFIGS))
def test_every_config_is_valid_and_better(name, sample_model):
    r, comp, base = solve(sample_model, CONFIGS[name])
    assert set(r.assignment) == set(sample_model.apps)                 # S1: one QM per app
    for res in evaluate(comp, by_qm(r.assignment), {}):
        if res.mode == "hard":
            assert res.violation == 0, (name, res)
    exact = inputs_from_assignment(base, r.assignment, r.inputs.policy)
    for f in exact.__dataclass_fields__:                                 # incremental state == recomputed
        assert getattr(exact, f) == pytest.approx(getattr(r.inputs, f)), f
    assert not (set(r.assignment.values()) & sample_model.external_gateways())


def test_deterministic(sample_model):
    a, _, _ = solve(sample_model, CONFIGS["isolate+domain"])
    b, _, _ = solve(sample_model, CONFIGS["isolate+domain"])
    assert a.assignment == b.assignment and a.score == b.score


def test_acceptance_levels(sample_model):
    r, _, _ = solve(sample_model, CONFIGS["default"])
    si = r.inputs
    same = 1 - (si.hop_sum - si.path_weight) / 4 / si.path_weight
    assert same >= 0.60 and si.qm_pairs <= 180
    assert max(Counter(r.assignment.values()).values()) <= 40
    r, _, _ = solve(sample_model, CONFIGS["isolate+domain"])
    si = r.inputs
    same = 1 - (si.hop_sum - si.path_weight) / 4 / si.path_weight
    assert same >= 0.50 and si.qm_pairs <= 260


@pytest.mark.parametrize("policy", [ISO, DOM, REG])
def test_soft_is_never_worse_than_hard(policy, sample_model):
    hard, _, _ = solve(sample_model, {"policies": [CAP, policy]})
    soft, _, _ = solve(sample_model, {"policies": [CAP, {**policy, "mode": "soft"}]})
    assert soft.score <= hard.score + 1e-6


def test_isolated_apps_are_alone(sample_model):
    r, _, _ = solve(sample_model, CONFIGS["isolate"])
    crit = [a for a, i in sample_model.apps.items() if i["attrs"]["app_criticality"] == "Mission Critical"]
    sizes = Counter(r.assignment.values())
    assert crit and all(sizes[r.assignment[a]] == 1 for a in crit)
    assert all("isolate" in r.reasons[a] for a in crit)


def test_pin_and_colocate(sample_model):
    apps = sorted(sample_model.apps)
    hr = [a for a in apps if sample_model.apps[a]["attrs"]["app_business_domain"] == "HR"]
    r, _, _ = solve(sample_model, {"policies": [
        CAP,
        {"type": "pin", "when": {"app_id": apps[:3]}, "qm": "QMPINNED"},
        {"type": "colocate", "when": {"app_business_domain": "HR"}},
    ]})
    assert {r.assignment[a] for a in apps[:3]} == {"QMPINNED"}
    assert len({r.assignment[a] for a in hr}) == 1 and len(hr) > 1


def test_qm_count_bounds(sample_model):
    r, _, _ = solve(sample_model, {"policies": [CAP, {"type": "qm_count", "max": 11}]})
    assert len(set(r.assignment.values())) <= 11
    r, _, _ = solve(sample_model, {"policies": [CAP, {"type": "qm_count", "min": 30}]})
    assert len(set(r.assignment.values())) >= 30
    with pytest.raises(PolicyError, match="cannot reach the hard limit"):
        solve(sample_model, {"policies": [CAP, ISO, {"type": "qm_count", "max": 20}]})
    with pytest.raises(PolicyError, match="cannot reach the hard limit"):
        solve(sample_model, {"policies": [CAP, {"type": "qm_count", "max": 5}]})


def test_names_reuse_and_gateways(sample_model):
    r, _, _ = solve(sample_model, {})
    assert set(r.reused_qms) <= sample_model.qms and not r.new_qms
    r, _, _ = solve(sample_model, {"target": {"reuse_qm_names": False, "new_qm_template": "TQM{n:02d}",
                                              "gateway_qms": ["DMZ1"]}})
    assert all(q.startswith("TQM") for q in r.assignment.values())
    assert set(r.gateway_map.values()) == {"DMZ1"}


def test_small_estate_and_no_attributes(small_df):
    from mqresolver.ingest import load_inventory
    from mqresolver.model import build_model
    bare = small_df[[c for c in small_df.columns if not c.startswith("app_") or c in ("app_id", "app_name", "app_role")]]
    inv = load_inventory(bare)
    m = build_model(inv.df, inv.attribute_columns)
    assert m.attr_names == []
    r, _, _ = solve(m, {})
    assert set(r.assignment) == set(m.apps)
    with pytest.raises(PolicyError, match="not in the inventory"):
        solve(m, {"policies": [DOM]})
