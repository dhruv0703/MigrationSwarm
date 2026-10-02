# Current Status

## Version

`0.1.0`

## Implemented

The repository contains deterministic repository, Java dependency, and
architecture analysis; grounded boundary proposals; migration planning; a
provider-neutral model registry/router; task DAG scheduling; PostgreSQL-backed
durable state; Redis transient coordination; bounded worker and verification
workers; managed Git worktrees; copy-first extraction; bounded DEBUG repair;
single- and multi-service orchestration; approvals; security controls; recovery
inspection; evaluation; observability; performance characterization; and
release-validation tooling.

## Validated

- Python tests: 527 passed, 2 skipped, 529 collected.
- Ruff and mypy: passed.
- Maven fixtures: Commerce, Support, Booking, and Fulfillment baseline tests pass.
- Normal benchmark boundary precision/recall/F1: `1.0 / 1.0 / 1.0`.
- Normal benchmark technical result: 10 services extracted, built, tested, and verified.
- Semantic contracts: 15 defined, 10 executed, 10 passed, 0 failed, 5 unavailable.
- External PostgreSQL, Redis, and combined worker validation: passed in the recorded local environment validation.
- Security policy check: passed.
- Clean-install validation: implemented and covered by packaging tests.
- Release validation: bounded harness implemented; offline validation is the release path.

The latest meaningful normal benchmark is recorded under
`artifacts/benchmarks/<run-id>/`. It covers all four fixtures, completes Commerce,
Support, and Booking, and intentionally leaves Fulfillment `review_required`.

## Environment-dependent validation

The following are intentionally not required for offline validation:

- external PostgreSQL connectivity and integration tests;
- external Redis runtime, queues, locks, and heartbeat behavior; and
- live provider health, quotas, network access, and model latency.

PostgreSQL is the durable authority and Redis is transient coordination. Unit
tests use SQLite and fakeredis where appropriate. No live model calls are part
of the offline release check.

## Intentionally blocked behavior

The Fulfillment fixture reaches `review_required` because its graph contains
cyclic dependencies, foreign repository access, shared domain objects, and a
cross-domain transaction. Semantic evaluation is `not_evaluated`, not failed,
because safe extraction was stopped before generated behavior existed.

## Current limitations

- Java analysis is lightweight source analysis, not a full compiler model.
- Semantic contracts compare bounded observables, not formal equivalence.
- Database decomposition and deployment are out of scope.
- Human review is required for unsafe or ambiguous dependency structures.
- External services and live providers remain environment-dependent.

## Release state

Offline release-candidate validation is complete for the current local state.
This is not a production-readiness certification.
