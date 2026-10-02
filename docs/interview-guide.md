# Interview Guide

## 30-second explanation

MigrationSwarm is an experimental Java modernization system that combines
deterministic repository evidence with bounded model reasoning. It plans and
extracts approved service candidates in isolated Git worktrees, verifies builds
and tests, evaluates bounded observable behavior, and stops for human review
when the evidence is unsafe or incomplete.

## 2-minute explanation

The system first inventories a repository, parses Java dependencies, and builds
deterministic architecture and ownership evidence. Grounded agents can propose
service boundaries and a migration DAG, but explicit approvals are required.
Approved work runs in managed task worktrees, never in the main repository.

PostgreSQL is the durable authority for tasks and run state. Redis is transient
coordination for queues, locks, and heartbeats. Workers execute bounded tasks;
verification workers independently inspect build evidence. Eligible failures
become explicit bounded DEBUG tasks. Finally, typed fixture contracts compare
outputs, statuses, errors, state, side effects, API/DTO shapes, validation, and
consumer/provider operations. Unsafe structures remain review-required.

## Architecture walkthrough

Walk from `agents/` to `core/scheduler/`, `core/orchestrator/`,
`persistence/`, `workers/`, `core/verification/`, and `evaluation/`. Explain
that each layer has a narrow responsibility and that durable lifecycle state is
separate from transient queue state.

## Why not just send the whole monolith to an LLM?

The model would have to infer ownership, dependencies, build constraints, and
behavior simultaneously. That makes hallucinations difficult to detect and
prevents reproducible evidence. MigrationSwarm supplies bounded facts first and
uses the model only for constrained interpretation or file proposals.

## Why deterministic analysis first?

Deterministic inventory, dependency edges, architecture metrics, cycle
detection, and ownership findings are repeatable and testable. They provide an
audit trail and prevent the model from inventing classes or dependencies.

## Why PostgreSQL plus Redis?

PostgreSQL preserves durable task, event, run, lock-related, and verification
state. Redis provides fast expiring queues, locks, and heartbeats. Keeping Redis
transient prevents queue loss or stale coordination from becoming authoritative
business state.

## How duplicate execution is prevented

Workers re-read durable task state, claim owner-token locks, and refuse work
when the task is no longer `READY`. Locks and heartbeats are bounded and
verification is a separate lifecycle stage.

## Why Git worktrees?

Each code-changing task gets an isolated clean worktree. This protects the main
repository, allows inspection of failed attempts, and gives each service branch
an independent filesystem boundary.

## How bounded repair works

`FailureClassifier` admits only configured compilation, generated-configuration,
and test failures. A DEBUG task records an explicit repair attempt, lets
`DebugAgent` make bounded safe file changes, and returns to build verification.
The configured repair limit is finite; exhausted or unsupported failures require
human review.

## How dependent services are scheduled

The multi-service orchestrator uses a dependent-to-prerequisite DAG. Independent
approved branches may run within the service-worker bound. A dependent starts
only after every prerequisite is durably `COMPLETED`; failed branches block
descendants while unrelated branches continue.

## What happens when a worker dies?

The durable task remains in PostgreSQL, while the heartbeat and owner lock can
expire. `recovery-status` inspects the state conservatively. It does not replay,
reset, delete, or requeue work automatically; an operator decides how to resume.

## How semantic correctness is evaluated

Fixture-declared typed contracts compare only bounded observables: outputs,
statuses, errors, state transitions, ordered side effects, API routes, DTOs,
validation, and consumer/provider contracts. Normalization is allowlisted.
This is not formal semantic equivalence or a proof of business correctness.

## Why Fulfillment is blocked

Fulfillment contains a service cycle, foreign repository access, shared domain
objects, and a cross-domain transaction. The system stops with
`review_required` and records semantic `not_evaluated` rather than claiming a
safe generated service without evidence.

## What performance measurements showed

The newest local characterization measured roughly 21.3 seconds sequentially,
16.0 seconds with the 2/2/1 configuration, and 15.7 seconds with 3/2/2. Maven
dominated the run. The small dependency graph limited useful concurrency, so
these are fixture measurements, not enterprise throughput claims.

## Biggest limitations

The Java parser is lightweight, semantic contracts are bounded adapters rather
than formal equivalence, external PostgreSQL/Redis and provider health are
environment-dependent, database decomposition is out of scope, and human review
is required for unsafe boundaries.

## What I would build next

I would first validate the external PostgreSQL/Redis paths in a controlled
environment and improve compiler-aware Java analysis before expanding migration
scope. I would keep approval, verification, and bounded-repair boundaries intact.
