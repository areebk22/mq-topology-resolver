# Problem Analysis

Independent analysis of the problem and the input. It starts from the problem statement, not from any earlier implementation.

## 1. The problem in one paragraph

An IBM MQ estate grows by accretion. Applications end up connected to several queue managers (QMs), QMs are linked point-to-point wherever a flow once needed it, and alias queues pile indirection on top. Nobody can say who owns a link, and onboarding an app means hand-crafting objects on several QMs. We are given the **as-is inventory** (one row per queue definition, tied to an application and a role) and must produce a **target-state topology** that is simpler, standard, and explainable, in the same format as the input, with a measurable before/after complexity score.

## 2. What the target state must look like (MQ mechanics, not policy)

These rules define a valid target. They come from the problem statement and from how MQ works, so they are **always on**.

| ID | Rule |
|---|---|
| S1 | Every application connects to exactly one QM. |
| S2 | A producer puts to a queue on its own QM. If the consumer is on another QM, that queue is a **remote queue** naming the consumer's QM, the consumer's local queue, and a transmission queue. |
| S3 | A consumer gets from a **local queue** on its own QM. |
| S4 | Two QMs that exchange messages are joined by sender/receiver channel pairs with deterministic names (`FROM.TO`), and each sender has a transmission queue named after the destination QM. |
| S5 | Every as-is flow that can be resolved is still deliverable in the target. Nothing is silently dropped. |
| S6 | Only information in the input is used. |

Alias removal is a simplification choice, not a law of MQ, so it is a setting (default on).

## 3. What kind of problem this is

Once S1 holds, the whole target is determined by **one decision: which QM each app lives on**. Everything else (local queues, remote queues, transmission queues, channels) is generated mechanically from that assignment.

- Two apps on the same QM: their flow needs 1 object (a local queue).
- Two apps on different QMs: their flow needs 5 objects on the message path (remote queue, transmission queue, sender channel, receiver channel, local queue), and the QM pair needs channels in each direction used.

So the task is **constrained graph partitioning**. Nodes are apps, edge weights are how much two apps talk, groups are QMs. We want heavy talkers together, subject to limits on group size and to the enterprise's placement policies.

Two degenerate answers show why constraints and scoring matter:

- **One QM for everything**: zero channels, but a single point of failure and an unmanageable QM.
- **One QM per app**: perfect isolation, but every flow crosses a channel.

A good answer sits between them, and where it sits depends on what the enterprise values. That is why policies and scoring weights must be configurable.

## 4. Does Louvain make sense? Yes, as the core, not the whole

Louvain community detection maximises modularity: it finds groups of apps that talk more to each other than to the rest. That is exactly "put heavy talkers on one QM". It is fast, deterministic with a seed, and needs no target number of groups.

Measured during design on an earlier 438-app version of the sample (1,207 cross-app flows, as-is 194 QMs and 914 QM pairs). Results of the finished optimiser on the current sample and seven other estates are in `SCENARIO_RESULTS.md`.

| Approach | QMs | Max apps/QM | Flow staying on one QM | Channel pairs |
|---|---|---|---|---|
| Baseline: each app to its busiest as-is QM | 114 | 35 | 10.0% | 410 |
| Baseline: one QM per business domain | 11 | 131 | 69.6% | 36 |
| Baseline: one QM per app | 438 | 1 | 0% | 704 |
| Louvain, no limits (resolution 1) | 76 | 63 | 76.3% | 58 |
| Louvain + size cap 40 + packing | 38 | 40 | 66.2% | 165 |
| ... + isolate mission-critical apps (hard) | 68 | 40 | 58.1% | 245 |
| ... + one business domain per QM (hard) | 64 | 38 | 55.1% | 225 |
| Louvain + size cap 25 + packing | 51 | 25 | 61.7% | 221 |
| ... + both policies (hard) | 73 | 25 | 50.5% | 264 |

Findings that shape the design:

1. **Louvain clearly beats the naive baseline** (55–66% of flow kept local vs 10%).
2. **Louvain alone is not enough.** It ignores size limits (63 apps on one QM) and policies.
3. **59 apps have no cross-app flow.** Louvain leaves each as its own community. They need a packing step or the QM count balloons.
4. **Each policy has a visible price** (isolating 25 critical apps adds about 30 QMs and 80 channel pairs). The tool should show that price so users can decide between hard and soft.
5. Resolution changes the answer a lot, so the tool should pick it automatically by scoring candidates rather than asking the user.

## 5. Reading the input

One row = one queue definition on one QM, tied to one application in one role.

- **Flows** are found by joining producer rows and consumer rows of the same queue. A remote queue's `remote_queue_manager`/`remote_queue_name` and an alias's `alias_target_queue` say where the message really lands.
- **Partner names that match no known app** are external endpoints. They cannot be placed on a QM, so they are routed through gateway QMs and reported.
- **Rows with no app** are infrastructure (transmission queues). They are regenerated, not migrated.
- **Unknown-role rows and exact duplicates** are data-quality issues: reported, excluded from flow inference.
- **App attributes** (`app_*` columns) are free-form. The tool treats any of them as something a policy can refer to. It hard-codes none.

## 6. Realism problems found in the first synthetic dataset (now fixed)

| Problem | Fix in the generator |
|---|---|
| Same queue name defined as Local and Alias on one QM (impossible in MQ) | Aliases have their own name and an `alias_target_queue` |
| Transmission queues random and unrelated to links | Generated from real links, as infrastructure rows, about 60% inventoried |
| Almost all traffic one-way | 80% of request queues get a reply queue returning over the same QM pair |
| No large consumers | Consumers are also chosen with a popularity weight |
| External partners landing on internal QMs | Routed to three gateway QMs |
| Attributes uncorrelated | Mission-critical apps are mostly regulated, tier 0/1, restricted/confidential |
| Business unit a copy of domain | Units cut across domains |
| Cluster and persistence random per row | Cluster is a QM property, persistence is fixed per queue |

`scripts/check_dataset_realism.py` verifies these on any inventory file.
