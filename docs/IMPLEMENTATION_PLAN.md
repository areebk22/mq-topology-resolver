# Implementation Plan

Build order is engine first, UI last. Each phase ends with tests that must pass before the next starts. Design references: `DESIGN.md`, `SCORING.md`, `DATA_SCHEMA.md`.

## Status

| Item | State |
|---|---|
| Problem analysis, design, scoring design | Done (docs) |
| Sample dataset + generator + realism checker | Done |
| Louvain feasibility prototype | Done (results in `PROBLEM_ANALYSIS.md` §4) |
| Engine (ingest, policies, scoring, optimiser, generator, validator, pipeline, CLI) | Done, merged |
| Scenario matrix (8 estates × up to 13 rule sets, 99 runs) | Done, all pass (`docs/SCENARIO_RESULTS.md`) |
| API (Starlette + uvicorn) | Done, merged |
| UI (dependency-free canvas) | Done, merged, browser-tested to 5,000 apps |
| Docker image, Render blueprint, README | Done, merged; not yet deployed |

## Repository layout

```
mq-topology-resolver/
├── CLAUDE.md                      # working context for development
├── README.md                      # written in phase 9
├── pyproject.toml
├── Dockerfile
├── config/
│   ├── default.yaml               # balanced profile, capacity only
│   └── examples/                  # sample policy sets and a sample mapping
├── data/sample/                   # synthetic inventory (csv, xlsx)
├── docs/
├── scripts/                       # dataset generator, realism checker
├── src/mqresolver/
│   ├── ingest.py  model.py  policy.py  optimise.py
│   ├── generate.py  validate.py  score.py  explain.py  graph.py
│   ├── config.py  pipeline.py  cli.py
│   └── api.py
├── tests/
    └── web/                       # canvas UI, served by the API
```

## Phase 0: Scaffold (small)

- `pyproject.toml`, package skeleton, pytest, ruff, `.gitignore`, `config/default.yaml`.
- **Done when:** `pytest` runs and `mqresolver --help` works.

## Phase 1: Ingest and model

- Read CSV/XLSX, apply mapping, value translation, de-duplicate.
- Build apps (attributes by most frequent value), QMs, queues, queue-level flows, app-level flows, external endpoints, as-is QM links.
- Data-quality report: duplicates, unknown roles, unresolved partners, attribute conflicts, dangling remote queues and aliases, (QM, name) type collisions.
- **Done when**, on the sample: 438 apps, 194 QMs, 161 duplicates, 32 unknown-role rows, 1,376 directed QM links, 914 QM pairs, 0 dangling internal remote queues, 0 type collisions. A mapped copy of the sample with renamed headers gives identical results.

## Phase 2: Policy engine

- Pydantic models for the config. Policy types: `isolate`, `homogeneous`, `separate`, `colocate`, `pin`, `capacity`, `qm_count`. `when` selectors.
- `evaluate(topology) → violations` for every policy, usable on as-is and target.
- Conflict detection between hard policies (for example `colocate` vs `separate`).
- Policy suggestions from attribute statistics.
- **Done when:** unit tests cover each type in hard, soft, and off modes, and a contradictory config produces a clear error naming the rules and apps.

## Phase 3: Scoring

- TCI dimensions, profiles, custom weights, anchors, sensitivity table (`SCORING.md`).
- Built before the optimiser because the optimiser uses it as its objective.
- **Done when:** tests prove (a) every dimension stays in 0–1, (b) on the sample under `balanced`, single-QM and one-QM-per-app both score worse than a simple Louvain assignment, (c) doubling the dataset (two disjoint copies) changes the TCI by less than 2 points, (d) scores are identical across runs.

## Phase 4: Optimiser

- Steps 1–9 of `DESIGN.md` §4. Incremental score updates so refinement stays fast (target: sample runs in under 30 seconds).
- **Done when**, on the sample:
  - Every app has exactly one QM, and all hard policies hold in every tested config.
  - Capacity 40, no other policy: at least 60% of flow weight on one QM and at most 180 channel pairs (prototype without refinement: 66.2% / 165).
  - With isolation and one-domain-per-QM both hard: at least 50% and at most 260 pairs (prototype: 55.1% / 225).
  - Switching a policy from hard to soft never worsens the TCI.
  - Same seed gives byte-identical output.

## Phase 5: Generator and validator

- Target objects, CSV in input columns, channels CSV, MQSC, graph JSON.
- Independent validator for S1–S6 and hard policies.
- **Done when:** the validator passes on every optimiser test case. Deliberately corrupted targets (app on two QMs, remote queue to a missing QM, missing channel half, alias left in, dropped flow) are each caught. The target CSV has the input's exact columns and order. Every resolvable as-is flow is deliverable.

## Phase 6: Pipeline, explain, CLI

- `pipeline.run(file, mapping, config) → result bundle`. Decision log. Migration-effort metrics. CLI commands `run`, `score`, `validate`.
- **Done when:** one command produces all output files from the sample, and the decision log gives a reason for every app.

## Phase 7: API

- Starlette endpoints in `DESIGN.md` §6 (FastAPI was not installable in the build environment; Starlette is what FastAPI is built on). In-memory processing, upload limit, input validation, no persistence.
- **Done when:** API tests pass for inspect, run, sample, bad file, oversized file, and bad config.

## Phase 8: UI

- Start only when phases 1–7 are green and the target has been reviewed on the sample.
- Screens in `DESIGN.md` §6. Graph views share selection and filters between as-is and target.
- **Done when:** the sample loads and runs from the browser, both graphs render and stay responsive at 438 apps, a policy switch changes the result, and all downloads work. Checked at desktop and laptop widths.

## Phase 9: Package and deploy

- Multi-stage Dockerfile (build UI, then Python image serving it).
- README: what it does, quick start, config reference, scoring summary, limitations.
- Push to GitHub. Connect the repository to the container host. Share the URL.
- **Done when:** a fresh visitor can open the link, run the sample, and download a target.

## Before pushing to GitHub

- Nothing from the original company project goes into the repository. `_reference/` (the rebuild spec and the first-format dataset) is git-ignored.
- Sample data is synthetic and uses neutral names only.
- No real inventories, credentials, or internal names in code, tests, docs, or commit history.

## Risks and how they are handled

| Risk | Handling |
|---|---|
| Refinement too slow on large estates | Incremental scoring, move candidates limited to graph neighbours, pass limit |
| Hard policies make the problem infeasible | Detect early and name the conflict. Suggest which rule to soften. |
| Real inventories messier than the sample | Data-quality report first, tolerant ingest, nothing dropped silently |
| Queue-name collisions after consolidation | Detected in generation and reported. Never silently merged. |
| Score distrusted | Raw numbers, anchors, and sensitivity table shown with every result |
| Free hosting limits | Stateless single container, portable to any Docker host |

## Known limits (to state in the README)

- Flow weight is the number of queues unless message volumes are supplied.
- High availability, disaster recovery, and network zones are not modelled unless expressed as app attributes and policies.
- MQ clusters in the as-is state are read but the target uses explicit channels.
