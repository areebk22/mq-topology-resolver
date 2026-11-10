import pandas as pd
import pytest

from mqresolver.config import Config
from mqresolver.generate import generate
from mqresolver.ingest import load_inventory, to_original_format
from mqresolver.model import build_model
from mqresolver.optimise import optimise
from mqresolver.policy import compile_policies
from mqresolver.score import flow_base, inputs_from_model
from mqresolver.validate import validate
from test_optimise import CONFIGS


def build(df, cfg, mapping=None):
    config = Config(**cfg)
    inv = load_inventory(df, mapping=mapping)
    model = build_model(inv.df, inv.attribute_columns)
    comp = compile_policies(config, model.apps, model.attr_names)
    result = optimise(model, flow_base(model), comp, config)
    target = generate(inv, model, result, config)
    gateways = {result.gateway_map.get(g, g) for g in model.external_gateways()}
    report = validate(model, target.df, target.channels, comp, config, gateways)
    return inv, model, comp, config, result, target, report, gateways


def failed(report):
    return {c["id"]: c["examples"] for c in report["checks"] if not c["passed"]}


@pytest.mark.parametrize("name", list(CONFIGS))
def test_target_is_valid_for_every_config(name, sample_df):
    inv, model, comp, config, result, target, report, _ = build(sample_df, CONFIGS[name])
    assert report["passed"], failed(report)
    assert not (target.df.queue_type == "Alias").any()
    out = to_original_format(inv, target.df)
    assert list(out.columns) == inv.original_columns            # same columns, same order
    tgt = build_model(target.df, model.attr_names)
    assert {a: next(iter(q)) for a, q in tgt.app_qms.items()} == result.assignment
    # the score the optimiser predicted is the score of what was actually generated
    exact = inputs_from_model(tgt, result.inputs.policy)
    assert exact.qm_pairs == result.inputs.qm_pairs
    assert exact.hop_sum / exact.path_weight == pytest.approx(result.inputs.hop_sum / result.inputs.path_weight, abs=0.05)
    assert exact.n_qms == result.inputs.n_qms


def test_target_contents(sample_df):
    inv, model, comp, config, result, target, report, gateways = build(sample_df, CONFIGS["isolate+domain"])
    df = target.df
    rem = df[df.queue_type == "Remote"]
    assert (rem.xmit_queue_name == rem.remote_queue_manager).all() and len(rem) > 0
    xq = df[df.queue_usage == "Transmission"]
    assert set(zip(xq.queue_manager, xq.queue_name)) == set(zip(rem.queue_manager, rem.remote_queue_manager))
    assert (xq.app_id == "").all()
    senders = [c for c in target.channels if c["channel_type"] == "Sender"]
    assert len(senders) == len(xq) == len(target.channels) / 2
    assert all(c["channel_name"] == f"{c['queue_manager']}.{c['partner_queue_manager']}" for c in senders)
    assert any(c["partner_is_external"] for c in senders)
    assert "DEFINE QREMOTE" in target.mqsc and "CHLTYPE(SDR)" in target.mqsc and "USAGE(XMITQ)" in target.mqsc
    assert (df.cluster_name == "").all()
    assert len(df) < len(inv.df)                                  # sprawl and aliases collapsed
    assert set(df[df.app_id != ""].app_id) == set(model.apps)


def test_keep_aliases(small_df):
    *_, target, report, _ = build(small_df, {"target": {"eliminate_aliases": False}})
    assert report["passed"], failed(report)
    al = target.df[target.df.queue_type == "Alias"]
    assert len(al) > 0
    objects = set(zip(target.df.queue_manager, target.df.queue_name))
    assert all((q, t) in objects for q, t in zip(al.queue_manager, al.alias_target_queue))


def test_gateway_remap(small_df):
    *_, target, report, gateways = build(small_df, {"target": {"gateway_qms": ["DMZA", "DMZB"]}})
    assert report["passed"], failed(report)
    assert gateways <= {"DMZA", "DMZB"}
    ext = {c["partner_queue_manager"] for c in target.channels if c["partner_is_external"] and c["channel_type"] == "Sender"}
    assert ext and ext <= {"DMZA", "DMZB"}


def test_mapped_headers_round_trip(small_df):
    from test_ingest_model import RENAMES
    renamed = small_df.rename(columns=RENAMES)
    renamed["Type"] = renamed["Type"].map({"Local": "QL", "Remote": "QR", "Alias": "QA"})
    inv, *_, target, report, _ = build(renamed, {}, mapping={"columns": {
        "app_id": "Application ID", "app_name": "Application Name", "alias_target_queue": "Target"}})
    assert report["passed"], failed(report)
    out = to_original_format(inv, target.df)
    assert list(out.columns) == list(renamed.columns) and set(out["Type"]) <= {"QL", "QR"}


def test_same_queue_name_on_two_qms_is_not_merged():
    rows = []
    for qm, prod, cons in (("QM1", "A", "B"), ("QM2", "C", "D")):
        rows.append({"queue_name": "SHARED.Q", "queue_type": "Local", "queue_manager": qm, "app_id": cons,
                     "app_name": cons, "app_role": "Consumer"})
        rows.append({"queue_name": "SHARED.Q", "queue_type": "Local", "queue_manager": qm, "app_id": prod,
                     "app_name": prod, "app_role": "Producer"})
    rows.append({"queue_name": "A.D.Q", "queue_type": "Local", "queue_manager": "QM1", "app_id": "A", "app_name": "A",
                 "app_role": "Producer"})
    rows.append({"queue_name": "A.D.Q", "queue_type": "Local", "queue_manager": "QM1", "app_id": "D", "app_name": "D",
                 "app_role": "Consumer"})
    *_, result, target, report, _ = build(pd.DataFrame(rows).fillna(""), {"policies": []})
    assert report["passed"], failed(report)
    assert len(set(result.assignment.values())) == 1              # all four end up together
    names = set(target.df.queue_name)
    assert "SHARED.Q" in names and len([n for n in names if n.startswith("SHARED.Q")]) == 2
    assert target.renamed and any("renamed" in w for w in target.warnings)


def corrupt(kind, df, channels):
    df, channels = df.copy(), [dict(c) for c in channels]
    if kind == "app-on-two-qms":
        row = df[df.app_id != ""].iloc[0].copy()
        row["queue_manager"] = "QMELSEWHERE"
        df = pd.concat([df, row.to_frame().T], ignore_index=True)
    elif kind == "remote-to-missing-qm":
        i = df.index[df.queue_type == "Remote"][0]
        df.loc[i, "remote_queue_manager"] = "QMGHOST"
    elif kind == "remote-queue-missing":
        i = df.index[df.queue_type == "Remote"][0]
        df.loc[i, "remote_queue_name"] = "NO.SUCH.QUEUE"
    elif kind == "xmitq-missing":
        df = df.drop(df.index[df.queue_usage == "Transmission"][0])
    elif kind == "channel-half-missing":
        channels = [c for c in channels if c != next(x for x in channels if x["channel_type"] == "Receiver")]
    elif kind == "alias-left-in":
        i = df.index[(df.queue_type == "Local") & (df.app_role == "Consumer")][0]
        df.loc[i, "queue_type"] = "Alias"
    elif kind == "flow-dropped":
        i = df.index[df.queue_type == "Remote"][0]
        df = df.drop(i)
    elif kind == "consumer-on-remote":
        i = df.index[(df.queue_type == "Remote")][0]
        df.loc[i, "app_role"] = "Consumer"
    elif kind == "policy-broken":
        apps = df[df.app_id != ""]
        qm = apps.queue_manager.value_counts().index[0]
        victim = apps[apps.app_criticality == "Mission Critical"].app_id.iloc[0]
        df.loc[df.app_id == victim, "queue_manager"] = qm
    return df.reset_index(drop=True), channels


@pytest.mark.parametrize("kind,expect", [
    ("app-on-two-qms", "S1"), ("remote-to-missing-qm", "S2"), ("remote-queue-missing", "S2"),
    ("xmitq-missing", "S2"), ("channel-half-missing", "S4"), ("alias-left-in", "aliases"),
    ("flow-dropped", "S5"), ("consumer-on-remote", "S3"), ("policy-broken", "policy:isolate"),
])
def test_validator_catches_corruption(kind, expect, sample_df):
    inv, model, comp, config, result, target, report, gateways = build(sample_df, CONFIGS["isolate"])
    assert report["passed"]
    df, channels = corrupt(kind, target.df, target.channels)
    bad = validate(model, df, channels, comp, config, gateways)
    assert not bad["passed"] and expect in failed(bad), failed(bad)
