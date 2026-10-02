# MigrationSwarm security boundaries

MigrationSwarm is a local teaching and evaluation harness. This document
describes the current bounded orchestration controls; it is not a formal
security certification or production threat assessment.

## Threat model

The main untrusted inputs are local repositories, model/provider responses,
artifact files, environment configuration, subprocess output, and transient
Redis state. A malicious repository or model response may attempt path
traversal, symlink escape, protected-metadata writes, oversized input/output,
malformed JSON, command injection, secret exfiltration, or partial filesystem
updates.

## Repository and filesystem boundaries

The main application source is read-only during orchestration. Code-changing
work is confined to managed Git worktrees under `.migrationswarm/worktrees/`.
Generated paths are normalized as cross-platform relative paths, protected
directories are rejected, and existing symlink components cannot redirect a
write outside the authorized root.

Artifact, model-response, repository-scan, and build-log sizes are bounded.
Writes use temporary files and replacement where practical. Required artifacts
are loaded fail-closed: missing, malformed, or oversized evidence is reported
as corrupt or insufficient rather than regenerated silently.

## Secrets and model output

Provider credentials use Pydantic `SecretStr` settings. Credentials are not
printed, logged, persisted, or written to benchmark artifacts. Provider errors,
CLI failures, persisted metadata, and verification logs use the central
redaction policy. Prompts, complete source files, raw response bodies, and
unbounded logs are not durable state.

Model output is untrusted. Structured output is size-limited, schema-validated,
and checked against grounded repository evidence before code-changing work can
proceed.

## Subprocess and resource policy

Build verification executes only an internally selected Maven or Gradle `test`
command, with `shell=False`, an explicit timeout, and bounded result summaries.
Windows wrapper execution uses an explicit `cmd.exe` argument vector and does
not enable shell mode. Repository, model-response, artifact, subprocess, and
log bounds are enforced by reusable security helpers.

The current harness does not provide OS-level sandboxing or quotas for every
filesystem operation. Review process permissions, dependency updates, and
environment files before handling sensitive repositories.

## Recovery and durable state

`recovery-status` inspects durable tasks, heartbeats, locks, and preserved
worktrees. It never retries, requeues, resets, deletes, commits, pushes, or
mutates task state automatically. Human operators decide how to resume after
reviewing evidence.

PostgreSQL is the durable authority. Redis is transient coordination and may
contain queue IDs, locks, and heartbeats only. External service ACLs, encrypted
storage, authenticated multi-user access control, and production deployment
hardening are outside this phase.

## Security-check scope

`migrationswarm security-check` is a local policy smoke test covering path
containment, subprocess shell policy, destructive Git policy, secret scanning,
and conservative recovery. A passing result means the checked repository
policies pass; it does not certify the complete system or its environment.
