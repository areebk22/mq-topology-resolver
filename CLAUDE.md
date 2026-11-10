# MQ Topology Resolver: working context

## What this is

A tool that takes an enterprise's as-is IBM MQ inventory (CSV/XLSX) and produces a simpler target topology in the same format, with a before/after complexity score, a policy compliance report, a decision log, and an as-is vs target graph UI. It must work for **any** enterprise.

## Read these first

| File | Content |
|---|---|
| `docs/PROBLEM_ANALYSIS.md` | The problem, why it is graph partitioning, Louvain evidence |
| `docs/DATA_SCHEMA.md` | Canonical schema, column mapping, sample dataset facts |
| `docs/DESIGN.md` | Modules, policies, config, optimiser, generation, API, UI, deployment |
| `docs/SCORING.md` | Topology Complexity Index |
| `docs/IMPLEMENTATION_PLAN.md` | Phases, acceptance criteria, current status |

## Non-negotiables

1. **No company-specific terms or data.** No column names, attribute names, rule names, counts, or QM/app names from the original project. `_reference/` holds that material and is git-ignored. Never copy from it into code, docs, tests, or sample data. Use the neutral vocabulary in `docs/DATA_SCHEMA.md`.
2. **Structural MQ rules S1–S6 are always enforced.** Everything else is a user policy with mode hard / soft / off.
3. **No attribute name is hard-coded.** Policies refer to `app_*` columns by name from config.
4. **The optimiser minimises the same score that is reported.**
5. **Deterministic.** Fixed seed, sorted iteration. Same input and config give identical output.
6. **Louvain is the clustering core** (networkx built-in), wrapped by hard pre-partitioning, capacity repair, packing, and local-search refinement.
7. **Few knobs for the user.** Profile + policy switches + capacity numbers. Everything else has a default or is derived from the data.
8. **Engine before UI.** The UI starts only when the engine passes its acceptance tests.
9. **Stateless.** Uploaded inventories are never stored or logged.

## Layout and commands

```
src/mqresolver/   engine (no web dependency)     tests/   pytest
src/mqresolver/web/  dependency-free canvas UI (no build step)   config/  default and example configs
scripts/          dataset generator and checker   data/sample/  synthetic inventory
```

```
python scripts/generate_synthetic_dataset.py --out data/sample/mq_topology_sample.csv
python scripts/check_dataset_realism.py data/sample/mq_topology_sample.csv
pytest
mqresolver run data/sample/mq_topology_sample.csv --out out/
mqresolver serve                                   # UI on http://127.0.0.1:8000
pytest -m "not slow and not ui"                    # quick tests; plain `pytest` runs everything
python scripts/run_scenarios.py --large            # regenerates docs/SCENARIO_RESULTS.md
```

## Conventions

- Python 3.10+, type hints, pydantic for config, ruff formatting. Engine dependencies: pandas, networkx, pydantic, PyYAML, openpyxl only.
- Every module gets tests in the same change. Acceptance numbers are in the implementation plan.
- Terms: "QM" = queue manager, "flow" = producer app → consumer app, "policy" = configurable placement rule, "TCI" = Topology Complexity Index.
- Target QM names: reuse an existing QM name when a group is anchored on it, otherwise the naming template.
- Update the Status table in `docs/IMPLEMENTATION_PLAN.md` when a phase completes.

## Reference numbers (sample dataset, seed 42)

400 apps, 189 QMs, 9,964 rows (147 duplicates), 1,284 directed QM links, 875 QM pairs, 25 mission-critical apps.
Default run: 189 → 12 QMs, 875 → 59 channel pairs, TCI 35.1 → 8.5, validation passes. 5,000 apps solve in about 25 s and need about 720 MB.

## Delivery

Push to the owner's GitHub, deploy with `render.yaml` (Docker, Render free plan: 512 MB, sleeps after 15 min idle), share the onrender.com link. Check for confidential material before every push.

## Why the UI has no framework

The build environment had no npm access, and rendering was what made the earlier UI hang. Positions are computed on the server; `web/graph.js` draws on canvas, batching edges and nodes into a few paths per frame. Measured: 11,688 nodes and 15,910 edges at a median 4.5 ms per frame.
