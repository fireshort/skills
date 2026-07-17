---
name: writing-specs
description: Use when settled requirements need a technical design before implementation planning, especially for architecture, cross-module behavior, interfaces, data flow, persistence or data models, authorization or permissions, compatibility, failure handling, security, performance, or migration decisions. Do not use for simple localized changes whose implementation and validation path are already obvious.
---

# Writing Specs

## Purpose

Write the smallest technical design that resolves the decisions an implementer should not rediscover. A brief defines the problem and requirements; a spec defines how the system should satisfy them; a plan sequences the implementation.

Do not turn an obvious local change into a design ceremony.

## Default Location

Save specs to `docs/specs/YYYY-MM-DD-<topic>-design.md` unless project rules, an existing convention, or the user specifies another location. Preserve existing historical paths rather than renaming them for this skill.

## Workflow

1. Read project rules, the approved brief or settled requirements, nearby design documents, and the relevant source, configuration, tests, dependencies, and consumers.
2. If no material technical decision remains, stop without producing a placeholder spec. Recommend direct implementation, or `writing-plans` only when real sequencing, dependency, or verification boundaries remain.
3. Identify only the decision surfaces that matter: architecture, component boundaries, control and data flow, interfaces, persistence and schemas, authorization and permissions, failure behavior, compatibility, observability, performance, security, and migration. Omit irrelevant sections.
4. If one safe solution follows clearly from the evidence, record the assumptions and proceed. If a genuine unresolved decision remains, compare only plausible alternatives, recommend one, and explain the trade-off. Do not force a fixed number of options.
5. Ask the user only when the choice materially changes product scope, a public contract, security, migration, irreversible risk, or another user-owned decision that evidence cannot settle. Otherwise choose the most reasonable default and label it.
6. Verify feasibility against the real project. Distinguish confirmed behavior from assumptions and mark unavailable runtime checks explicitly.
7. Write and self-review the spec, then follow the project's review rules. Do not require section-by-section approval, a separate visual companion, a commit, a worktree, subagents, or an implementation plan unless the task independently warrants them.

## Spec Template
```markdown
# <Change> - Technical Design

- Date: YYYY-MM-DD
- Status: Design pending review
- Inputs: <brief, issue, request, or existing design>

## Context and Requirements
<The settled behavior this design must satisfy. Link rather than duplicate a brief when possible.>

## Current System
<Relevant code paths, constraints, and evidence from the repository or runtime.>

## Proposed Design
<Components, responsibilities, control flow, and data flow.>

## Interfaces and Data
<Public or internal contracts, events, schemas, persistence, and compatibility only when relevant.>

## Failure and Edge Cases
<Errors, retries, concurrency, fallback, cleanup, and observability only when relevant.>

## Security, Performance, and Migration
<Include only applicable risks, rollout constraints, and mitigations.>

## Verification Strategy
<Tests, runtime checks, and acceptance evidence needed to validate the design.>

## Alternatives Considered
<Only real alternatives and why the recommendation is preferable.>

## Open Questions
<Only unresolved decisions that could change the design.>
```

## Quality Bar

Before handing off the spec, check that:

- Every material design claim is grounded in the actual project or clearly labeled as an assumption.
- The design resolves important boundaries and failure behavior without prescribing trivial patch mechanics.
- Requirement changes have been returned to the brief or request instead of being smuggled into the design.
- Alternatives appear only where a real trade-off exists.
- The verification strategy can detect the most likely failures introduced by the design.
