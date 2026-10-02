# Performance characterization

MigrationSwarm includes a bounded offline performance command for measuring the
existing orchestration path:

```powershell
migrationswarm perf --offline
migrationswarm perf --offline --fixture commerce --iterations 7
```

Each run writes `artifacts/performance/<run-id>/summary.json` and `summary.md`.
The report records environment versions, three worker configurations, measured
speedups, stage timings, synthetic Java repositories, microbenchmarks, and
bounded safety metadata. It does not contain private paths, credentials,
prompts, source files, or machine identifiers.

## Latest local characterization

The newest recorded report is written under
`artifacts/performance/<run-id>/`:

| Configuration | Service/task/verification workers | Duration | Speedup |
| --- | --- | ---: | ---: |
| sequential | 1 / 1 / 1 | 41,359 ms | 1.0000x |
| parallel-2 | 2 / 2 / 1 | 28,782 ms | 1.4370x |
| parallel-3 | 3 / 2 / 2 | 30,328 ms | 1.3637x |

These numbers supersede older local measurements. They characterize one small
commerce fixture on one Windows/Python/Java/Maven environment, not an enterprise
scalability claim.

## Why more workers do not guarantee more throughput

The fixture exposes only a small amount of independent work. In the recorded
run, observed service concurrency was capped at two even when three service
workers were configured. Maven build/test time dominated the run, so a third
worker could not create proportional speedup. An earlier local run had
parallel-3 slightly slower than parallel-2; that is expected variance under the
same dependency and Maven bottleneck, not evidence of a stable regression.

## Measured stages

The offline commerce path measures repository analysis, dependency analysis,
architecture analysis, deterministic model-backed reasoning, task execution,
service extraction, worktree creation, Maven verification, repair, verification
decisions, and artifact persistence. Durations use monotonic clocks. Model
latency is recorded separately when available.

Queue operations, SQLite round trips, and dispatch overhead are measured as
microbenchmarks. The demo does not pretend that its local service orchestration
is a full external PostgreSQL/Redis deployment.

## Synthetic scale characterization

Temporary Java repositories with 25, 100, and 500 classes are generated at run
time. Repository scan, dependency parse, and NetworkX graph timings are
reported. They do not require Maven and are local parser characterization, not
proof of enterprise scalability.

Microbenchmarks cover repository analysis, dependency analysis, architecture
analysis, task-graph scheduling, fakeredis queues, SQLite persistence, JSON
serialization, and worker dispatch inspection. Each reports median, minimum,
maximum, and p95 where the sample size supports it.

## Limitations

Maven build time, filesystem state, CPU availability, and local machine load can
dominate results. Offline timings exclude live provider latency, rate limits,
and network behavior. The fixture is intentionally small. Performance runs use
temporary copies, assert that the main repository remains unchanged, preserve
service dependency ordering, and keep workers, iterations, generated
repositories, and artifacts bounded.
