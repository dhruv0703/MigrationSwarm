# Design Decisions

These short ADR-style notes record the decisions that shape the current
release candidate.

## 1. Deterministic analysis before model reasoning

**Context:** Model reasoning is useful for interpretation but can invent facts.

**Decision:** Inventory, dependency edges, architecture metrics, ownership
findings, and DAG validation run deterministically before model-assisted steps.

**Tradeoff:** The parser is narrower than a full compiler model, but proposals
are grounded in inspectable evidence.

## 2. Bounded structured model context

**Context:** Whole-repository prompts increase cost, leakage, and ambiguity.

**Decision:** Agents receive bounded structured artifacts and strict output
contracts through the provider-neutral router.

**Tradeoff:** Some context is omitted and may require another deterministic
analysis step, but the model boundary is inspectable and size-limited.

## 3. PostgreSQL as durable authority

**Context:** Task state must survive worker restarts and coordinate ownership.

**Decision:** PostgreSQL stores durable projects, tasks, runs, events, agent
executions, repair attempts, and verification decisions.

**Tradeoff:** An external database is operationally heavier than process memory,
but Redis cannot become the source of truth.

## 4. Redis as transient coordination

**Context:** Queues, locks, and heartbeats need fast expiring coordination.

**Decision:** Redis stores transient queue entries, owner-token locks, and
heartbeats only.

**Tradeoff:** Redis availability affects dispatch, but durable state remains
recoverable and authoritative in PostgreSQL.

## 5. Git worktrees for all code changes

**Context:** A migration worker must not contaminate the source repository.

**Decision:** Code-changing tasks write only inside managed task worktrees.

**Tradeoff:** Worktree lifecycle and disk usage are more complex, but source
immutability and branch isolation are explicit.

## 6. Verification separate from execution

**Context:** An agent claiming success is not build or behavior evidence.

**Decision:** Worker execution stops at `VERIFYING`; a separate verifier decides
whether evidence supports `COMPLETED`.

**Tradeoff:** The lifecycle has more states, but evidence cannot be skipped.

## 7. Repair as explicit DEBUG tasks

**Context:** Invisible retries make failures difficult to audit and bound.

**Decision:** Eligible failures create durable DEBUG work with an attempt record
and a finite repair limit.

**Tradeoff:** Repair takes more orchestration steps, but operators can inspect
every attempt and outcome.

## 8. Explicit service approvals

**Context:** A technically plausible boundary may still be organizationally or
architecturally unsafe.

**Decision:** Service approvals are human-controlled metadata and are never
populated automatically.

**Tradeoff:** The system cannot run fully unattended, but it does not turn
analysis confidence into authorization.

## 9. Cross-provider fallback disabled by default

**Context:** Silent provider changes can alter behavior, cost, or data handling.

**Decision:** Same-provider candidates are preferred and cross-provider fallback
requires explicit configuration.

**Tradeoff:** A route can fail sooner, but provider choice remains predictable.

## 10. Only COMPLETED dependencies unlock work

**Context:** `VERIFYING` work has not yet passed evidence-based review.

**Decision:** Scheduler readiness requires every dependency to be durably
`COMPLETED`.

**Tradeoff:** Downstream work waits longer, but partial success cannot race ahead.

## 11. Technical versus semantic success

**Context:** Compiling code can still change observable behavior.

**Decision:** Build/test/verification status and typed semantic-contract status
are recorded separately.

**Tradeoff:** Reports are more nuanced, but a green build is not mislabeled as
behavioral equivalence.

## 12. Fulfillment blocked instead of force-extracted

**Context:** Cycles, foreign repositories, and cross-domain writes provide no
safe automatic extraction seam.

**Decision:** Fulfillment enters `review_required` and semantic evaluation stays
`not_evaluated` until a human chooses a safe boundary.

**Tradeoff:** The benchmark does not maximize extraction count, but it preserves
the safety signal instead of fabricating a successful result.
