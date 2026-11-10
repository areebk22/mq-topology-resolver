"""Generate a realistic, fully synthetic as-is IBM MQ topology inventory.

Standard library only, deterministic for a given seed.
Schema: docs/DATA_SCHEMA.md (canonical, enterprise-neutral column names).

    python scripts/generate_synthetic_dataset.py --out data/sample/mq_topology_sample.csv
"""
import argparse, csv, random, string, zlib

COLS = [
    "queue_name", "queue_type", "queue_manager", "app_id", "app_name", "app_role",
    "producer_app_name", "consumer_app_name",
    "remote_queue_manager", "remote_queue_name", "xmit_queue_name", "alias_target_queue",
    "queue_usage", "persistent", "put_inhibited", "get_inhibited", "cluster_name",
    "app_business_domain", "app_business_unit", "app_criticality", "app_data_classification",
    "app_regulated", "app_internet_facing", "app_hosting", "app_recovery_tier", "app_lifecycle",
]
DOMAINS = ["Sales", "Finance", "Supply Chain", "Customer Service", "Core Platform",
           "Risk & Compliance", "Data & Analytics", "Marketing", "Manufacturing", "HR"]
DOMAIN_W = [31.5, 19.1, 14.5, 8.8, 7.1, 5.6, 4.6, 3.9, 2.2, 1.8]
UNITS = [f"Business Unit {c}" for c in "ABCDEF"]
PURPOSES, PURPOSE_W = ["RQST", "EVT", "DATA", "NOTIFY"], [35, 25, 25, 15]
BUCKETS = [((1, 1), .29), ((2, 2), .19), ((3, 5), .27), ((6, 10), .12), ((11, 20), .09), ((21, 58), .04)]


def generate(apps=400, qms=200, flows=750, seed=42, external_rate=0.08, duplicate_rate=0.015,
             unknown_roles=32, xmitq_inventory=0.6):
    """Return (columns, rows) for a synthetic inventory."""
    class a:  # noqa: N801 - simple namespace for the parameters
        pass
    a.apps, a.qms, a.flows, a.seed = apps, qms, flows, seed
    R = random.Random(a.seed)
    code = lambda n: "".join(R.choices(string.ascii_uppercase + string.digits, k=n))
    pick = lambda xs, ws: R.choices(xs, ws)[0]

    # --- Queue managers: Zipf popularity, a home domain, QM-level cluster membership
    qms = []
    while len(qms) < a.qms:
        q = "QM" + code(4)
        if q not in qms:
            qms.append(q)
    qm_dom = {q: pick(DOMAINS, DOMAIN_W) for q in qms}
    qm_w = {q: 1 / (i + 1) ** 0.6 for i, q in enumerate(qms)}
    qm_cluster = {q: f"CLUSTER_{R.randint(1, 4)}" if R.random() < 0.12 else "" for q in qms}
    gateways = [f"QMGW{i:02d}" for i in (1, 2, 3)]          # DMZ gateways for external partners

    # --- Applications: correlated attributes
    apps = {}
    while len(apps) < a.apps:
        i = code(R.choice([2, 2, 3])) + ("/" + code(2) if R.random() < 0.25 else "")
        if i in apps:
            continue
        dom = pick(DOMAINS, DOMAIN_W)
        crit = pick(["Mission Critical", "Business Critical", "Standard", "Low"], [6, 24, 55, 15])
        mc = crit == "Mission Critical"
        hosting = pick(["On-Prem", "Private Cloud", "Mainframe", "Public Cloud", "SaaS"], [30, 30, 28, 8, 4])
        cloud = hosting in ("Public Cloud", "SaaS")
        # a domain has a main business unit, but units cut across domains
        unit = UNITS[DOMAINS.index(dom) % len(UNITS)] if R.random() < 0.65 else R.choice(UNITS)
        apps[i] = {
            "name": f"App {i} Service",
            "dom": dom if R.random() > 0.01 else "",
            "unit": unit, "crit": crit,
            "dc": pick(["Restricted", "Confidential", "Internal", "Public"], [45, 50, 5, 0] if mc else [8, 60, 27, 5]),
            "reg": "Yes" if R.random() < (0.85 if mc else 0.28) else "No",
            "inet": "Yes" if R.random() < (0.30 if cloud else 0.04) else "No",
            "host": hosting,
            "rto": pick(["Tier 0", "Tier 1", "Tier 2", "Tier 3", "Tier 4"],
                        [60, 40, 0, 0, 0] if mc else [2, 10, 45, 30, 13]),
            "life": "Retiring" if (not mc and R.random() < 0.06) else "Active",
        }
    ids = list(apps)

    # --- Heavy-tailed app -> QM sprawl, 70% inside the app's own domain
    spread = [R.randint(*pick([b for b, _ in BUCKETS], [w for _, w in BUCKETS])) for _ in ids]
    for i, k in zip(ids, spread):
        m = apps[i]
        same = [q for q in qms if qm_dom[q] == m["dom"]] or qms
        pool = set()
        while len(pool) < min(k, len(qms)):
            src = same if R.random() < 0.7 else qms
            pool.add(pick(src, [qm_w[q] for q in src]))
        m["qms"] = sorted(pool, key=lambda q: -qm_w[q])     # most popular first = gateway QM

    # --- Directed app flows: hub producers AND hub consumers (sinks), 70% intra-domain
    prod_w = [1 / (r + 1) ** 0.9 for r in range(len(ids))]
    sink_order = ids[:]; R.shuffle(sink_order)
    sink_w = {i: 1 / (r + 1) ** 0.7 for r, i in enumerate(sink_order)}
    flows = set()
    while len(flows) < a.flows - 34:
        p = pick(ids, prod_w)
        cands = [c for c in ids if c != p and apps[c]["dom"] == apps[p]["dom"]] if R.random() < 0.7 else ids
        cands = [c for c in cands if c != p]
        if cands:
            flows.add((p, pick(cands, [sink_w[c] for c in cands])))
    flows |= {(i, i) for i in R.sample(ids, 34)}            # apps with internal work queues

    rows = []

    def row(app, role, qm, qname, qtype, prod, cons, persistent, rqm="", rq="", target="", usage=None):
        m = apps.get(app, {})
        local = qtype == "Local"
        return {
            "queue_name": qname, "queue_type": qtype, "queue_manager": qm,
            "app_id": app, "app_name": m.get("name", ""), "app_role": role,
            "producer_app_name": prod, "consumer_app_name": cons,
            "remote_queue_manager": rqm, "remote_queue_name": rq,
            "xmit_queue_name": rqm if rqm and R.random() < 0.96 else "",
            "alias_target_queue": target,
            "queue_usage": usage or ("Normal" if local else ""),
            "persistent": persistent,
            "put_inhibited": "Yes" if R.random() < 0.003 else "No",
            "get_inhibited": "" if qtype == "Remote" else "No",
            "cluster_name": qm_cluster.get(qm, "") if local and usage is None and R.random() < 0.6 else "",
            "app_business_domain": m.get("dom", ""), "app_business_unit": m.get("unit", ""),
            "app_criticality": m.get("crit", ""), "app_data_classification": m.get("dc", ""),
            "app_regulated": m.get("reg", ""), "app_internet_facing": m.get("inet", ""),
            "app_hosting": m.get("host", ""), "app_recovery_tier": m.get("rto", ""),
            "app_lifecycle": m.get("life", ""),
        }

    def emit(p, c, qname, pn, cn, external_out=False, external_in=False, via=None):
        """One queue carrying messages p -> c, with every MQ object it needs."""
        pers = "Yes" if R.random() < 0.83 else "No"          # one value per queue, end to end
        shared = [q for q in apps[p]["qms"] if q in apps[c]["qms"]]
        if external_out:                                     # partner pulls from a DMZ gateway
            gw = gateways[zlib.crc32(cn.encode()) % 3]
            rows.append(row(p, "Producer", apps[p]["qms"][0], qname, "Remote", pn, cn, pers, gw, qname))
            return None
        if via:                                              # replies return over the request's QM pair
            pqm, cqm = via
        elif shared and R.random() < 0.5:
            pqm = cqm = R.choice(shared)
        elif R.random() < 0.45:                              # sprawl: whichever QM is handy
            pqm = R.choice(apps[p]["qms"])
            cqm = R.choice(apps[c]["qms"]) if R.random() < 0.35 else apps[c]["qms"][0]
        else:                                                # routed through each side's main QM
            pqm, cqm = apps[p]["qms"][0], apps[c]["qms"][0]
        alias = qname + ".AL"
        cons_alias = R.random() < 0.25
        # consumer side: the real local queue, optionally read through an alias
        rows.append(row(c, "Consumer", cqm, qname, "Local", pn, cn, pers))
        if cons_alias:
            rows.append(row(c, "Consumer", cqm, alias, "Alias", pn, cn, pers, target=qname))
        if external_in:
            return None
        # producer side
        if pqm != cqm:
            rows.append(row(p, "Producer", pqm, qname, "Remote", pn, cn, pers, cqm, qname))
        elif R.random() < 0.4:
            rows.append(row(p, "Producer", pqm, alias, "Alias", pn, cn, pers, target=qname))
        else:
            rows.append(row(p, "Producer", pqm, qname, "Local", pn, cn, pers))
        return pqm, cqm

    for p, c in sorted(flows):
        pn, cn = apps[p]["name"], apps[c]["name"]
        x = R.random()
        ext_out, ext_in = x < external_rate * 0.625, external_rate * 0.625 <= x < external_rate
        partner = f"External Partner {code(4)}"
        for _ in range(R.randint(1, 8)):
            purpose, sfx = pick(PURPOSES, PURPOSE_W), code(3)
            if ext_out:
                emit(p, c, f"{p}.EXT.{purpose}.{sfx}", pn, partner, external_out=True)
            elif ext_in:
                emit(c, c, f"EXT.{c}.{purpose}.{sfx}", partner, cn, external_in=True)
            else:
                pq, cq = emit(p, c, f"{p}.{c}.{purpose}.{sfx}", pn, cn)
                if purpose == "RQST" and p != c and R.random() < 0.8:      # reply path back
                    emit(c, p, f"{c}.{p}.RPLY.{sfx}", cn, pn, via=(cq, pq))

    seen = {r["app_id"] for r in rows}
    for i in sorted(set(ids) - seen):                        # every app appears at least once
        rows.append(row(i, "Consumer", apps[i]["qms"][0], f"{i}.LOCAL.DATA.{code(3)}", "Local",
                        apps[i]["name"], apps[i]["name"], "Yes"))

    # --- Transmission queues: infrastructure rows (no owning app); inventories are rarely complete
    links = sorted({(r["queue_manager"], r["xmit_queue_name"]) for r in rows if r["xmit_queue_name"]})
    for src, dst in links:
        if R.random() < xmitq_inventory:
            rows.append(row("", "", src, dst, "Local", "", "", "Yes", usage="Transmission"))

    # --- Inventory mess: unknown roles and exact duplicates
    app_rows = [r for r in rows if r["app_id"]]
    for r in R.sample(app_rows, min(unknown_roles, len(app_rows))):
        r["app_role"] = "Unknown"
    rows += [dict(r) for r in R.sample(rows, int(len(rows) * duplicate_rate))]
    R.shuffle(rows)

    return COLS, rows


def write_csv(path, cols, rows):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--apps", type=int, default=400)
    ap.add_argument("--qms", type=int, default=200)
    ap.add_argument("--flows", type=int, default=750, help="primary directed app flows (replies add more)")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--out", default="mq_topology_sample.csv")
    a = ap.parse_args(argv)
    cols, rows = generate(a.apps, a.qms, a.flows, a.seed)
    write_csv(a.out, cols, rows)
    print(f"wrote {len(rows)} rows x {len(cols)} columns -> {a.out}")


if __name__ == "__main__":
    main()
