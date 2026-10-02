# Contributing to MigrationSwarm

MigrationSwarm is an experimental Java modernization system. Keep changes
small, evidence-based, and within the current bounded orchestration scope.
Read [AGENTS.md](AGENTS.md) before changing the repository.

## Setup

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --editable ".[dev]"
```

Python 3.11 or newer is supported. The offline demo and tests do not require
provider credentials, PostgreSQL, or Redis.

## Validation commands

```powershell
ruff check .
mypy
pytest
python scripts/validate_clean_install.py
```

For fixture and release checks, see [docs/releasing.md](docs/releasing.md).

## Coding standards

- Keep the `src/` layout and import through `migrationswarm`.
- Prefer small typed functions and explicit contracts.
- Keep lifecycle transitions in `core/tasks/state_machine.py`.
- Keep dependency edges and readiness in `core/scheduler/`.
- Keep model IDs in `ModelRegistry` and provider credentials in settings.
- Use structured, secret-free logging and bounded artifacts.
- Do not add speculative abstractions or unrelated refactors.

## Safety constraints

- Never add credentials, tokens, prompts, or complete source files to tests,
  fixtures, issues, logs, or documentation.
- Do not modify `.env` or commit secret-bearing files.
- Do not pass the original repository root as a writable code-change workspace.
- Do not bypass worktree, path-containment, subprocess, or artifact policies.
- Do not add automatic commits, pushes, resets, cleanup, or unbounded retries.
- Treat technical verification and semantic evaluation as separate outcomes.
- Do not introduce providers, databases, queues, frontend code, or deployment
  systems without a defined project phase.

## Adding a benchmark fixture

Add a self-contained Maven fixture under `examples/` with:

1. a `pom.xml` and deterministic tests;
2. a root `benchmark.json` with expected services and package metadata;
3. typed behavior contracts grounded in source evidence; and
4. tests covering matching, extraction, verification, and review findings.

Run the four-fixture validation and inspect both technical and semantic status.
Do not hide a difficult or unsafe fixture by changing its expected result.

## Adding semantic contracts

Add contracts to the fixture metadata only for observable behavior that can be
grounded in source evidence. Keep outputs, statuses, errors, state transitions,
side effects, API/DTO shapes, validation, and provider operations explicit.
Use allowlisted normalization only for genuinely nondeterministic fields. Add
tests for pass, mismatch, and unavailable generated observations.

## Adding provider support

Add a provider-neutral implementation behind the existing provider protocol and
registry. Keep provider model IDs centralized, capability-aware, bounded, and
secret-free. Add configuration, eligibility, routing, redaction, failure, and
fallback tests. Cross-provider fallback must remain opt-in unless the project
phase explicitly changes that policy.

## Regression expectations

Every behavior change needs targeted tests and a full validation run. A change
is complete when behavior is tested, Ruff, mypy, and pytest pass, documentation
matches the implementation, release-facing files contain no machine-specific
content or secrets, and no out-of-scope phase work was added.
