# Data Schema

The tool works on a **canonical, enterprise-neutral schema**. An enterprise's own export is mapped onto it with a small mapping file, and the target CSV is written back **in the enterprise's original columns and order**.

## 1. Canonical columns

### Topology (required unless marked optional)

| Column | Values | Meaning |
|---|---|---|
| `queue_name` | string | Queue name on this QM |
| `queue_type` | `Local` / `Remote` / `Alias` | MQ queue type |
| `queue_manager` | string | QM that owns this definition |
| `app_id` | string, may be empty | Application tied to this row. Empty = infrastructure row. Treated as atomic (may contain `/`). |
| `app_name` | string | Full application name |
| `app_role` | `Producer` / `Consumer` / `Unknown` | Role of the app on this queue |
| `producer_app_name` | string | Name of the producing app for this queue's flow |
| `consumer_app_name` | string | Name of the consuming app |
| `remote_queue_manager` | string | Remote rows: destination QM |
| `remote_queue_name` | string | Remote rows: queue on the destination QM |
| `xmit_queue_name` | string, optional | Remote rows: transmission queue (empty = default) |
| `alias_target_queue` | string, optional | Alias rows: queue the alias resolves to. If the column is missing, the alias is assumed to target the queue it shares a flow with. |
| `queue_usage` | `Normal` / `Transmission` / empty, optional | `Transmission` marks a transmission queue |

### Queue attributes (optional, carried through unchanged)

`persistent`, `put_inhibited`, `get_inhibited`, `cluster_name`, and any other column not listed here.

### Application attributes (optional, any number)

Any column starting with `app_` other than the ones above is an **application attribute**. Policies can refer to any of them. The sample dataset has:

| Column | Sample values |
|---|---|
| `app_business_domain` | Sales, Finance, Supply Chain, ... (10) |
| `app_business_unit` | Business Unit A–F, cuts across domains |
| `app_criticality` | Mission Critical / Business Critical / Standard / Low |
| `app_data_classification` | Restricted / Confidential / Internal / Public |
| `app_regulated` | Yes / No |
| `app_internet_facing` | Yes / No |
| `app_hosting` | On-Prem / Private Cloud / Mainframe / Public Cloud / SaaS |
| `app_recovery_tier` | Tier 0 – Tier 4 |
| `app_lifecycle` | Active / Retiring |

If an app has different values on different rows, the most frequent value is used and the conflict is reported.

## 2. Column mapping

```yaml
# mapping.yaml: left = canonical name, right = the enterprise's header
columns:
  queue_name: "Queue"
  queue_type: "Type"
  queue_manager: "QMgr"
  app_role: "Application Role"
  app_business_domain: "Portfolio"
values:                       # optional value translation
  app_role: {"TBD*": "Unknown"}
```

- If headers already match the canonical names, no mapping file is needed.
- The UI proposes a mapping by fuzzy-matching headers and lets the user confirm it.
- Unmapped columns are kept and written back untouched.

## 3. Row semantics

- `app_role = Producer`: `app_id` produces, the partner is `consumer_app_name`. `Consumer`: the reverse.
- Partner names are resolved to `app_id` through `app_name`. Names that match no app are **external endpoints**.
- A queue-level flow is found by joining producer and consumer rows on the queue's base identity: a remote row points to (`remote_queue_manager`, `remote_queue_name`), an alias row points to `alias_target_queue` on the same QM.
- An app-level flow is the directed pair (producer app, consumer app). Its weight is the number of distinct queues carrying it. If a `message_volume` column exists it is used instead.
- An as-is QM link is a remote row whose `remote_queue_manager` differs from `queue_manager`.

## 4. Sample dataset

`data/sample/mq_topology_sample.csv` (and `.xlsx`), produced by `scripts/generate_synthetic_dataset.py` (400 apps, 200 QMs, seed 42). Fully synthetic.

| Fact | Value |
|---|---|
| Rows / columns | 9,964 / 26 (147 exact duplicates) |
| Apps / QMs in use | 400 / 189 |
| Queue types | Local 50%, Remote 36%, Alias 15% |
| QMs per app | mean 2.22, max 11 |
| Directed QM links / QM pairs | 1,284 / 875 (47% two-way) |
| App flows | 1,247 (59 to or from external partners) |
| Transmission queue rows | 752 of 1,284 needed (inventory deliberately incomplete) |
| Mission-critical apps | 25 |
| Unknown-role rows | 32 |

Scale it with `--apps`, `--qms`, `--flows`, `--seed`.
