# MigrationSwarm agent guide

## Purpose and phase

MigrationSwarm will become a multi-agent system for analyzing Spring Boot monoliths and extracting bounded domains into independently deployable microservices. The repository is currently in the bounded multi-service orchestration phase: deterministic task orchestration, provider-independent worker contracts, grounded analysis, isolated extraction, safe verification, bounded repair, and explicit service approvals.

## Repository map

- `src/migrationswarm/` — application package.
- `src/migrationswarm/api/` — FastAPI application and HTTP routes.
- `src/migrationswarm/cli/` — Typer CLI entry point.
- `src/migrationswarm/config/` — Pydantic Settings configuration.
- `src/migrationswarm/core/tasks/` — task model, enums, domain exceptions, and centralized state machine.
- `src/migrationswarm/core/scheduler/` — in-memory dependency graph and synchronous readiness scheduler.
- `src/migrationswarm/agents/repository_analysis.py` — deterministic repository inventory agent.
- `src/migrationswarm/agents/dependency_analysis.py` — lightweight Java source dependency graph agent and models.
- `src/migrationswarm/agents/architecture_analysis.py` — deterministic NetworkX architecture metrics and candidate components.
- `src/migrationswarm/core/models/` — provider-neutral model contracts, model registry, provider registry, and router.
- `src/migrationswarm/core/git/` — subprocess-backed repository inspection and safe managed worktrees.
- `src/migrationswarm/core/workspaces/` — `TaskWorkspace` contract for isolated code-changing work.
- `src/migrationswarm/providers/` — synchronous Groq and SiliconFlow HTTP integrations.
- `src/migrationswarm/agents/service_boundary.py` — grounded AI-assisted candidate boundary proposals.
- `src/migrationswarm/agents/migration_planning.py` — grounded migration plans and Task DAG conversion.
- `src/migrationswarm/agents/refactor.py` — bounded model-assisted full-file refactoring inside task worktrees.
- `src/migrationswarm/agents/service_extraction.py` — copy-first candidate extraction into a new service directory.
- `src/migrationswarm/agents/build_verification.py` — allowlisted Maven/Gradle test verification and evidence artifacts.
- `src/migrationswarm/core/verification/` — deterministic evidence policy, decision engine, and lifecycle coordinator.
- `src/migrationswarm/core/orchestrator/` — single-service workflow plus bounded multi-service DAG orchestration, approval metadata, and run artifacts.
- `src/migrationswarm/persistence/` — SQLAlchemy/Alembic durable state repositories and Redis-only transient coordination.
- `src/migrationswarm/workers/` — durable-state-aware worker, dispatcher, bounded manager, and finite swarm coordinator.
- `src/migrationswarm/workers/verification/` — separate verification queue, evidence loader, dispatcher, and bounded decision workers.
- `src/migrationswarm/workers/debugging/` — separate bounded DEBUG queue, repair-attempt persistence contract, dispatcher, and workers that invoke `DebugAgent` then re-run verification.
- `.migrationswarm/service-approvals.json` — explicit human approvals; never populate it automatically.
- `.migrationswarm/multi-runs/` — bounded JSON/Markdown summaries for multi-service runs; no prompts, keys, source, or full logs.
- `migrations/` and `alembic.ini` — committed Alembic schema migrations; do not replace with runtime `create_all`.
- `src/migrationswarm/core/` — small shared application primitives.
- `tests/unit/` and `tests/integration/` — unit and API tests.
- `docs/`, `examples/`, `scripts/` — future documentation, examples, and utilities.
- `src/migrationswarm/core/observability/` — in-process secret-free metrics and benchmark artifacts.
- `src/migrationswarm/performance/` — bounded offline performance benchmarks, synthetic fixtures, and reports.
- `src/migrationswarm/core/security/` — reusable path, size-limit, redaction, and atomic-artifact helpers.
- `src/migrationswarm/core/recovery/` — conservative crash-state inspection; it never replays work automatically.
- `src/migrationswarm/core/validation/` — read-only cross-artifact lineage and path-consistency checks.
- `src/migrationswarm/evaluation/` — typed fixture metadata, deterministic service matching, package baseline, benchmark runner, metrics, and renderers.
- `src/migrationswarm/evaluation/dependency.py` — deterministic service dependency edges, Tarjan SCCs, migration ordering, foreign-repository and transaction findings.
- `src/migrationswarm/evaluation/behavior.py` — typed, bounded source/generated observable contracts for outputs, API/DTO/validation shapes, state, side effects, and provider operations; not formal semantic equivalence.
- `scripts/validate_clean_install.py` — builds a wheel and validates it in a temporary isolated environment.
- `docs/releasing.md` — reproducible release checklist and package audit guidance.
- `src/migrationswarm/demo.py` — thin offline/live demonstration wrapper over existing components; not a second orchestrator.
- `examples/demo-commerce-monolith/` — realistic local Spring Boot/Maven commerce fixture.
- `examples/demo-support-monolith/` and `examples/demo-booking-monolith/` — independent Maven benchmark fixtures with root `benchmark.json` contracts.
- `examples/demo-fulfillment-monolith/` — harder Maven benchmark fixture with cycles, shared values, foreign repository access, and cross-domain transaction evidence.
- `docs/evaluation.md` — benchmark methodology, formulas, determinism, and limitations.
- `docs/architecture.md` and `docs/demo.md` — architecture and demonstration notes.
- `.github/workflows/ci.yml` — CI checks.
- `pyproject.toml` — package metadata, dependencies, and tool configuration.
- `alembic.ini` and `migrations/` — committed migration source; packaged installs expose the same assets under the installed data directory.

## Architecture and coding rules

- Keep the `src` layout and import through `migrationswarm`.
- Keep API, CLI, configuration, and core responsibilities separate.
- Prefer small, explicit functions and typed interfaces over speculative abstractions.
- Use Pydantic v2 models/settings for structured configuration and data.
- Keep task lifecycle transitions in `core/tasks/state_machine.py`; do not duplicate transition rules elsewhere.
- Keep dependency edges and readiness logic in `core/scheduler/`; the graph observes task state but does not mutate it.
- Keep local repository inspection in `agents/`; `RepositoryAnalysisAgent` writes only `.migrationswarm/repository-inventory.json` inside the analyzed workspace.
- Keep Java dependency analysis in `agents/dependency_analysis.py`; it reads source files, writes only `.migrationswarm/java-dependency-graph.json`, and must not build or modify application source.
- Keep architecture metrics in `agents/architecture_analysis.py`; it writes only `.migrationswarm/architecture-report.json` and `.migrationswarm/java-dependency-graph.dot`.
- Keep model IDs in the centralized `ModelRegistry`; agents should use `ModelRouter` and never depend directly on providers.
- Keep service graph analysis deterministic and evidence-based in `evaluation/dependency.py`; do not use an LLM for SCCs or migration ordering.
- Treat entity references separately from persistence ownership. Foreign repositories and cross-domain transactions require explicit review findings.
- Keep technical extraction status separate from `review_status`; compiling generated code does not erase review-required findings.
- Keep routing capability-aware and deterministic: same-provider fallbacks precede cross-provider fallbacks, and cross-provider fallback is disabled unless `MODEL_ALLOW_CROSS_PROVIDER_FALLBACK=true`.
- Treat provider credentials as configuration only; `ProviderRegistry` tracks safe current-process eligibility and excludes providers after authentication failure. Keep routing traces free of prompts, source, raw response bodies, and secrets.
- Keep provider credentials in environment/settings only. Never print, log, or write API keys to artifacts; avoid logging full prompts.
- Use `core/security/` for model-output bounds, path containment, secret redaction, and required-artifact loading; fail closed on corrupt artifacts.
- Keep `ServiceBoundaryAgent` grounded to architecture artifacts; require explicit evidence accounting for every strong deterministic candidate, validate class coverage and grounded merges/exclusions, and reject hallucinated classes, duplicate assignments, invalid confidence, and unknown candidate dependencies.
- Keep `MigrationPlanningAgent` planning-only: ground affected classes and step dependencies, require testing and verification, and never modify application source.
- Keep migration plans in `.migrationswarm/migration-plan.json` and `.migrationswarm/migration-plan.md`; validate their step DAG through `core/scheduler/TaskGraph`.
- `RefactorAgent` may receive only `.migrationswarm/worktrees/<task-id>/` as its writable workspace; it uses bounded context, strict JSON full-file changes, and leaves the task at verification.
- `ServiceExtractionAgent` may write only new files under `services/<service-slug>/` in a clean managed task worktree. It consumes grounded boundary, plan, dependency, and architecture artifacts, never deletes or overwrites monolith files, and records its result in `.migrationswarm/extraction-results/<task-id>.json`.
- `ServiceExtractionAgent` uses a fixed 16-file context ceiling; required candidate-owned files are never truncated and optional context is deterministically prioritized within the remaining capacity.
- Service extraction is copy-first: database ownership, messaging, deployment, and changes to the original monolith remain out of scope. The generated service must preserve unresolved/shared dependencies as explicit records or warnings.
- `BuildVerificationAgent` is read-only and may execute only the internally selected Maven or Gradle `test` command in `.migrationswarm/worktrees/<task-id>/`, with `shell=False`, a timeout, bounded result summaries, and complete logs in the main repository metadata.
- `DebugAgent` may receive only bounded failure evidence and extracted-service files, and may only atomically MODIFY or CREATE safe text/source files inside the managed task worktree. `FailureClassifier` is deterministic; only compilation, generated-configuration, and test failures are eligible for at most three configured repairs.
- `MigrationOrchestrator` coordinates one approved service from managed worktree creation through extraction, safe subproject build verification, and `VerificationCoordinator`; it does not duplicate component logic or run multiple candidates.
- `MultiServiceMigrationOrchestrator` schedules explicitly approved services with a dependent-to-prerequisite DAG and at most three service workflows. It delegates each service to `MigrationOrchestrator`, uses separate task worktrees, starts dependents only after `COMPLETED`, and isolates failed branches.
- Service approvals are human-controlled. Cycles, missing prerequisites, and ambiguous branches require `HUMAN_REVIEW`; no order is invented.
- The main repository is immutable with respect to application source during orchestration. Generated files remain in the task worktree, run artifacts remain under `.migrationswarm/runs/`, and failed or successful worktrees are preserved for inspection.
- Orchestration never commits, pushes, creates pull requests, or invokes unbounded retries/debugging.
- Orchestration may run the finite `--max-debug-attempts` repair loop, but never performs unbounded retries or commits/pushes repairs.
- Code-changing agents must never receive the original repository root, use arbitrary shell commands, delete files by default, or run builds/tests as part of refactoring.
- Verification evidence belongs in `.migrationswarm/verification-results/<task-id>.json` and `.migrationswarm/verification-results/<task-id>.log`; never place it inside the task worktree.
- Build verification produces evidence; lifecycle verification decides whether that evidence is sufficient for `COMPLETED`, `FAILED`, or `VERIFYING`.
- `VerificationDecisionEngine` requires passing build status, exit code zero, passing available test counts, existing artifacts, and changed files for code-refactoring tasks. Missing evidence leaves a task in `VERIFYING`; it is never treated as success.
- `GitWorktreeManager` owns only `.migrationswarm/worktrees/`; reject paths outside it, refuse dirty removal without explicit force, and never use destructive Git commands on the main repository.
- Use `structlog` for application logging as logging is introduced.
- PostgreSQL is the durable source of truth for projects, tasks, runs, events, and agent executions. Redis is transient coordination only; it must not become the durable task store.
- `workers/` may use local threads only within the configured bound (currently 1–4); workers re-read task state from PostgreSQL before execution and use `TaskLock` plus `WorkerRuntime` for claims and lifecycle transitions.
- The dispatcher owns scheduler ticks and ready-task enqueueing. Workers never infer dependency readiness, execute after lock contention, or mark `VERIFYING` tasks complete.
- Keep execution-ready work and verification work in separate Redis namespaces. `VERIFYING` is not completion; dependency unlock requires durable `COMPLETED` state.
- Keep repair work in `migrationswarm:debug-tasks`; DEBUG tasks are explicit durable tasks, not invisible retries. `FailureClassifier` is deterministic and repair attempts are bounded at 0..3 (default 2).
- Verification workers load structured evidence, call `VerificationCoordinator`, persist task/decision state, and treat missing or corrupt evidence as insufficient evidence rather than failure.
- Debug workers claim owner-token locks, reload READY DEBUG tasks, persist `RepairAttempt` state, invoke `DebugAgent` through `WorkerRuntime`, run `BuildVerificationAgent` on the original managed worktree, and enqueue the original task for verification. They never complete verification directly.
- Keep SQLAlchemy records and Pydantic domain models separated through `persistence/mapping.py`; repositories own transactions and propagate database errors.
- Never persist API keys, full prompts, complete source files, or unbounded build logs. Artifacts remain filesystem-managed.
- Do not run Alembic migrations automatically during API or CLI startup; use `migrationswarm db-upgrade`.
- Keep package resources independent of the current working directory. Use `migrationswarm doctor`, `migrationswarm config-check`, and `python scripts/validate_clean_install.py` when changing packaging or settings.
- Do not casually add model providers, databases, advanced Java analysis, migration logic, or infrastructure dependencies before their phase is defined.
- `migrationswarm security-check` is a local policy smoke test; `migrationswarm recovery-status PATH` is read-only and never performs automatic recovery.
- `migrationswarm perf --offline` measures existing orchestration, bounded microbenchmarks, and temporary synthetic Java repositories; it must report measured results honestly and never infer enterprise scalability.
- `migrationswarm benchmark --all` includes the intentionally difficult `demo-fulfillment-monolith`; inspect its cycle, foreign-repository, transaction, and review findings rather than calling it clean.
- `migrationswarm validate-run PATH` checks existing run artifacts without repairing them; `migrationswarm release-check --full` runs the bounded release-candidate harness.
- `scripts/validate_release_candidate.py` owns repeatability, fixture, packaging, and bounded soak validation. It must not print secrets or mutate the main application source.

## Commands

```powershell
python -m pip install --editable ".[dev]"
ruff check .
mypy
pytest
uvicorn migrationswarm.api.main:app --reload
migrationswarm --version
migrationswarm doctor
migrationswarm config-check
migrationswarm models
migrationswarm analyze-repo PATH
migrationswarm analyze-dependencies PATH
migrationswarm analyze-architecture PATH
migrationswarm models
migrationswarm model-check PROVIDER
migrationswarm model-check PROVIDER --capability coding
migrationswarm demo PATH --offline
migrationswarm demo PATH --live
migrationswarm benchmark PATH
migrationswarm benchmark --all
migrationswarm benchmark --fixture demo-commerce-monolith
migrationswarm benchmark --all --baseline
python -m build
python scripts/validate_clean_install.py
migrationswarm propose-boundaries PATH [--dry-run]
migrationswarm plan-migration PATH --service SERVICE_NAME [--dry-run] [--show-dag]
migrationswarm git-status PATH
migrationswarm create-worktree PATH --task-id TASK_ID
migrationswarm worktrees PATH
migrationswarm remove-worktree PATH --task-id TASK_ID [--force]
migrationswarm refactor PATH --task-id TASK_ID --instruction "..." [--dry-run]
migrationswarm extract-service PATH --task-id TASK_ID --service SERVICE_NAME [--dry-run]
migrationswarm migrate-service PATH --service SERVICE_NAME [--timeout SECONDS] [--max-debug-attempts 0..3] [--dry-run]
migrationswarm approve-service PATH --service SERVICE_NAME
migrationswarm revoke-service PATH --service SERVICE_NAME
migrationswarm approvals PATH
migrationswarm migrate PATH --service SERVICE_NAME --service-workers 2 [--dry-run]
migrationswarm migrate PATH --all-approved --service-workers 2 [--dry-run]
migrationswarm migration-status PATH
migrationswarm verify-build PATH --task-id TASK_ID [--timeout SECONDS]
migrationswarm decide-verification PATH --task-id TASK_ID
migrationswarm db-status
migrationswarm db-upgrade
migrationswarm redis-status
migrationswarm workers PATH
migrationswarm run-swarm PATH --workers 2 [--poll-interval SECONDS] [--dry-run]
migrationswarm swarm-status PATH
migrationswarm repair-status PATH
migrationswarm recovery-status PATH
migrationswarm security-check
migrationswarm perf --offline [--fixture commerce] [--iterations 7]
```

## Files to treat carefully

Review changes to `pyproject.toml`, `src/migrationswarm/config/`, public API routes, CI workflow files, and licensing files. Avoid changing contracts or dependency versions without updating the relevant tests and documentation.

## Agent workflow and definition of done

Inspect relevant files, understand existing tests, make the smallest correct change, run targeted tests, run full validation, and summarize changes. A change is done when its behavior is tested, `ruff check .`, `mypy`, and `pytest` pass, documentation/configuration is current, and no out-of-scope phase work was added.

## Future architecture notes

The current single-service `MigrationOrchestrator` is synchronous. `MultiServiceMigrationOrchestrator` adds only bounded service-level concurrency and dependency scheduling around it; it does not split databases, deploy services, commit/push changes, or run autonomous migration loops. Later phases may add richer scheduling and Docker sandboxing. Introduce each later capability behind clear interfaces and tests when its phase begins.
