"""Build the target-state objects from an app -> QM assignment."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

import pandas as pd

from .config import Config
from .ingest import QUEUE_ATTRS, ROLE_ORIG, Inventory
from .model import Model, queue_key
from .optimise import Result


@dataclass
class Target:
    df: pd.DataFrame                       # canonical columns, same set as the inventory frame
    channels: list[dict]
    mqsc: str
    warnings: list[str] = field(default_factory=list)
    renamed: list[dict] = field(default_factory=list)


def _claim(registry: dict, qm: str, name: str, owner, suffix: str) -> str:
    """Reserve a queue name on a QM. A different object already using it forces a new name."""
    cand, n = name, 1
    while registry.get((qm, cand), owner) != owner:
        n += 1
        cand = f"{name}.{suffix}" if n == 2 else f"{name}.{suffix}{n - 1}"
    registry[(qm, cand)] = owner
    return cand


def generate(inv: Inventory, model: Model, result: Result, config: Config) -> Target:
    assign, gw = result.assignment, result.gateway_map
    recs = [r for r in inv.df.to_dict("records") if r["app_id"] and r.get("queue_usage") != "Transmission"]
    warnings: list[str] = []
    renamed: list[dict] = []
    keep_alias = not config.target.eliminate_aliases
    uses_usage = bool((inv.df["queue_usage"] != "").any())
    has = set(inv.df.columns)

    # ---- attributes of the real (local) queue behind each logical queue -------
    local_attrs: dict[tuple, dict] = {}
    for r in recs:
        if r["queue_type"] == "Local":
            local_attrs.setdefault((r["queue_manager"], r["queue_name"]), {c: r[c] for c in QUEUE_ATTRS if c in has})

    # ---- home QM and name of every logical queue --------------------------------
    registry: dict[tuple[str, str], object] = {}
    home: dict[tuple, str] = {}
    tname: dict[tuple, str] = {}
    copies: dict[tuple, set[str]] = defaultdict(set)       # QMs holding a local definition of the queue
    for key in sorted(model.queues):
        lq = model.queues[key]
        consumers = sorted(lq.consumers, key=lambda a: (-len(lq.consumers[a]), a)) + sorted(lq.inferred_consumers)
        if consumers:
            home[key] = assign[consumers[0]]
            elsewhere = sorted({assign[a] for a in lq.consumers} - {home[key]})
            if elsewhere:
                warnings.append(
                    f"queue {lq.name}: consumers end up on different queue managers ({home[key]} and "
                    f"{', '.join(elsewhere)}); producers deliver to {home[key]} only. Co-locate these apps.")
        elif lq.ext_consumers and key[0] not in model.qm_apps:
            home[key] = gw.get(key[0], key[0])                 # stays on the partner-facing gateway
        else:
            owners = sorted(lq.producers) or sorted(lq.inferred_producers) or sorted(lq.others)
            home[key] = assign[owners[0]]
        tname[key] = _claim(registry, home[key], lq.name, key, key[0])
        if tname[key] != lq.name:
            renamed.append({"queue": lq.name, "was_on": key[0], "now": tname[key], "on": home[key]})
        if home[key] in set(assign.values()):
            copies[key].add(home[key])
        for a in lq.consumers:                                  # split consumers keep a local copy
            if assign[a] != home[key]:
                copies[key].add(assign[a])
                registry[(assign[a], tname[key])] = key
    if renamed:
        warnings.append(f"{len(renamed)} queue(s) were renamed because two different queues with the same "
                        "name landed on one queue manager.")

    # ---- rows ---------------------------------------------------------------------
    out: list[dict] = []
    seen: set = set()
    links: set[tuple[str, str]] = set()

    def emit(r: dict, **over) -> None:
        row = dict(r)
        row.update(over)
        if "cluster_name" in has:
            row["cluster_name"] = ""
        k = (row["queue_manager"], row["queue_name"], row["queue_type"], row["app_id"], row.get(ROLE_ORIG, ""))
        if k not in seen:
            seen.add(k)
            out.append(row)

    def local_row(r, qm, key, name):
        attrs = {} if r["queue_type"] == "Local" else {c: v for c, v in local_attrs.get(key, {}).items()
                                                         if c == "get_inhibited"}
        emit(r, queue_manager=qm, queue_name=name, queue_type="Local", remote_queue_manager="",
             remote_queue_name="", xmit_queue_name="", alias_target_queue="",
             queue_usage="Normal" if uses_usage else "", **attrs)

    def remote_row(r, qm, key):
        dst = home[key]
        name = _claim(registry, qm, tname[key], ("remote", key), dst)
        extra = {"get_inhibited": ""} if "get_inhibited" in has else {}
        emit(r, queue_manager=qm, queue_name=name, queue_type="Remote", remote_queue_manager=dst,
             remote_queue_name=tname[key], xmit_queue_name=dst, alias_target_queue="", queue_usage="", **extra)
        links.add((qm, dst))
        return name

    for r in recs:
        key = queue_key(r)
        if key not in model.queues:
            continue
        a, qm = r["app_id"], assign[r["app_id"]]
        role = r["app_role"]
        is_alias = keep_alias and r["queue_type"] == "Alias"
        if role == "Producer" and qm not in copies[key]:
            obj = remote_row(r, qm, key)                       # consumer lives on another QM
        else:
            if qm not in copies[key]:
                # an app with an unknown role keeps its own definition on its QM
                copies[key].add(qm)
                registry.setdefault((qm, tname[key]), key)
                warnings.append(f"app {a} has an unknown role on queue {tname[key]}; it keeps a local "
                                f"definition on {qm} that no flow feeds. Review it.")
            obj = tname[key]
            local_row(r, qm, key, obj)
        if is_alias:                                           # keep the alias, pointing at the object above
            wanted = r["queue_name"] if r["queue_name"] != obj else f"{obj}.AL"
            alias = _claim(registry, qm, wanted, ("alias", key, obj), "AL")
            emit(r, queue_manager=qm, queue_name=alias, queue_type="Alias", remote_queue_manager="",
                 remote_queue_name="", xmit_queue_name="", alias_target_queue=obj, queue_usage="")

    # ---- transmission queues and channels ---------------------------------------------
    blank = {c: "" for c in inv.df.columns}
    for src, dst in sorted(links):
        row = dict(blank, queue_name=dst, queue_type="Local", queue_manager=src, queue_usage="Transmission")
        if "persistent" in has:
            row["persistent"] = "Yes"
        out.append(row)
    hosting = set(assign.values())
    channels = []
    for src, dst in sorted(links):
        name = config.target.channel_template.format(src=src, dst=dst)
        if len(name) > 20:
            warnings.append(f"channel name {name} is longer than MQ's 20-character limit; adjust the template.")
        external = dst not in hosting
        channels.append({"channel_name": name, "channel_type": "Sender", "queue_manager": src,
                         "partner_queue_manager": dst, "xmit_queue": dst, "partner_is_external": external})
        channels.append({"channel_name": name, "channel_type": "Receiver", "queue_manager": dst,
                         "partner_queue_manager": src, "xmit_queue": "", "partner_is_external": external})

    df = pd.DataFrame(out, columns=list(inv.df.columns)).fillna("")
    df = df.sort_values(["queue_manager", "queue_name", "app_id", "queue_type"], kind="stable").reset_index(drop=True)
    return Target(df=df, channels=channels, mqsc=_mqsc(df, channels), warnings=_dedupe(warnings), renamed=renamed)


def _dedupe(items: list[str]) -> list[str]:
    return list(dict.fromkeys(items))


def _mqsc(df: pd.DataFrame, channels: list[dict]) -> str:
    """runmqsc commands, grouped by queue manager."""
    per_qm: dict[str, list[str]] = defaultdict(list)
    done: set = set()
    yes = {"yes", "y", "true", "1"}
    for r in df.to_dict("records"):
        k = (r["queue_manager"], r["queue_name"])
        if k in done:
            continue
        done.add(k)
        pers = ""
        if r.get("persistent"):
            pers = f" DEFPSIST({'YES' if r['persistent'].lower() in yes else 'NO'})"
        n = r["queue_name"]
        if r.get("queue_usage") == "Transmission":
            cmd = f"DEFINE QLOCAL('{n}') USAGE(XMITQ){pers} REPLACE"
        elif r["queue_type"] == "Local":
            cmd = f"DEFINE QLOCAL('{n}'){pers} REPLACE"
        elif r["queue_type"] == "Remote":
            cmd = (f"DEFINE QREMOTE('{n}') RNAME('{r['remote_queue_name']}') RQMNAME('{r['remote_queue_manager']}') "
                   f"XMITQ('{r['xmit_queue_name']}'){pers} REPLACE")
        else:
            cmd = f"DEFINE QALIAS('{n}') TARGET('{r['alias_target_queue']}'){pers} REPLACE"
        per_qm[r["queue_manager"]].append(cmd)
    for c in channels:
        if c["channel_type"] == "Sender":
            cmd = (f"DEFINE CHANNEL('{c['channel_name']}') CHLTYPE(SDR) TRPTYPE(TCP) "
                   f"CONNAME('<host-of-{c['partner_queue_manager']}>(1414)') XMITQ('{c['xmit_queue']}') REPLACE")
        else:
            cmd = f"DEFINE CHANNEL('{c['channel_name']}') CHLTYPE(RCVR) TRPTYPE(TCP) REPLACE"
        per_qm[c["queue_manager"]].append(cmd)
    lines = ["* Generated target-state definitions. Review before running with runmqsc.",
             "* Replace every <host-of-QM> with the listener address of that queue manager.", ""]
    for qm in sorted(per_qm):
        lines += [f"* ===== Queue manager {qm} =====", *per_qm[qm], ""]
    return "\n".join(lines)
