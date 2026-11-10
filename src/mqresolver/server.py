"""HTTP API and static UI (Starlette + uvicorn).

Stateless: uploads are parsed in memory, never written to disk and never logged.
"""
from __future__ import annotations

import json
import os
import threading
from functools import lru_cache
from pathlib import Path
from typing import Any

import pandas as pd
import yaml
from pydantic import ValidationError
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.middleware import Middleware
from starlette.middleware.gzip import GZipMiddleware
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from . import __version__, pipeline, synth

WEB = Path(__file__).resolve().parent / "web"
MAX_UPLOAD_MB = float(os.environ.get("MQR_MAX_UPLOAD_MB", "25"))
MAX_PARALLEL = int(os.environ.get("MQR_MAX_PARALLEL_RUNS", "2"))
_slots = threading.BoundedSemaphore(MAX_PARALLEL)


class BadRequest(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


@lru_cache(maxsize=1)
def sample_frame() -> pd.DataFrame:
    """The bundled synthetic estate (deterministic, contains no real data)."""
    cols, rows = synth.generate()
    return pd.DataFrame(rows, columns=cols).astype(str)


def _parse_json_or_yaml(text: str, what: str) -> Any:
    if not text or not text.strip():
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise BadRequest(f"{what} is neither valid JSON nor valid YAML: {exc}") from exc


async def _read_form(request: Request) -> dict:
    """File (or the sample), mapping and config from a multipart form."""
    length = request.headers.get("content-length")
    if length and int(length) > MAX_UPLOAD_MB * 1024 * 1024 + 1_000_000:
        raise BadRequest(f"upload is larger than the {MAX_UPLOAD_MB:g} MB limit", 413)
    try:
        form = await request.form(max_files=1, max_fields=10, max_part_size=int(MAX_UPLOAD_MB * 1024 * 1024))
    except Exception as exc:                     # malformed or oversized multipart body
        raise BadRequest(f"could not read the upload: {exc}") from exc
    out: dict[str, Any] = {"mapping": _parse_json_or_yaml(str(form.get("mapping") or ""), "mapping"),
                           "config": _parse_json_or_yaml(str(form.get("config") or ""), "config")}
    upload = form.get("file")
    if str(form.get("sample") or "").lower() in ("1", "true", "yes"):
        out["source"], out["filename"] = sample_frame(), "sample.csv"
    elif upload is not None and hasattr(upload, "read"):
        name = upload.filename or "upload.csv"
        if not name.lower().endswith((".csv", ".xlsx", ".xlsm", ".txt")):
            raise BadRequest("upload a .csv or .xlsx file")
        data = await upload.read()
        if len(data) > MAX_UPLOAD_MB * 1024 * 1024:
            raise BadRequest(f"upload is larger than the {MAX_UPLOAD_MB:g} MB limit", 413)
        if not data:
            raise BadRequest("the uploaded file is empty")
        out["source"], out["filename"] = data, name
    else:
        raise BadRequest("send a file in the 'file' field, or sample=1 to use the bundled sample")
    await form.close()
    return out


def _guarded(fn, *args, **kwargs):
    if not _slots.acquire(timeout=1):
        raise BadRequest("the server is busy with other runs; try again in a moment", 503)
    try:
        return fn(*args, **kwargs)
    finally:
        _slots.release()


def _error(exc: Exception) -> JSONResponse:
    if isinstance(exc, BadRequest):
        return JSONResponse({"error": str(exc)}, status_code=exc.status)
    if isinstance(exc, ValidationError):
        msgs = [f"{'.'.join(str(p) for p in e['loc']) or 'config'}: {e['msg']}" for e in exc.errors()]
        return JSONResponse({"error": "invalid configuration: " + "; ".join(msgs[:5])}, status_code=400)
    if isinstance(exc, (ValueError, KeyError)):          # InventoryError, PolicyError and friends
        return JSONResponse({"error": str(exc).strip("'\"")}, status_code=400)
    return JSONResponse({"error": "unexpected server error"}, status_code=500)


async def health(request: Request) -> Response:
    return JSONResponse({"status": "ok", "version": __version__, "ui": (WEB / "index.html").exists(),
                         "max_upload_mb": MAX_UPLOAD_MB})


async def sample(request: Request) -> Response:
    csv = sample_frame().to_csv(index=False, lineterminator="\n")
    return Response(csv, media_type="text/csv",
                    headers={"Content-Disposition": 'attachment; filename="mq_topology_sample.csv"'})


async def inspect(request: Request) -> Response:
    try:
        f = await _read_form(request)
        info = await run_in_threadpool(_guarded, pipeline.inspect, f["source"], f["filename"], f["mapping"])
        return JSONResponse(info)
    except Exception as exc:  # noqa: BLE001 - every failure becomes a readable JSON error
        return _error(exc)


async def run(request: Request) -> Response:
    try:
        f = await _read_form(request)
        bundle = await run_in_threadpool(_guarded, pipeline.run, f["source"], f["filename"], f["mapping"],
                                         f["config"])
        return JSONResponse(bundle)
    except Exception as exc:  # noqa: BLE001
        return _error(exc)


async def index(request: Request) -> Response:
    page = WEB / "index.html"
    if not page.exists():
        return JSONResponse({"error": "UI not built into this package"}, status_code=404)
    return FileResponse(page)


class SecurityHeaders:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                h = message.setdefault("headers", [])
                h += [(b"x-content-type-options", b"nosniff"), (b"referrer-policy", b"no-referrer"),
                      (b"x-frame-options", b"DENY"),
                      (b"content-security-policy",
                       b"default-src 'self'; img-src 'self' data: blob:; style-src 'self' 'unsafe-inline'")]
            await send(message)
        await self.app(scope, receive, send_with_headers if scope["type"] == "http" else send)


def create_app() -> Starlette:
    routes = [
        Route("/api/health", health),
        Route("/api/sample", sample),
        Route("/api/inspect", inspect, methods=["POST"]),
        Route("/api/run", run, methods=["POST"]),
        Route("/", index),
    ]
    if WEB.exists():
        routes.append(Mount("/", StaticFiles(directory=WEB), name="web"))
    return Starlette(routes=routes, middleware=[Middleware(GZipMiddleware, minimum_size=2048, compresslevel=6),
                                                Middleware(SecurityHeaders)])


app = create_app()


def serve(host: str = "127.0.0.1", port: int = 8000) -> None:
    import uvicorn
    uvicorn.run(app, host=host, port=int(os.environ.get("PORT", port)), log_level="info", access_log=False)
