# Design

Read `PROBLEM_ANALYSIS.md` first. Scoring is in `SCORING.md`, the schema in `DATA_SCHEMA.md`.

## 1. Shape of the solution

```
inventory CSV/XLSX + mapping ──► ingest ──► as-is model ──┐
                                                           ├─► score (as-is)
config (profile, policies) ──► policy engine ──► optimiser ┤
                                                           ▼
                                   target assignment ──► generator ──► target model
                                                                        │
                       validator ◄──────────────────────────────────────┤
                       score (target), decision log, graph JSON ◄───────┘
                                                                        ▼
                                              target CSV (input format), channels CSV, MQSC script
```

One Python package (`mqresolver`) holds all logic and has no web dependency. A CLI, an API, and the UI are thin layers over it.

| Module | Responsibility |
|---|---|
| `ingest` | Read CSV/XLSX, apply column mapping, de-duplicate, data-quality report |
| `model` | Apps, QMs, queues, flows, external endpoints. The as-is and target topologies use the same classes. |
| `policy` | Parse and evaluate policies. Report violations for any topology. |
| `optimise` | App → QM assignment (section 4) |
| `generate` | Build target queues, transmission queues, channels. Write CSV in the input's columns, channels CSV, MQSC. |
| `validate` | Independent check of structural rules S1–S6 and all hard policies. Shares no code with `generate`. |
| `score` | Topology Complexity Index for any topology |
| `explain` | Decision log: for each app, where it went and why, and which policy forced it |
| `graph` | Cytoscape-style JSON for as-is and target |

## 2. Rules: structural vs policy

**Structural rules (S1–S6)** define a valid MQ target. Always enforced. See `PROBLEM_ANALYSIS.md`.

**Policies** are the enterprise's choices. Each has a `mode`: `hard` (must hold, or the run fails with an explanation), `soft` (violations are penalised in the score, weighted), or `off`.

| Type | Meaning | Example |
|---|---|---|
| `isolate` | Each matching app gets a dedicated QM | Mission-critical apps |
| `homogeneous` | Apps on one QM share an attribute (at most N distinct values) | One business domain per QM |
| `separate` | Apps with different values of an attribute never share a QM, or two named groups never mix | Regulated vs non-regulated |
| `colocate` | Matching apps must share a QM | A tightly coupled suite |
| `pin` | An app stays on a named QM | Mainframe app on its z/OS QM |
| `capacity` | Maximum apps per QM, maximum queues per QM | 40 apps, 1,000 queues |
| `qm_count` | Minimum / maximum number of QMs | Budget of 80 |

`when` selects apps by any `app_*` attribute: equality, list membership, or negation. No attribute name is built into the code.

Rules that one enterprise treats as fixed (for example "isolate our most critical apps" or "one business domain per QM") are ordinary entries here, so another enterprise can soften or drop them.

## 3. Configuration

One YAML file. Everything has a default, so an empty file is a valid configuration.

```yaml
profile: balanced              # balanced | consolidate | resilient | compliance
# weights: {simplicity: 0.5, resilience: 0.3, policy: 0.2}   # optional override

policies:
  - id: capacity
    type: capacity
    max_apps_per_qm: 40
    max_queues_per_qm: 1000
    mode: hard

  - id: isolate-critical
    type: isolate
    when: {app_criticality: "Mission Critical"}
    mode: hard

  - id: one-domain-per-qm
    type: homogeneous
    attribute: app_business_domain
    max_distinct: 1
    mode: soft
    weight: 2

target:
  eliminate_aliases: true
  external_partners: gateway   # gateway | keep
  gateway_qms: []              # empty = reuse QMs that already face external partners
  reuse_qm_names: true         # anchor target QMs on existing ones to cut migration
  naming: {new_qm: "QM{n:03d}", channel: "{from}.{to}"}

advanced:                      # most users never touch this
  seed: 42
  resolutions: [0.5, 1, 2, 4]
  refine_passes: 5
```

Keeping it easy for the user:

- **Profiles** set the scoring weights in one word.
- **Policy presets** in the UI: the user ticks suggested policies. Suggestions come from the data: attributes with few distinct values are offered for `homogeneous`/`separate`, rare values (under 10% of apps) are offered for `isolate`.
- Each policy is a three-way switch (hard / soft / off). Weights and the `advanced` block are hidden by default.
- Capacity defaults are derived from the data (for example the 90th percentile of as-is apps per QM) and shown as editable numbers.

## 4. Optimiser

Goal: the assignment that minimises the TCI (including soft-policy penalties) while satisfying structural rules and hard policies.

1. **App graph.** Undirected, weight = distinct queues between two apps (or message volume if supplied). External endpoints are not nodes.
2. **Hard pre-partitioning.** Hard policies are applied before clustering so they can never be broken:
   - `isolate` apps become fixed single-app QMs.
   - `colocate`/`pin` groups are contracted into one node.
   - Hard `homogeneous`/`separate` policies cut the graph into blocks. Clustering runs inside each block.
3. **Louvain per block**, for each resolution in `advanced.resolutions`. Seeded and iterated in sorted order, so results are reproducible.
4. **Capacity repair.** A community over a hard capacity limit is split by running Louvain on it at a higher resolution, recursively. Balanced chunking is the last resort.
5. **Packing.** Apps with no cross-app flow, and one- or two-app leftovers, join the QM they talk to most if it has room. Otherwise they are bin-packed into shared QMs within their block.
6. **Refinement (local search).** Repeatedly try moving one app to a neighbouring QM, or merging two small QMs. Keep a move only if no hard policy breaks and the TCI falls. This is where soft policies and the user's weights take effect. Stop when a pass finds nothing or after `refine_passes`.
7. **Candidate selection.** Steps 3–6 run for each resolution. The candidate with the lowest TCI wins. The user never tunes resolution.
8. **QM count bounds.** If `qm_count` is hard and exceeded, merge the smallest compatible QMs. If no legal merge exists, fail with a message naming the conflicting policies.
9. **Naming and anchoring.** Each target group is matched to the existing QM where most of its apps and queues already live (greedy by overlap, one group per QM). Unmatched groups get new names from the template. This minimises migration.

Infeasibility is explained, not hidden: if hard policies contradict each other, the run stops and names the apps and rules involved.

## 5. Target generation

For each queue-level flow, with producer P on QM A and consumer C on QM B:

| Case | Objects created |
|---|---|
| A = B | One local queue on A |
| A ≠ B | Local queue on B. Remote queue on A (`remote_queue_manager` = B, `remote_queue_name` = the local queue, `xmit_queue_name` = B). Transmission queue `B` on A. Channel pair `A.B` (sender on A, receiver on B). |
| C is external | Remote queue on A pointing at a gateway QM. Flagged for review. |
| P is external | Local queue on C's QM. Flagged for review. |

- Aliases are dropped when `eliminate_aliases` is on. Producer and consumer rows then name the real queue.
- If two flows would create the same queue name on one QM with different definitions, the name is kept for the first and the conflict is reported.
- Queue attributes (persistence, inhibit flags) are carried over.
- Rows with unknown role are kept, re-homed to their app's QM, and listed in the report.

Outputs:

| File | Content |
|---|---|
| `target_topology.csv` | Same columns and order as the input, including transmission-queue rows |
| `target_channels.csv` | Channel name, type, QM, partner QM, transmission queue |
| `target.mqsc` | `DEFINE QLOCAL / QREMOTE / CHANNEL` commands per QM |
| `report.json` | Scores, dimensions, anchors, policy compliance, data quality, migration effort |
| `decisions.json` | Per-app decision log |
| `graph_as_is.json`, `graph_target.json` | Graphs for the UI |

## 6. Interfaces

**CLI**

```
mqresolver run inventory.csv --config config.yaml --mapping mapping.yaml --out out/
mqresolver score inventory.csv          # score any topology file
mqresolver validate out/target_topology.csv --config config.yaml
```

**API (FastAPI, stateless)**

| Endpoint | Purpose |
|---|---|
| `POST /api/inspect` | Upload a file. Returns detected columns, proposed mapping, attribute summary, suggested policies. |
| `POST /api/run` | File + mapping + config. Returns the full result bundle. |
| `GET /api/sample` | The sample dataset, so anyone with the link can try it |
| `GET /api/health` | Liveness |

Uploads are processed in memory and never written to disk or logged. Upload size is limited.

**UI (React + TypeScript + Vite + Cytoscape.js)**, built after the engine is proven:

1. **Load**: upload a file or use the sample. Confirm the column mapping.
2. **Configure**: profile, policy switches, capacity numbers. Advanced options collapsed.
3. **Topology**: as-is and target graphs side by side. QMs are containers with their apps inside, channels are edges. Filter by any app attribute, search, click for details, highlight an app's flows in both views.
4. **Score**: TCI before and after, dimension bars with raw numbers, anchors, sensitivity table.
5. **Policies**: compliance before and after, per rule, with offending apps.
6. **Decisions and downloads**: decision log, CSV, channels, MQSC, report.

## 7. Technology and deployment

- **Engine**: Python 3.11+, pandas, networkx (its built-in Louvain, no extra library), pydantic, PyYAML, openpyxl. pytest for tests.
- **API**: FastAPI + uvicorn, also serving the built UI.
- **Deployment**: one Docker image. Push to GitHub, connect the repository to a container host with a free tier (Render is the first choice, Hugging Face Spaces the fallback), and share the URL it gives. No database and no secrets. Free instances usually sleep when idle, so the first request after a pause is slow. Check the host's current limits before relying on it.

## 8. Decisions and reasons

| Decision | Reason |
|---|---|
| Canonical schema + mapping | Any enterprise can use the tool, and output still matches their own format |
| Hard policies applied before clustering | They can never be violated, and no repair step can undo another |
| Same score for optimiser and report | The reported improvement is the optimised quantity |
| Automatic resolution choice | Removes the most sensitive tuning knob from the user |
| Reuse existing QM names | Lower migration effort, and familiar names in the target |
| Stateless API | Enterprises will not upload inventories to a service that stores them |
| networkx Louvain | One less dependency, same algorithm |
