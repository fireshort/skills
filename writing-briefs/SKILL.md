---
name: writing-briefs
description: Use when turning a feature idea, product problem, UX change, or uncertain request into a high-level requirements brief before writing a technical spec or implementation plan, especially when scope, user value, non-goals, acceptance criteria, or requirement trade-offs need alignment.
---

# Writing Briefs

## Overview

Write a brief when the next useful artifact is requirement alignment, not technical design. A brief answers what problem to solve, for whom, what must be true when done, and what is deliberately out of scope. Specs decide how to satisfy it; plans decide how to implement it.

Use the project's normal language for the generated brief. Keep this skill itself generic and compact.

## Default Location

Save briefs to `docs/superpowers/briefs/YYYY-MM-DD-<topic>-brief.md` unless project rules or the user specify another location.

## Workflow

1. Check project rules and nearby briefs, specs, and plans before writing.
2. Restate the request as a verifiable requirement goal.
3. Separate product decisions from implementation details. Keep code paths, event chains, schemas, libraries, and patch steps out unless they are explicit user-facing constraints.
4. Ask only for missing information that would change the requirement. Otherwise make the most reasonable assumption, label it, and continue.
5. Draft the brief, then self-review it before saving or presenting.
6. After the user approves the brief, continue to the appropriate design or spec workflow. If a spec or plan already exists, revise it against the approved brief before implementation.

## Brief Template

```markdown
# <Feature or Change> - Brief

- Date: YYYY-MM-DD
- Status: Brief pending review
- Links: <existing spec, plan, issue, or source request if any>

## Problem
<What is broken, missing, confusing, costly, or risky from the user's point of view.>

## Users and Scenarios
<Who experiences this, and in what concrete workflow.>

## Goals
- <User-visible outcome 1>
- <User-visible outcome 2>

## Non-Goals
- <Scope intentionally excluded from this change>

## Requirements
- <Observable behavior or rule>
- <Observable behavior or rule>

## Acceptance Criteria
- <How a reviewer or user can tell the requirement is satisfied>
- <Important regression that must remain unchanged>

## Constraints and Assumptions
- <Product, platform, privacy, performance, compatibility, or policy constraint>

## Open Questions
- <Only questions that would change the requirement or scope>
```

## Quality Bar

Before handing off the brief, check:

- A non-engineer stakeholder can change the desired behavior without reading implementation details.
- Every goal and acceptance criterion is observable in the product or user workflow.
- Every non-goal actually removes scope.
- Technical details are either absent or explicitly justified as user-facing constraints.
- Open questions are few and decision-relevant; routine implementation questions belong in the spec.

## Common Mistakes

| Mistake | Fix |
|---|---|
| Writing a design doc under a brief title | Move architecture, code paths, data flow, and event semantics to the spec. |
| Skipping non-goals | Add exclusions before scope leaks into the spec and plan. |
| Asking the user to choose every detail | Recommend the default, record the assumption, and ask only when it changes scope. |
| Treating the plan as source of truth | The approved brief governs the spec and plan; update downstream documents when requirements change. |
| Vague acceptance criteria | Rewrite them as observable outcomes or concrete regression checks. |
