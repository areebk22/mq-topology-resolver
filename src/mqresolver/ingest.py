"""Read an inventory (CSV/XLSX), map it onto the canonical schema, clean it."""
from __future__ import annotations

import fnmatch
import io
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import pandas as pd

REQUIRED = ["queue_name", "queue_type", "queue_manager", "app_id"]
TOPOLOGY = REQUIRED + ["app_name", "app_role", "producer_app_name", "consumer_app_name",
                       "remote_queue_manager", "remote_queue_name", "xmit_queue_name",
                       "alias_target_queue", "queue_usage"]
QUEUE_ATTRS = ["persistent", "put_inhibited", "get_inhibited", "cluster_name"]
ROLE_ORIG = "__role_orig"

SYNONYMS = {
    "queue_name": ["queuename", "queue", "qname"],
    "queue_type": ["queuetype", "qtype", "type"],
    "queue_manager": ["queuemanager", "queuemanagername", "qmgr", "qmgrname", "qm", "qmname"],
    "app_id": ["appid", "applicationid", "appcode"],
    "app_name": ["appname", "applicationname", "appfullname"],
    "app_role": ["approle", "applicationrole", "role"],
    "producer_app_name": ["producerappname", "producername", "producer", "producerapp"],
    "consumer_app_name": ["consumerappname", "consumername", "consumer", "consumerapp"],
    "remote_queue_manager": ["remotequeuemanager", "remoteqmgr", "remoteqmgrname", "rqmname", "remoteqm"],
    "remote_queue_name": ["remotequeuename", "remotequeue", "remoteqname", "rname"],
    "xmit_queue_name": ["xmitqueuename", "xmitqueue", "xmitqname", "xmitq", "transmissionqueue"],
    "alias_target_queue": ["aliastargetqueue", "aliastarget", "targetqueue", "target", "targq"],
    "queue_usage": ["queueusage", "usage"],
}


class InventoryError(ValueError):
    """The file cannot be used as an inventory. The message says why."""


@dataclass
class Inventory:
    df: pd.DataFrame                 # canonical column names, all strings, de-duplicated
    original_columns: list[str]      # header order of the input file
    rename: dict[str, str]           # original header -> canonical name
    type_spelling: dict[str, str]    # canonical queue type -> the input's own spelling
    attribute_columns: list[str]
    quality: dict[str, Any] = field(default_factory=dict)


def _norm(header: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(header).lower())


def read_table(source: Any, filename: Optional[str] = None) -> pd.DataFrame:
    """Read CSV or XLSX from a path or bytes. Every cell is a string, blanks are ''."""
    name = (filename or (str(source) if not isinstance(source, (bytes, bytearray)) else "")).lower()
    data = io.BytesIO(source) if isinstance(source, (bytes, bytearray)) else source
    try:
        if name.endswith((".xlsx", ".xlsm")):
            df = pd.read_excel(data, dtype=str, keep_default_na=False, engine="openpyxl")
        elif name.endswith((".csv", ".txt", "")):
            df = pd.read_csv(data, dtype=str, keep_default_na=False, encoding="utf-8-sig")
        else:
            raise InventoryError(f"unsupported file type: {filename}. Use .csv or .xlsx")
    except InventoryError:
        raise
    except Exception as exc:  # parser errors carry the useful detail
        raise InventoryError(f"could not read the file: {exc}") from exc
    if df.empty or len(df.columns) < 2:
        raise InventoryError("the file has no data rows")
    df.columns = [str(c).strip() for c in df.columns]
    for c in df.columns:                 # column by column, so only one column is copied at a time
        df[c] = df[c].fillna("").astype(str).str.strip()
    return df


def propose_mapping(columns: list[str]) -> dict[str, str]:
    """Best guess of canonical name -> input header."""
    by_norm = {_norm(c): c for c in columns}
    out: dict[str, str] = {}
    for canon in TOPOLOGY:
        if canon in columns:
            out[canon] = canon
            continue
        for cand in [_norm(canon)] + SYNONYMS.get(canon, []):
            if cand in by_norm and by_norm[cand] not in out.values():
                out[canon] = by_norm[cand]
                break
    return out


def _canon_type(v: str) -> str:
    x = _norm(v)
    if x in ("local", "qlocal", "ql", "queuelocal"):
        return "Local"
    if x in ("remote", "qremote", "qr", "queueremote"):
        return "Remote"
    if x in ("alias", "qalias", "qa", "queuealias"):
        return "Alias"
    return v


def _canon_role(v: str) -> str:
    x = _norm(v)
    if x.startswith("produc") or x in ("sender", "putter", "put"):
        return "Producer"
    if x.startswith("consum") or x in ("receiver", "getter", "get"):
        return "Consumer"
    return "Unknown" if v else ""


def _canon_usage(v: str) -> str:
    x = _norm(v)
    if x in ("transmission", "xmitq", "xmit"):
        return "Transmission"
    return "Normal" if x == "normal" else v


def detect_app_attributes(df: pd.DataFrame) -> list[str]:
    """Columns whose value is fixed per application are application attributes."""
    apps = df[df["app_id"] != ""]
    if apps.empty:
        return []
    out = []
    for col in df.columns:
        if col in TOPOLOGY or col in QUEUE_ATTRS or col.startswith("__"):
            continue
        per_app = apps.groupby("app_id")[col].nunique()
        if (per_app <= 1).mean() >= 0.98:
            out.append(col)
    return out


def load_inventory(source: Any, filename: Optional[str] = None,
                   mapping: Optional[dict[str, Any]] = None) -> Inventory:
    raw = source if isinstance(source, pd.DataFrame) else read_table(source, filename)
    raw = raw.fillna("").astype(str)
    original_columns = list(raw.columns)
    mapping = mapping or {}
    cols = dict(propose_mapping(original_columns))
    cols.update({k: v for k, v in (mapping.get("columns") or {}).items() if v})
    missing_headers = [v for v in cols.values() if v not in raw.columns]
    if missing_headers:
        raise InventoryError(f"mapping refers to columns that are not in the file: {missing_headers}")
    missing = [c for c in REQUIRED if c not in cols]
    if missing:
        raise InventoryError(
            f"required column(s) not found: {missing}. Columns in the file: {original_columns}. "
            "Provide a mapping for them.")
    rename = {orig: canon for canon, orig in cols.items()}
    if len(rename) != len(cols):
        raise InventoryError("two canonical columns are mapped to the same input column")
    df = raw.rename(columns=rename)
    for col in TOPOLOGY:
        if col not in df.columns:
            df[col] = ""

    for canon, table in (mapping.get("values") or {}).items():
        if canon in df.columns:
            df[canon] = df[canon].map(
                lambda v, t=table: next((new for pat, new in t.items() if fnmatch.fnmatch(v, pat)), v))

    spelling = (df["queue_type"].groupby(df["queue_type"].map(_canon_type)).agg(lambda s: s.value_counts().index[0])
                .to_dict())
    df[ROLE_ORIG] = df["app_role"]
    df["queue_type"] = df["queue_type"].map(_canon_type)
    df["app_role"] = df["app_role"].map(_canon_role)
    df["queue_usage"] = df["queue_usage"].map(_canon_usage)

    bad_types = sorted(set(df["queue_type"]) - {"Local", "Remote", "Alias"})
    if bad_types:
        raise InventoryError(f"unrecognised queue type value(s): {bad_types[:5]}. Expected Local, Remote or Alias.")
    blank = int(((df["queue_name"] == "") | (df["queue_manager"] == "")).sum())
    if blank:
        df = df[(df["queue_name"] != "") & (df["queue_manager"] != "")]

    before = len(df)
    df = df.drop_duplicates().reset_index(drop=True)
    attrs = mapping.get("attributes")
    if attrs:
        attrs = [rename.get(a, a) for a in attrs]
        unknown = [a for a in attrs if a not in df.columns]
        if unknown:
            raise InventoryError(f"attribute column(s) not in the file: {unknown}")
    else:
        attrs = detect_app_attributes(df)
    attrs = [a for a in attrs if a not in ("app_name",)]

    quality = {
        "rows_in_file": int(len(raw)),
        "rows_missing_queue_or_qm": blank,
        "duplicate_rows_removed": int(before - len(df)),
        "rows_used": int(len(df)),
        "columns": len(original_columns),
    }
    return Inventory(df=df, original_columns=original_columns, rename=rename,
                     type_spelling={k: v for k, v in spelling.items() if k in ("Local", "Remote", "Alias")},
                     attribute_columns=attrs, quality=quality)


def to_original_format(inv: Inventory, df: pd.DataFrame) -> pd.DataFrame:
    """Write a canonical frame back in the input's own headers, order and spellings."""
    out = df.copy()
    out["queue_type"] = out["queue_type"].map(lambda v: inv.type_spelling.get(v, v))
    if ROLE_ORIG in out.columns:
        out["app_role"] = out[ROLE_ORIG]
    back = {canon: orig for orig, canon in inv.rename.items()}
    out = out.rename(columns=back)
    for col in inv.original_columns:
        if col not in out.columns:
            out[col] = ""
    return out[inv.original_columns]


def save_table(df: pd.DataFrame, path: str | Path) -> None:
    path = Path(path)
    if path.suffix.lower() == ".xlsx":
        df.to_excel(path, index=False)
    else:
        df.to_csv(path, index=False, encoding="utf-8")
