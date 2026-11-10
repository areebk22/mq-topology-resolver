import io

import pandas as pd
import pytest

from mqresolver.ingest import InventoryError, load_inventory, propose_mapping, read_table, to_original_format
from mqresolver.model import build_model, quality_report

RENAMES = {"queue_name": "Queue", "queue_type": "Type", "queue_manager": "QMgr", "app_id": "Application ID",
           "app_name": "Application Name", "app_role": "Application Role", "producer_app_name": "Producer",
           "consumer_app_name": "Consumer", "remote_queue_manager": "Remote QMgr",
           "remote_queue_name": "Remote Queue", "xmit_queue_name": "XmitQ", "alias_target_queue": "Target",
           "queue_usage": "Usage", "app_business_domain": "Portfolio"}


def test_sample_counts(sample_inv, sample_model):
    q = quality_report(sample_model)
    assert sample_inv.quality["duplicate_rows_removed"] > 100
    assert q["apps"] == 400
    assert q["unknown_role_rows"] == 32
    assert q["type_collisions"] == [] and q["dangling_aliases"] == [] and q["dangling_remote_queues"] == []
    assert q["external_partners"] > 0
    assert 0 < q["transmission_queues_inventoried"] < q["transmission_queues_required"]
    assert len(sample_model.links) == q["transmission_queues_required"]
    assert q["apps_on_multiple_qms"] > 100


def test_attributes_detected(sample_inv):
    assert "app_business_domain" in sample_inv.attribute_columns
    assert "app_criticality" in sample_inv.attribute_columns
    assert "persistent" not in sample_inv.attribute_columns


def test_flows_resolve(sample_model):
    fw = sample_model.flow_weights()
    assert len(fw) > 900
    assert all(p in sample_model.apps and c in sample_model.apps for p, c in fw)
    hops = {p.hops for p in sample_model.paths}
    assert hops <= {1, 2, 3, 5, 6}
    assert any(p.consumer is None for p in sample_model.paths)   # outbound to external partners
    assert any(p.producer is None for p in sample_model.paths)   # inbound from external partners


def test_renamed_headers_give_same_model(sample_df, sample_model):
    renamed = sample_df.rename(columns=RENAMES)
    inv = load_inventory(renamed, mapping={"columns": {"app_id": "Application ID", "app_name": "Application Name",
                                                       "alias_target_queue": "Target"}})
    m = build_model(inv.df, inv.attribute_columns)
    assert m.flow_weights() == sample_model.flow_weights()
    assert m.links == sample_model.links
    assert "Portfolio" in inv.attribute_columns
    back = to_original_format(inv, inv.df)
    assert list(back.columns) == list(renamed.columns)


def test_propose_mapping():
    m = propose_mapping(list(RENAMES.values()))
    assert m["queue_manager"] == "QMgr" and m["queue_type"] == "Type" and m["xmit_queue_name"] == "XmitQ"


def test_type_spelling_round_trip(small_df):
    df = small_df.copy()
    df["queue_type"] = df["queue_type"].map({"Local": "QLOCAL", "Remote": "QREMOTE", "Alias": "QALIAS"})
    df["app_role"] = df["app_role"].replace({"Unknown": "TBD"})
    inv = load_inventory(df)
    assert set(inv.df.queue_type) == {"Local", "Remote", "Alias"}
    back = to_original_format(inv, inv.df)
    assert set(back.queue_type) == {"QLOCAL", "QREMOTE", "QALIAS"}
    assert "TBD" in set(back.app_role)


def test_csv_and_xlsx_bytes(small_df):
    csv_bytes = small_df.to_csv(index=False).encode()
    buf = io.BytesIO()
    small_df.to_excel(buf, index=False)
    a = load_inventory(csv_bytes, "x.csv")
    b = load_inventory(buf.getvalue(), "x.xlsx")
    assert len(a.df) == len(b.df)


@pytest.mark.parametrize("bad,msg", [
    (pd.DataFrame({"a": ["1"], "b": ["2"]}), "required column"),
    (pd.DataFrame({"queue_name": ["Q"], "queue_type": ["Weird"], "queue_manager": ["M"], "app_id": ["A"]}),
     "unrecognised queue type"),
])
def test_bad_inputs(bad, msg):
    with pytest.raises(InventoryError, match=msg):
        load_inventory(bad)


def test_unreadable_file():
    with pytest.raises(InventoryError):
        read_table(b"\x00\x01\x02", "x.xlsx")


def test_minimal_columns_only():
    df = pd.DataFrame([
        {"queue_name": "A.B.Q", "queue_type": "Remote", "queue_manager": "QM1", "app_id": "A", "app_role": "Producer",
         "remote_queue_manager": "QM2", "remote_queue_name": "A.B.Q"},
        {"queue_name": "A.B.Q", "queue_type": "Local", "queue_manager": "QM2", "app_id": "B", "app_role": "Consumer"},
    ]).fillna("")
    inv = load_inventory(df)
    m = build_model(inv.df, inv.attribute_columns)
    assert m.flow_weights() == {("A", "B"): 1}
    assert m.links == {("QM1", "QM2")}
    assert m.paths[0].hops == 5 and m.paths[0].verified
