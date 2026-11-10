import networkx as nx
import pandas as pd
import pytest

from mqresolver.config import PROFILES, Config
from mqresolver.ingest import load_inventory
from mqresolver.model import build_model
from mqresolver.policy import compile_policies, evaluate, policy_dimension
from mqresolver.score import (ScoreInputs, describe, dimensions, flow_base, inputs_from_assignment,
                              inputs_from_model, sensitivity, tci_of)

W = PROFILES["balanced"]


def louvain_assignment(model, base):
    g = nx.Graph()
    g.add_nodes_from(base.apps)
    for (p, c), n in base.pair_paths.items():
        if p != c:
            g.add_edge(p, c, weight=g.get_edge_data(p, c, {"weight": 0})["weight"] + n)
    cs = nx.community.louvain_communities(g, weight="weight", resolution=2, seed=1)
    return {a: f"C{i}" for i, c in enumerate(sorted(cs, key=min)) for a in c}


def policy_violation(model, assign, config=None):
    c = compile_policies(config or Config(), model.apps, model.attr_names)
    qm_apps = {}
    for a, q in assign.items():
        qm_apps.setdefault(q, set()).add(a)
    return policy_dimension(evaluate(c, qm_apps, {}))


def test_dimensions_bounded(sample_model):
    si = inputs_from_model(sample_model, 0.3)
    d = dimensions(si)
    assert all(0 <= v <= 1 for v in d.values()), d
    assert d["app_sprawl"] > 0.3 and d["routing_depth"] > 0.2
    assert 0 < tci_of(si, W) < 100


def test_degenerate_cases_exact():
    empty = ScoreInputs(0, 0, 0, 0, 0, 0, 0, 0, 0, 0)
    assert tci_of(empty, W) == 0
    best = ScoreInputs(10, 10, 50, 50, 0, 20, 50, 50, 0.1, 0)
    assert dimensions(best) == {"routing_depth": 0, "app_sprawl": 0, "channel_load": 0, "object_overhead": 0,
                                "qm_footprint": 0, "blast_radius": 0.1, "policy_violations": 0}


def test_anchors_lose_to_a_sensible_partition(sample_model):
    base = flow_base(sample_model)
    single = {a: "ONE" for a in base.apps}
    per_app = {a: f"Q_{a}" for a in base.apps}
    louv = louvain_assignment(sample_model, base)
    score = {}
    for name, assign in (("single", single), ("per_app", per_app), ("louvain", louv)):
        si = inputs_from_assignment(base, assign, policy_violation(sample_model, assign))
        score[name] = tci_of(si, W)
    as_is = tci_of(inputs_from_model(sample_model, 0), W)
    assert score["louvain"] < score["single"], score
    assert score["louvain"] < score["per_app"], score
    assert score["louvain"] < as_is, (score, as_is)
    # without any capacity policy a single QM is still penalised through blast radius
    si = inputs_from_assignment(base, single, 0.0)
    assert dimensions(si)["blast_radius"] > 0.9
    assert tci_of(si, PROFILES["resilient"]) > tci_of(
        inputs_from_assignment(base, louv, 0.0), PROFILES["resilient"])


def test_single_qm_and_per_app_dimension_values(sample_model):
    base = flow_base(sample_model)
    d1 = dimensions(inputs_from_assignment(base, {a: "ONE" for a in base.apps}))
    assert d1["channel_load"] < 0.02 and d1["app_sprawl"] == 0   # only gateway links remain
    assert d1["routing_depth"] < 0.05          # only external-partner traffic leaves the QM
    dn = dimensions(inputs_from_assignment(base, {a: f"Q_{a}" for a in base.apps}))
    assert dn["channel_load"] == 1 and dn["routing_depth"] > 0.5 and dn["blast_radius"] < 0.05
    assert dn["qm_footprint"] == 1 and d1["qm_footprint"] < 0.01


def test_size_independence(small_df):
    one = load_inventory(small_df)
    m1 = build_model(one.df, one.attribute_columns)
    copy = small_df.copy()
    for col in ("queue_name", "queue_manager", "app_id", "app_name", "producer_app_name", "consumer_app_name",
                "remote_queue_manager", "remote_queue_name", "xmit_queue_name", "alias_target_queue"):
        copy[col] = copy[col].map(lambda v: f"X{v}" if v else v)
    two = load_inventory(pd.concat([small_df, copy], ignore_index=True))
    m2 = build_model(two.df, two.attribute_columns)
    assert len(m2.apps) == 2 * len(m1.apps)
    d1, d2 = dimensions(inputs_from_model(m1, 0)), dimensions(inputs_from_model(m2, 0))
    for k in ("routing_depth", "app_sprawl", "channel_load", "object_overhead", "qm_footprint"):
        assert d2[k] == pytest.approx(d1[k], abs=1e-9), k
    assert d2["blast_radius"] == pytest.approx(d1["blast_radius"] / 2)
    assert abs(tci_of(inputs_from_model(m1, 0), W) - tci_of(inputs_from_model(m2, 0), W)) < 2


def test_deterministic_and_reports(sample_model):
    a, b = inputs_from_model(sample_model, 0.1), inputs_from_model(sample_model, 0.1)
    assert a == b
    rep = describe(a, W)
    assert set(rep) == {"tci", "dimensions", "raw"} and rep["raw"]["qms_per_app"] > 1
    base = flow_base(sample_model)
    after = inputs_from_assignment(base, louvain_assignment(sample_model, base))
    sens = sensitivity(a, after)
    assert [s["profile"] for s in sens] == list(PROFILES)
    assert all(s["target"] < s["as_is"] for s in sens)
