# Complexity Scoring: Topology Complexity Index (TCI)

## 1. Why the earlier score is not trustworthy

- Its normalisers were arbitrary constants (channels / 1000, aliases / 5000, QMs against a fixed "optimal 65"). Change the estate size and the score stops meaning anything.
- One component was always zero, one penalised the isolation the same solution mandated, and the weights had no rationale.
- It was not the thing the optimiser optimised, so "score improved" and "solution is better" were only loosely related.

## 2. Design principles

1. **Every dimension has a plain meaning and a natural 0–1 range** that comes from MQ mechanics or from the estate itself. No tuned constants.
2. **Size-independent.** A 50-app estate and a 5,000-app estate are scored on the same scale.
3. **Neither degenerate answer can win.** "One QM for everything" and "one QM per app" must both score badly under the default weights.
4. **The optimiser minimises this same score.** What is reported is what was optimised.
5. **Weights follow the user's priorities**, chosen with a profile or three sliders, not twelve numbers.
6. **Transparent.** Raw numbers are shown beside every dimension, and the score is re-computed under every profile so the user can see whether the improvement depends on the weighting.

## 3. Dimensions (each 0 = best, 1 = worst)

| Theme | Dimension | Definition | Meaning |
|---|---|---|---|
| Simplicity | **Routing depth** | `(h − 1) / 6`, where `h` is the flow-weighted mean number of MQ objects a message crosses. Same-QM local queue = 1. Each alias adds 1. Cross-QM = 5 (remote queue, transmission queue, sender, receiver, local queue). Worst case is 7. | How far a message travels |
| Simplicity | **Application sprawl** | `1 − apps / (app, QM) connections` | Share of app connections that are redundant. 0 when every app uses one QM. |
| Simplicity | **Channel load** | `QM pairs linked / distinct app pairs that communicate`, capped at 1 | How many channels are paid for per business relationship |
| Simplicity | **Object overhead** | `1 − logical queues / managed objects`. Managed objects = queue definitions + transmission queues + channel definitions actually required. | Share of objects that exist only for plumbing |
| Resilience | **Blast radius** | `Σ s_q²`, where `s_q` is QM q's share of all flow endpoints (Herfindahl index) | Chance that two random message endpoints fail together. 1 for a single QM, `1/n` for an even spread over n QMs. |
| Policy fit | **Policy violations** | Weighted share of apps (or QMs, for capacity rules) breaking each enabled policy, averaged by rule weight | How far the estate is from the enterprise's own rules |

Notes:

- Aliases are counted inside routing depth, not as a separate dimension, to avoid counting them twice.
- For the **as-is** state, transmission queues and channels are counted as *required by the links*, not as inventoried, because inventories are incomplete.
- For the as-is state, policy violations are measured against the same enabled policies, so the user sees how non-compliant today's estate is.
- A target that breaks a **hard** policy is not scored as "worse". It is rejected as invalid.

## 4. The index

```
TCI = 100 × ( w_simplicity × mean(routing depth, sprawl, channel load, object overhead)
            + w_resilience × blast radius
            + w_policy     × policy violations )
```

Lower is better. Range 0–100.

| Profile | Simplicity | Resilience | Policy fit | Use when |
|---|---|---|---|---|
| `balanced` (default) | 0.50 | 0.30 | 0.20 | No strong preference |
| `consolidate` | 0.70 | 0.15 | 0.15 | Cost and operational effort dominate |
| `resilient` | 0.35 | 0.50 | 0.15 | Outage impact dominates |
| `compliance` | 0.35 | 0.20 | 0.45 | Regulatory placement rules dominate |

Users may instead set the three weights directly. They are normalised to sum to 1.

## 5. Sanity anchors (reported with every run, and enforced by tests)

Each report scores four topologies with the same formula:

| Topology | Expected behaviour under `balanced` |
|---|---|
| As-is | High: deep routing, sprawl, many channels |
| One QM per app | High: every flow crosses channels, maximum object overhead |
| Single QM | Poor: blast radius = 1, and capacity policies violated |
| Proposed target | Lowest of the four |

If the proposed target does not beat all three anchors, the run is flagged. This is the main defence against a score that can be gamed by consolidation or by fragmentation.

## 6. What is reported

- TCI for as-is and target, with the change in points and percent.
- Each dimension for as-is and target, with the raw numbers behind it (for example "mean objects per message path 3.4 → 2.1").
- The same comparison under all four profiles (sensitivity table).
- The price of each policy: TCI and QM count with that policy relaxed (computed on request, because it needs a re-run).

**Migration effort** is reported separately and is not part of the index, because it describes the journey, not the destination: apps that change QM, queues re-homed, QMs reused, QMs retired, QMs created. It can be turned on as a soft objective.
