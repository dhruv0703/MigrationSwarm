# Evaluation

The evaluation layer measures reproducible local extraction behavior. It does
not turn benchmark metadata into a production claim and it does not use an LLM
to decide whether a service name matches an expected boundary.

Each run writes bounded artifacts under
`artifacts/benchmarks/<run-id>/`. A summary contains fixture status, stage
denominators, dependency findings, technical results, semantic results, and
review findings.

## Fixtures

Each Spring Boot fixture contains a root `benchmark.json` with expected service
names, aliases, package roots, ignored shared/config segments, and typed
behavior contracts.

| Fixture | Purpose |
| --- | --- |
| Commerce | Baseline multi-domain order, inventory, customer, and notification extraction. |
| Support | Independent ticket, user, and notification branch behavior. |
| Booking | Reservation/payment/notification dependencies and consumer/provider contracts. |
| Fulfillment | Safety case with cycles, foreign repositories, shared values, and cross-domain transactions. |

## Technical status

Technical status describes whether the extraction pipeline produced a usable
technical artifact. It covers:

- boundary prediction and matching;
- copy-first extraction completion;
- Maven build execution and result;
- fixture test execution and result; and
- deterministic verification evidence and lifecycle decision.

Technical extraction, build, test, and verification are measured with explicit
denominators. An unavailable stage is not silently counted as zero or success.

The latest normal benchmark records 10 extracted services with 10 build passes,
10 test passes, and 10 verification passes. Fulfillment is intentionally not
included in those successful technical totals because its safety findings stop
the extraction before a trustworthy generated service exists.

## Semantic status

Semantic status is separate from technical status. The bounded evaluator compares
only typed observables declared by the fixture contract:

- outputs and response statuses;
- expected errors;
- state transitions;
- ordered side effects;
- API routes and HTTP methods;
- request/response and DTO shapes;
- validation rules; and
- consumer/provider operation contracts.

Generated services carry small contract adapters under test resources. The
evaluator compares source and generated observations without starting an
unbounded Spring application. A contract may use explicitly allowlisted
normalization for nondeterministic fields such as `requestId` or `timestamp`.
Unlisted field changes are not ignored.

The latest normal benchmark records:

| Metric | Result |
| --- | ---: |
| contracts defined | 15 |
| contracts executed | 10 |
| contracts passed | 10 |
| contracts failed | 0 |
| contracts unavailable | 5 |
| complete fixtures | 3 |
| semantic failures | 0 |

These are bounded observable-contract comparisons, not formal semantic
equivalence, business correctness, or production compatibility proof.

## Why Fulfillment is `not_evaluated`

Fulfillment is `review_required`, not a semantic failure. Its dependency graph
contains a cycle involving candidate services, foreign repository access, shared
domain objects, and a cross-domain transactional write. The orchestrator stops
before it creates a trustworthy generated service. Comparing absent generated
behavior would fabricate evidence, so its semantic status is `not_evaluated`.

The report records the safety findings and the unavailable contracts explicitly.
Human review must choose a contract, inversion, or consistency boundary before
semantic evaluation can become meaningful.

## Boundary matching

The matcher lowercases names, treats spaces, hyphens, and underscores as
separators, removes only a trailing `Service`/`Services` suffix, applies
conservative plural normalization, and checks declared aliases. Each prediction
can match at most one expected service.

For each fixture:

```text
precision = matched / predicted
recall    = matched / expected
F1        = 2 * precision * recall / (precision + recall)
```

Undefined zero-denominator values are `null` in JSON and `N/A` in Markdown.
Aggregate boundary metrics are macro averages over fixtures with defined values,
not micro averages over all services.

## Extraction and verification rates

Rates use only services for which a stage was attempted:

- extraction completion = completed extractions / extraction attempts;
- build success = passed builds / build attempts;
- test success = passed tests / test attempts;
- verification success = passed verification / verification attempts;
- first-pass success = zero-repair successes / zero-repair attempts; and
- repair success = successful repairs / services requiring repair.

The reports retain every denominator so blocked or unavailable work is visible.

## Dependency and ownership findings

The baseline groups Java classes by the first package segment below the fixture's
declared package root. A deterministic graph then classifies service edges,
foreign repositories, unresolved dependencies, shared value objects, and
transaction findings. Tarjan strongly connected components expose cycles and
prevent a false total migration order.

Entity references do not transfer persistence ownership. Direct access to a
foreign repository and `@Transactional` writes spanning candidate services are
review findings. They are not silently copied into the generated service.

## Determinism and limitations

For a fixed checkout and environment, fixture metadata, package predictions,
matching, graph analysis, structured offline content, and metric calculation are
deterministic. Run IDs, timestamps, elapsed durations, Maven output, and ignored
generated files vary and must be normalized for repeatability comparisons.

Offline benchmark mode uses deterministic model responses and makes no provider
calls. Live mode is explicit and provider-dependent. A live provider failure is
recorded per fixture and does not erase unrelated results.

Passing these checks does not establish deployment readiness, data migration,
network contract readiness, or production-grade service boundaries.
