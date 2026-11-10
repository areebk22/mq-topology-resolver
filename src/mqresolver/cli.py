"""Command line: mqresolver run | inspect | serve | sample."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml

from . import pipeline, synth


def _mapping(path):
    return yaml.safe_load(Path(path).read_text(encoding="utf-8")) if path else None


def _print_result(b: dict) -> None:
    s, sc = b["summary"], b["score"]
    a, t = s["as_is"], s["target"]
    print(f"\nApplications: {s['apps']}    Profile: {sc['profile']}    Time: {b['seconds']}s\n")
    print(f"{'':30s}{'As-is':>10s}{'Target':>10s}")
    for label, key in (("Queue managers", "queue_managers"), ("Channel pairs", "channel_pairs"),
                       ("Queue definitions", "queue_definitions"), ("Alias queues", "alias_queues"),
                       ("Max apps per QM", "max_apps_per_qm"), ("Apps on several QMs", "apps_on_multiple_qms"),
                       ("Flows staying on one QM %", "same_qm_flow_pct")):
        print(f"{label:30s}{a[key]:>10}{t[key]:>10}")
    print(f"\n{'Topology Complexity Index':30s}{sc['as_is']['tci']:>10}{sc['target']['tci']:>10}   ({sc['change_pct']}%)")
    for k in sc["as_is"]["dimensions"]:
        print(f"  {k:28s}{sc['as_is']['dimensions'][k]:>10.3f}{sc['target']['dimensions'][k]:>10.3f}")
    for x in sc["anchors"]:
        print(f"  anchor: {x['name']:28s} TCI {x['tci']}")
    print(f"\nValidation: {'PASSED' if b['validation']['passed'] else 'FAILED'}")
    for c in b["validation"]["checks"]:
        if not c["passed"]:
            print(f"  FAILED {c['id']}: {c['rule']} ({c['count']})")
            for e in c["examples"][:5]:
                print(f"     - {e}")
    for p in b["policies"]["target"]:
        was = next(x for x in b["policies"]["as_is"] if x["id"] == p["id"])
        print(f"  policy {p['id']:28s} {p['mode']:5s} violations as-is {was['violating']:>5}  target {p['violating']:>5}")
    for w in b["warnings"][:10]:
        print(f"  note: {w}")
    if len(b["warnings"]) > 10:
        print(f"  ... and {len(b['warnings']) - 10} more notes in report.json")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="mqresolver", description="Simplify an IBM MQ topology.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="produce the target topology and report")
    r.add_argument("inventory")
    r.add_argument("--config")
    r.add_argument("--mapping")
    r.add_argument("--out", default="out")
    i = sub.add_parser("inspect", help="show columns, attributes and suggested policies")
    i.add_argument("inventory")
    i.add_argument("--mapping")
    s = sub.add_parser("serve", help="start the web UI")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8000)
    g = sub.add_parser("sample", help="write a synthetic inventory")
    g.add_argument("--out", default="mq_topology_sample.csv")
    g.add_argument("--apps", type=int, default=400)
    g.add_argument("--qms", type=int, default=200)
    g.add_argument("--flows", type=int, default=750)
    g.add_argument("--seed", type=int, default=42)
    a = ap.parse_args(argv)

    try:
        if a.cmd == "run":
            b = pipeline.run(a.inventory, mapping=_mapping(a.mapping), config=a.config)
            out = Path(a.out)
            out.mkdir(parents=True, exist_ok=True)
            for name, text in b.pop("files").items():
                (out / name).write_text(text, encoding="utf-8")
            (out / "decisions.json").write_text(json.dumps(b.pop("decisions"), indent=1), encoding="utf-8")
            graph = b.pop("graph", None)
            if graph:
                (out / "graph.json").write_text(json.dumps(graph), encoding="utf-8")
            (out / "report.json").write_text(json.dumps(b, indent=1), encoding="utf-8")
            _print_result(b)
            print(f"\nOutput written to {out}/")
            return 0 if b["validation"]["passed"] else 2
        if a.cmd == "inspect":
            print(json.dumps(pipeline.inspect(a.inventory, mapping=_mapping(a.mapping)), indent=1))
            return 0
        if a.cmd == "serve":
            from .server import serve
            serve(a.host, a.port)
            return 0
        if a.cmd == "sample":
            cols, rows = synth.generate(a.apps, a.qms, a.flows, a.seed)
            synth.write_csv(a.out, cols, rows)
            print(f"wrote {len(rows)} rows -> {a.out}")
            return 0
    except (ValueError, FileNotFoundError) as exc:      # bad input or infeasible policies: explain, no traceback
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
