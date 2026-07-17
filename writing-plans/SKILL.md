---
name: writing-plans
description: Use when requirements or a technical spec are settled and a multi-step change needs an actionable file-level implementation sequence, dependency ordering, or explicit verification. Do not use for simple localized edits that can be implemented and validated safely without a separate plan.
---

# Writing Plans

## Purpose

Turn settled requirements or an approved spec into a proportional implementation plan. A plan tells an implementer what to change, in what order, and how to prove it works. It does not reopen product or architecture decisions and does not expand every obvious edit into minute-by-minute steps.

## Default Location

Save plans to `docs/plans/YYYY-MM-DD-<topic>.md` unless project rules, an existing convention, or the user specifies another location. Preserve existing historical paths rather than renaming them for this skill.

## Workflow

1. Read project rules, the governing brief or spec, and the actual source, configuration, tests, scripts, and downstream consumers affected by the change.
2. If there is no real multi-step sequence, dependency, validation boundary, ownership boundary, or rollback boundary, stop without producing a placeholder plan and recommend direct implementation.
3. Confirm that no blocking requirement or design decision remains. If implementation would decide product scope, a public contract, security, migration, or architecture, return to the appropriate brief or spec instead of hiding the decision in the plan.
4. Define the smallest safe sequence of tasks. Name target files or discoverable locations, describe the intended change, capture dependencies, and attach a concrete verification step to each meaningful task.
5. Use exact commands when they are known from the project. Do not invent commands, APIs, file paths, or test facilities; mark discovery or manual validation where needed.
6. Add rollout, compatibility, data migration, rollback, or documentation work only when the change creates that obligation.
7. Self-review the plan and follow the project's review rules. Do not automatically start implementation, create a worktree, delegate to subagents, require test-first mechanics, or present branch-finishing choices.

## Planning Granularity
- Keep a single cohesive edit together when splitting it would add coordination without reducing risk.
- Split tasks at real dependency, validation, ownership, or rollback boundaries.
- Include code snippets only when an exact contract or non-obvious algorithm must be preserved; otherwise describe intent and point to the relevant symbols.
- Prefer targeted tests and smoke checks first, then broader validation when the impact justifies it.

## Plan Template

```markdown
# <Change> - Implementation Plan

- Date: YYYY-MM-DD
- Status: Plan pending review
- Inputs: <brief, spec, issue, or request>

## Goal
<The outcome this plan will deliver.>

## Scope
- In scope: <files, components, or behaviors>
- Out of scope: <explicit exclusions>

## Tasks

### 1. <Meaningful implementation unit>
- Files: `<path>` and relevant symbols
- Change: <what to add, remove, or preserve>
- Depends on: <earlier task or external condition, if any>
- Verify: `<targeted command>` or <specific manual/runtime check>

### 2. <Next meaningful implementation unit>
- Files: `<path>`
- Change: <intended behavior>
- Verify: <evidence required>

## Final Validation
- <Integrated regression, build, runtime, browser, or client check proportionate to risk>

## Rollout and Rollback
<Include only when deployment, migration, compatibility, or irreversible state warrants it.>

## Open Blockers
<Only issues that prevent safe implementation.>
```

## Quality Bar

Before handing off the plan, check that:

- Every task traces to a settled requirement or design decision.
- File paths, symbols, commands, and dependencies were checked against the real project.
- Each meaningful task has evidence that can show whether it succeeded.
- The sequence exposes risky changes early and avoids unrelated refactoring.
- The plan is detailed enough to execute without becoming a transcript of trivial edits.
