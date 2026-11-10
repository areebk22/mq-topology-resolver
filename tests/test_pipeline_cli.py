import json

import pandas as pd
import pytest

from mqresolver import cli, pipeline
from mqresolver.ingest import read_table


@pytest.fixture(scope="module")
def small_bundle(small_df):
    return pipeline.run(small_df)


def test_run_is_valid_and_better(small_bundle):
    b = small_bundle
    assert b["validation"]["passed"], [c for c in b["validation"]["checks"] if not c["passed"]]
    assert b["score"]["anchors_ok"]
    assert b["score"]["target"]["tci"] < b["score"]["as_is"]["tci"]
    assert b["summary"]["target"]["apps_on_multiple_qms"] == 0
    assert b["summary"]["target"]["alias_queues"] == 0
    assert len(b["decisions"]) == b["summary"]["apps"]
    assert all(d["reason"] for d in b["decisions"])


def test_target_file_keeps_input_format(small_df, small_bundle):
    out = pd.read_csv(pd.io.common.StringIO(small_bundle["files"]["target_topology.csv"]), dtype=str,
                      keep_default_na=False)
    assert list(out.columns) == list(small_df.columns)
    assert set(out["queue_type"]) <= {"Local", "Remote"}


def test_run_is_deterministic(small_df, small_bundle):
    again = pipeline.run(small_df)
    assert again["files"] == small_bundle["files"]
    assert again["score"] == small_bundle["score"]


def test_mapping_round_trip(small_df, small_bundle):
    """An export with its own headers and spellings gives the same answer, written back in its own format."""
    headers = {"queue_name": "Queue", "queue_type": "Type", "queue_manager": "QMgr", "app_id": "App Code",
               "app_role": "Application Role", "app_business_domain": "Portfolio"}
    df = small_df.rename(columns=headers)
    df["Type"] = df["Type"].map({"Local": "QLOCAL", "Remote": "QREMOTE", "Alias": "QALIAS"})
    df["Application Role"] = df["Application Role"].replace({"Unknown": "TBD-1"})
    mapping = {"columns": {"queue_name": "Queue", "queue_type": "Type", "queue_manager": "QMgr",
                           "app_id": "App Code", "app_role": "Application Role"}}
    b = pipeline.run(df, mapping=mapping)
    assert b["validation"]["passed"]
    assert b["score"]["target"]["tci"] == small_bundle["score"]["target"]["tci"]
    out = pd.read_csv(pd.io.common.StringIO(b["files"]["target_topology.csv"]), dtype=str, keep_default_na=False)
    assert list(out.columns) == list(df.columns)
    assert set(out["Type"]) <= {"QLOCAL", "QREMOTE"}


def test_inspect_suggests_policies(small_df):
    info = pipeline.inspect(small_df)
    assert info["ready"]
    assert info["proposed_mapping"]["queue_manager"] == "queue_manager"
    types = {p["type"] for p in info["suggested_policies"]}
    assert {"capacity", "homogeneous"} <= types
    assert "app_criticality" in info["attributes"]


def test_inspect_explains_unusable_file():
    info = pipeline.inspect(pd.DataFrame({"a": ["1"], "b": ["2"]}))
    assert not info["ready"]
    assert "required column" in info["problem"]


def test_graph_payload(small_bundle):
    g = small_bundle["graph"]
    for side in ("as_is", "target"):
        nodes = g[side]["nodes"]
        ids = {n["id"] for n in nodes}
        assert all(0 <= n["x"] <= 1 and 0 <= n["y"] <= 1 for n in nodes)
        assert all(e["s"] in ids and e["t"] in ids for e in g[side]["edges"])
    assert len(g["target"]["nodes"]) < len(g["as_is"]["nodes"])
    assert {n["status"] for n in g["as_is"]["nodes"]} <= {"kept", "retired", ""}


def test_cli_run_writes_outputs(tmp_path, small_df):
    src = tmp_path / "inv.csv"
    small_df.to_csv(src, index=False)
    code = cli.main(["run", str(src), "--out", str(tmp_path / "out")])
    assert code == 0
    for name in ("target_topology.csv", "target_channels.csv", "target.mqsc", "report.json", "decisions.json",
                 "graph.json"):
        assert (tmp_path / "out" / name).exists(), name
    report = json.loads((tmp_path / "out" / "report.json").read_text())
    assert report["validation"]["passed"]


def test_cli_xlsx_and_config(tmp_path, small_df):
    src = tmp_path / "inv.xlsx"
    small_df.to_excel(src, index=False)
    cfg = tmp_path / "cfg.yaml"
    cfg.write_text("profile: resilient\npolicies:\n  - {type: capacity, max_apps_per_qm: 10}\n")
    assert cli.main(["run", str(src), "--config", str(cfg), "--out", str(tmp_path / "o")]) == 0
    assert len(read_table(tmp_path / "o" / "target_topology.csv")) > 0


def test_cli_reports_bad_input_without_traceback(tmp_path, capsys):
    bad = tmp_path / "bad.csv"
    bad.write_text("x,y\n1,2\n")
    assert cli.main(["run", str(bad), "--out", str(tmp_path / "o")]) == 1
    assert "required column" in capsys.readouterr().err


def test_cli_reports_infeasible_policies(tmp_path, small_df, capsys):
    src = tmp_path / "inv.csv"
    small_df.to_csv(src, index=False)
    cfg = tmp_path / "cfg.yaml"
    cfg.write_text("policies:\n  - {type: isolate, when: {app_regulated: 'Yes'}}\n  - {type: qm_count, max: 3}\n")
    assert cli.main(["run", str(src), "--config", str(cfg), "--out", str(tmp_path / "o")]) == 1
    assert "queue managers" in capsys.readouterr().err


def test_cli_sample(tmp_path):
    out = tmp_path / "s.csv"
    assert cli.main(["sample", "--out", str(out), "--apps", "50", "--qms", "20", "--flows", "80"]) == 0
    assert len(read_table(out)) > 100
