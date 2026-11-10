import pytest
from pydantic import ValidationError

from mqresolver.config import Config, load_config, matches
from mqresolver.policy import PolicyError, compile_policies, evaluate, policy_dimension, suggest_policies

APPS = {
    "A": {"name": "a", "attrs": {"dom": "Sales", "crit": "High", "reg": "Yes"}},
    "B": {"name": "b", "attrs": {"dom": "Sales", "crit": "Low", "reg": "No"}},
    "C": {"name": "c", "attrs": {"dom": "Finance", "crit": "Low", "reg": "No"}},
    "D": {"name": "d", "attrs": {"dom": "Finance", "crit": "High", "reg": "Yes"}},
    "E": {"name": "e", "attrs": {"dom": "", "crit": "Low", "reg": "No"}},
}
ATTRS = ["dom", "crit", "reg"]


def run(policies, qm_apps, queues=None):
    c = compile_policies(Config(policies=policies), APPS, ATTRS)
    return {r.id: r for r in evaluate(c, qm_apps, queues or {})}, c


def test_selector():
    assert matches({"dom": "Sales"}, {"dom": "Sales"})
    assert matches({"dom": ["Sales", "HR"]}, {"dom": "HR"})
    assert not matches({"dom": {"not": "Sales"}}, {"dom": "Sales"})
    assert matches({"dom": {"not": ["Sales"]}, "crit": "Low"}, {"dom": "HR", "crit": "Low"})
    assert not matches({"missing": "x"}, {"dom": "HR"})


def test_isolate():
    r, _ = run([{"type": "isolate", "when": {"crit": "High"}}], {"Q1": {"A", "B"}, "Q2": {"D"}, "Q3": {"C", "E"}})
    assert r["isolate"].violating == 1 and r["isolate"].violation == 0.5 and r["isolate"].offenders == ["A"]


def test_homogeneous_and_max_distinct():
    qa = {"Q1": {"A", "B", "C"}, "Q2": {"D", "E"}}
    r, _ = run([{"type": "homogeneous", "attribute": "dom"}], qa)
    assert r["homogeneous"].violating == 2          # C on Q1, and one of D/E on Q2 (blank is a value)
    r, _ = run([{"type": "homogeneous", "attribute": "dom", "max_distinct": 2}], qa)
    assert r["homogeneous"].violation == 0


def test_separate_attribute_and_groups():
    qa = {"Q1": {"A", "B"}, "Q2": {"C", "D", "E"}}
    r, _ = run([{"type": "separate", "attribute": "reg"}], qa)
    assert r["separate"].violating == 2
    r, _ = run([{"type": "separate", "groups": [{"crit": "High"}, {"dom": "Sales", "crit": "Low"}]}], qa)
    assert r["separate"].violating == 1             # A and B mix; C/D/E: only D is in a group
    with pytest.raises(PolicyError, match="more than one group"):
        run([{"type": "separate", "groups": [{"crit": "High"}, {"dom": "Sales"}]}], qa)


def test_colocate_pin_capacity_qmcount():
    qa = {"Q1": {"A", "B", "C"}, "Q2": {"D"}, "Q3": {"E"}}
    r, _ = run([
        {"type": "colocate", "when": {"dom": "Finance"}},
        {"type": "pin", "when": {"app_id": "E"}, "qm": "Q9"},
        {"type": "capacity", "max_apps_per_qm": 2, "max_queues_per_qm": 10},
        {"type": "qm_count", "max": 2},
    ], qa, {"Q1": 30, "Q2": 5})
    assert r["colocate"].violating == 1
    assert r["pin"].violating == 1 and r["pin"].violation == 1
    assert r["capacity"].offenders == ["Q1"] and r["capacity"].violation == pytest.approx(20 / 35)
    assert r["qm_count"].violation == 0.5


def test_multi_qm_apps_as_is():
    # an app on several QMs is evaluated on each of them
    r, _ = run([{"type": "isolate", "when": {"crit": "High"}}], {"Q1": {"A"}, "Q2": {"A", "B"}, "Q3": {"D"}})
    assert r["isolate"].offenders == ["A"]


def test_off_is_ignored_and_dimension_weighting():
    r, c = run([
        {"type": "isolate", "when": {"crit": "High"}, "mode": "off"},
        {"id": "h", "type": "homogeneous", "attribute": "dom", "mode": "soft", "weight": 3},
        {"type": "capacity", "max_apps_per_qm": 10},
    ], {"Q1": {"A", "C"}})
    assert set(r) == {"h", "capacity"}
    assert policy_dimension(list(r.values())) == pytest.approx(3 * r["h"].violation / 4)
    assert policy_dimension([]) == 0


@pytest.mark.parametrize("policies,msg", [
    ([{"type": "homogeneous", "attribute": "nope"}], "not in the inventory"),
    ([{"type": "isolate", "when": {"crit": "High"}}, {"type": "colocate", "when": {"dom": "Sales"}}], "keeps alone"),
    ([{"type": "pin", "when": {"app_id": "A"}, "qm": "X"}, {"type": "pin", "when": {"app_id": "A"}, "qm": "Y"}],
     "pinned to both"),
    ([{"type": "colocate", "when": {"crit": "Low"}}, {"type": "separate", "attribute": "dom"}], "keeps apart"),
    ([{"type": "colocate", "when": {"crit": "Low"}}, {"type": "capacity", "max_apps_per_qm": 2}], "capacity limit"),
    ([{"type": "pin", "when": {"dom": "Sales"}, "qm": "X"}, {"type": "isolate", "when": {"crit": "High"}}],
     "must be isolated"),
])
def test_conflicts_are_explained(policies, msg):
    with pytest.raises(PolicyError, match=msg):
        compile_policies(Config(policies=policies), APPS, ATTRS)


def test_soft_policies_do_not_conflict():
    compile_policies(Config(policies=[{"type": "isolate", "when": {"crit": "High"}, "mode": "soft"},
                                      {"type": "colocate", "when": {"dom": "Sales"}}]), APPS, ATTRS)


@pytest.mark.parametrize("bad", [
    {"policies": [{"type": "isolate"}]},
    {"policies": [{"type": "homogeneous"}]},
    {"policies": [{"type": "pin", "when": {"a": 1}}]},
    {"policies": [{"type": "capacity"}]},
    {"policies": [{"type": "qm_count", "min": 5, "max": 2}]},
    {"policies": [{"type": "nonsense"}]},
    {"profile": "fastest"},
    {"weights": {"speed": 1}},
    {"unknown_key": 1},
])
def test_config_validation(bad):
    with pytest.raises(ValidationError):
        Config(**bad)


def test_config_defaults_and_weights():
    c = load_config(None)
    assert [p.type for p in c.policies] == ["capacity"]
    assert Config(policies=[]).policies == []
    assert Config(weights={"simplicity": 2, "resilience": 2}).score_weights() == {
        "simplicity": 0.5, "resilience": 0.5, "policy": 0.0}
    c = load_config("profile: resilient\npolicies:\n  - {type: qm_count, max: 9}\n  - {type: qm_count, min: 2}\n")
    assert c.score_weights()["resilience"] == 0.5 and [p.id for p in c.policies] == ["qm_count", "qm_count-2"]
    assert load_config("config/default.yaml").policies[0].max_apps_per_qm == 40


def test_suggestions(sample_model):
    sizes = [len(v) for v in sample_model.qm_apps.values()]
    s = suggest_policies(sample_model.apps, sample_model.attr_names, sizes)
    types = {p["type"] for p in s}
    assert {"capacity", "homogeneous", "separate", "isolate"} <= types
    assert any(p.get("when") == {"app_criticality": "Mission Critical"} for p in s)
    Config(policies=s)   # every suggestion is a valid policy
