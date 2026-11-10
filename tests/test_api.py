"""API tests against a real uvicorn server on a free port."""
import gzip
import json
import socket
import threading
import time
import urllib.error
import urllib.request
import uuid

import pytest
import uvicorn

from mqresolver import server


@pytest.fixture(scope="module")
def base_url():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    srv = uvicorn.Server(uvicorn.Config(server.app, host="127.0.0.1", port=port, log_level="warning"))
    t = threading.Thread(target=srv.run, daemon=True)
    t.start()
    for _ in range(100):
        if srv.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}"
    srv.should_exit = True
    t.join(5)


def multipart(fields: dict, files: dict | None = None) -> tuple[bytes, str]:
    boundary = uuid.uuid4().hex
    out = b""
    for k, v in fields.items():
        out += f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'.encode()
    for k, (name, data) in (files or {}).items():
        out += (f'--{boundary}\r\nContent-Disposition: form-data; name="{k}"; filename="{name}"\r\n'
                "Content-Type: application/octet-stream\r\n\r\n").encode() + data + b"\r\n"
    out += f"--{boundary}--\r\n".encode()
    return out, f"multipart/form-data; boundary={boundary}"


def call(base, path, fields=None, files=None, gz=False):
    if fields is None and files is None:
        req = urllib.request.Request(base + path)
    else:
        body, ctype = multipart(fields or {}, files)
        req = urllib.request.Request(base + path, data=body, headers={"Content-Type": ctype})
    if gz:
        req.add_header("Accept-Encoding", "gzip")
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            raw = r.read()
            if r.headers.get("Content-Encoding") == "gzip":
                raw = gzip.decompress(raw)
            return r.status, dict(r.headers), raw
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


@pytest.fixture(scope="module")
def small_csv(small_df):
    return small_df.to_csv(index=False).encode()


def test_health(base_url):
    code, headers, body = call(base_url, "/api/health")
    assert code == 200 and json.loads(body)["status"] == "ok"
    assert headers["x-content-type-options"] == "nosniff"


def test_sample_download(base_url):
    code, headers, body = call(base_url, "/api/sample")
    assert code == 200 and "text/csv" in headers["content-type"]
    assert body.decode().splitlines()[0].startswith("queue_name,queue_type,queue_manager")


def test_inspect_upload(base_url, small_csv):
    code, _, body = call(base_url, "/api/inspect", files={"file": ("inv.csv", small_csv)})
    info = json.loads(body)
    assert code == 200 and info["ready"]
    assert any(p["type"] == "homogeneous" for p in info["suggested_policies"])


def test_run_upload_with_config(base_url, small_csv):
    cfg = json.dumps({"profile": "resilient", "policies": [{"type": "capacity", "max_apps_per_qm": 10}]})
    code, headers, body = call(base_url, "/api/run", {"config": cfg}, {"file": ("inv.csv", small_csv)}, gz=True)
    assert code == 200, body[:300]
    assert headers.get("content-encoding") == "gzip"
    b = json.loads(body)
    assert b["validation"]["passed"] and b["score"]["profile"] == "resilient"
    assert {"as_is", "target"} <= set(b["graph"])


def test_run_sample_with_yaml_config(base_url):
    cfg = "policies:\n  - {type: capacity, max_apps_per_qm: 30}\n  - {type: isolate, when: {app_criticality: Mission Critical}}\n"
    code, _, body = call(base_url, "/api/run", {"sample": "1", "config": cfg})
    assert code == 200, body[:300]
    b = json.loads(body)
    assert b["summary"]["apps"] == 400 and b["validation"]["passed"]


def test_xlsx_with_mapping(base_url, small_df):
    import io
    df = small_df.rename(columns={"queue_manager": "QMgr", "queue_type": "Type"})
    buf = io.BytesIO()
    df.to_excel(buf, index=False)
    mapping = json.dumps({"columns": {"queue_manager": "QMgr", "queue_type": "Type"}})
    code, _, body = call(base_url, "/api/run", {"mapping": mapping}, {"file": ("inv.xlsx", buf.getvalue())})
    assert code == 200, body[:300]
    assert json.loads(body)["files"]["target_topology.csv"].split("\n")[0].split(",")[1:3] == ["Type", "QMgr"]


@pytest.mark.parametrize("fields,files,status,needle", [
    ({}, None, 400, "send a file"),
    ({}, {"file": ("inv.exe", b"MZ")}, 400, ".csv or .xlsx"),
    ({}, {"file": ("inv.csv", b"")}, 400, "empty"),
    ({}, {"file": ("inv.csv", b"a,b\n1,2\n")}, 400, "required column"),
    ({"sample": "1", "config": "{\"profile\": \"fastest\"}"}, None, 400, "invalid configuration"),
    ({"sample": "1", "config": "policies: [{type: homogeneous, attribute: app_colour}]"}, None, 400, "not in the inventory"),
    ({"sample": "1", "config": "policies:\n - {type: isolate, when: {app_regulated: 'Yes'}}\n - {type: qm_count, max: 4}"},
     None, 400, "cannot reach the hard limit"),
    ({"sample": "1", "config": "{not: [valid"}, None, 400, "neither valid JSON nor valid YAML"),
])
def test_errors_are_readable(base_url, fields, files, status, needle):
    code, _, body = call(base_url, "/api/run", fields, files)
    assert code == status
    assert needle in json.loads(body)["error"]


def test_oversized_upload_is_rejected(base_url, monkeypatch):
    monkeypatch.setattr(server, "MAX_UPLOAD_MB", 0.01)
    code, _, body = call(base_url, "/api/run", files={"file": ("inv.csv", b"x" * 50_000)})
    assert code in (400, 413)
    assert "limit" in json.loads(body)["error"] or "upload" in json.loads(body)["error"]


def test_get_on_run_not_allowed(base_url):
    code, _, _ = call(base_url, "/api/run")
    assert code == 405
