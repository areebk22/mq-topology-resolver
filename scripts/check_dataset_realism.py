"""Sanity-check an MQ inventory CSV (canonical schema) against how real MQ estates behave.

    python scripts/check_dataset_realism.py data/sample/mq_topology_sample.csv
"""
import sys, collections
import pandas as pd

path = sys.argv[1] if len(sys.argv) > 1 else "data/sample/mq_topology_sample.csv"
df = pd.read_csv(path, dtype=str, keep_default_na=False)
d = df.drop_duplicates()
appd = d[d.app_id != ""]
print(f"rows {len(df)} (dedup {len(d)}), cols {df.shape[1]}, exact duplicates {len(df) - len(d)}")
print(f"apps {appd.app_id.nunique()}, QMs {d.queue_manager.nunique()}, queue names {d.queue_name.nunique()}")
print("queue types %:", (appd.queue_type.value_counts(normalize=True) * 100).round(1).to_dict())

# 1. one queue namespace per QM
g = d.groupby(["queue_manager", "queue_name"]).queue_type.nunique()
print(f"\n[1] (QM, name) defined with 2+ types (must be 0): {(g > 1).sum()}")

# 2. aliases resolve to a local queue on the same QM
loc = set(zip(d[d.queue_type == "Local"].queue_manager, d[d.queue_type == "Local"].queue_name))
al = d[d.queue_type == "Alias"]
ok = sum((q, t) in loc for q, t in zip(al.queue_manager, al.alias_target_queue))
print(f"[2] alias rows {len(al)}, target resolves: {ok} ({ok / max(len(al), 1):.0%})")

# 3. remote queues resolve to a local queue on the remote QM
rem = d[d.queue_type == "Remote"]
res = [(m, q) in loc for m, q in zip(rem.remote_queue_manager, rem.remote_queue_name)]
gw = rem.remote_queue_manager.str.startswith("QMGW")
print(f"[3] remote rows {len(rem)}: resolve {sum(res)}, to external gateways {gw.sum()}, dangling internal {len(rem) - sum(res) - gw.sum()}")

# 4. transmission queues
links = set(zip(rem.queue_manager, rem.remote_queue_manager))
xq = d[d.queue_usage == "Transmission"]
xs = set(zip(xq.queue_manager, xq.queue_name))
print(f"[4] directed QM links {len(links)}, xmitq defs {len(xs)}, matching a link {len(xs & links)}, orphan xmitqs {len(xs - links)}")

# 5. link symmetry and hubs
und = {tuple(sorted(l)) for l in links}
bi = sum((a, b) in links and (b, a) in links for a, b in und)
deg = collections.Counter(x for p in und for x in p)
top = set(sorted(deg, key=deg.get)[-10:])
print(f"[5] QM pairs {len(und)}, bidirectional {bi / len(und):.0%}, QMs with no link {d.queue_manager.nunique() - len(set(deg) & set(d.queue_manager))}, "
      f"max degree {max(deg.values())}, pairs touching top-10 QMs {sum(a in top or b in top for a, b in und) / len(und):.0%}")

# 6. app flows
name2id = appd.drop_duplicates("app_name").set_index("app_name").app_id.to_dict()
fl = appd[appd.app_role.isin(["Producer", "Consumer"])][["producer_app_name", "consumer_app_name"]].drop_duplicates()
fl = fl.assign(p=fl.producer_app_name.map(name2id), c=fl.consumer_app_name.map(name2id))
r = fl.dropna()
pairs = set(zip(r.p, r.c))
recip = sum((c, p) in pairs for p, c in pairs if p != c) // 2
print(f"[6] app flows {len(fl)} (resolved {len(r)}, external {len(fl) - len(r)}), reciprocal pairs {recip}, "
      f"max fan-out {r.groupby('p').c.nunique().max()}, max fan-in {r.groupby('c').p.nunique().max()}")

# 7. sizing
qa, qq, aq = appd.groupby("queue_manager").app_id.nunique(), d.groupby("queue_manager").queue_name.nunique(), appd.groupby("app_id").queue_manager.nunique()
print(f"[7] apps/QM median {qa.median():.0f} max {qa.max()}; queues/QM median {qq.median():.0f} max {qq.max()}; QMs/app mean {aq.mean():.2f} max {aq.max()}")

# 8. attributes
a = appd.drop_duplicates("app_id")
mc = a[a.app_criticality == "Mission Critical"]
print(f"[8] mission-critical apps {len(mc)}: regulated {(mc.app_regulated == 'Yes').mean():.0%}, recovery tiers {sorted(mc.app_recovery_tier.unique())}, "
      f"classes {sorted(mc.app_data_classification.unique())}")
ct = pd.crosstab(a.app_business_domain, a.app_business_unit)
print(f"    business units per domain: min {(ct > 0).sum(axis=1).min()} max {(ct > 0).sum(axis=1).max()}; retiring apps {(a.app_lifecycle == 'Retiring').sum()}")
pp = d[d.queue_usage != "Transmission"].assign(base=lambda x: x.queue_name.str.replace(r"\.AL$", "", regex=True)).groupby("base").persistent.nunique()
print(f"    queues with inconsistent persistence (must be 0): {(pp > 1).sum()}; QMs with >1 cluster: {(d[d.cluster_name != ''].groupby('queue_manager').cluster_name.nunique() > 1).sum()}")
