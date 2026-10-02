# Releasing MigrationSwarm

MigrationSwarm is not published, tagged, or pushed automatically. The v0.1.0
release candidate is an offline validation target, not a production-readiness
certification.

## Mandatory offline release validation

Run these checks from the repository root:

```powershell
python -m pip install --editable ".[dev]"
ruff check .
mypy
pytest
python scripts/validate_release_candidate.py --fixtures-only
migrationswarm demo ./examples/demo-commerce-monolith --offline
migrationswarm benchmark --all
migrationswarm perf --offline
migrationswarm security-check
python scripts/validate_clean_install.py
python -m build
python -m pip check
git diff --check
```

Also inspect the generated wheel and source distribution for secrets, caches,
worktrees, generated `.migrationswarm` output, and machine-specific paths. Run a
secret scan over release-facing files and confirm that the package version,
README, `CHANGELOG.md`, and status document agree.

The bounded shortcut is:

```powershell
migrationswarm release-check --skip-expensive --skip-soak
```

The release harness runs Ruff, mypy, pytest, security policy checks, and clean
installation in the bounded path. Its full mode additionally runs repeatability,
fixture, benchmark, performance, package-build, and pip-check checks according
to the selected flags.

## Evaluation review

Review the normal benchmark for:

- all four fixture statuses;
- boundary precision, recall, and F1;
- extraction, build, test, and verification denominators;
- semantic contract totals and unavailable contracts;
- Fulfillment review findings; and
- absence of fabricated completion for blocked work.

The expected current state is Commerce, Support, and Booking completed, with
Fulfillment `review_required` and `not_evaluated` semantically.

## Optional environment-dependent validation

These checks are useful but are not required for the offline release candidate:

- PostgreSQL connectivity and integration tests;
- Redis queues, locks, and heartbeat behavior against an external service;
- live Groq or SiliconFlow health, quotas, and model latency; and
- Docker-host integration.

Do not expose credentials in logs or reports. Do not run live model calls as part
of the offline release check.

## Release safety

Before any future publication, confirm that:

- no automatic commit, push, tag, reset, or cleanup occurred;
- API keys, tokens, prompts, source files, and full logs are absent from artifacts;
- `.env` is not packaged;
- `git diff --check` is clean; and
- the release notes make no production-readiness claim.

Publishing and tagging require explicit operator action and are outside this
repository validation document.
