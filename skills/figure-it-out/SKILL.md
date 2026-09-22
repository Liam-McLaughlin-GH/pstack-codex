---
name: figure-it-out
description: Design and run a bounded, auditable workflow for a large, ambiguous, or multi-part task when no narrower workflow fits. Use it when the task needs a task-specific plan, finite checkpoints, independent phase verification, resume after interruption, or an explicit blocked or out-of-scope result.
---

# Figure it out

Use this skill when the work needs a bespoke workflow. Define the outcome and
the proof of completion before broad implementation. Keep the run finite,
restartable, and legible to someone returning later.

The deterministic helper in [`scripts/figure_it_out.py`](scripts/figure_it_out.py)
stores a run contract and enforces its state transitions. Read the
[workflow contract](references/codex-workflow-contract.md) before creating a
run. The helper is optional for a tiny task, but a multi-phase run must leave a
state artifact and a decision trail.

## Start with the contract

Write a task-specific plan before changing the broad implementation. The plan
must name all of the following:

- **Desired outcome.** State the artifact or behavior that should exist when the
  run ends.
- **Scope.** List the paths, systems, actors, and actions included. List the
  exclusions separately.
- **Constraints.** Record compatibility, safety, ownership, time, dependency,
  and permission limits that shape the work.
- **Terminal predicate.** Give a falsifiable statement and the named checks
  that must pass. "Looks good" is not a predicate.
- **Finite phases.** Order the riskiest unknown first. Every phase needs an
  hypothesis, expected output, and an independent verification check.
- **Run limits.** Set maximum phase attempts, replans, checkpoints, and
  decisions. Increase a limit only through an explicit replan.

Create the run with `figure_it_out.py init`. Do not start implementation until
the helper accepts this plan. The plan is the first output a reviewer checks.

Read the [poteto-mode guidance](../poteto-mode/SKILL.md) and its relevant leaf
principles before choosing the workflow shape. In particular, use
[foundational thinking](../principle-foundational-thinking/SKILL.md) for the
phase data shape, [sequence-verifiable-units](../principle-sequence-verifiable-units/SKILL.md)
for ordering, and [prove-it-works](../principle-prove-it-works/SKILL.md) for
the terminal predicate.

## Frame and design

1. Capture the desired outcome, scope, constraints, terminal predicate, and
   limits in the plan artifact.
2. Identify the smallest useful verification harness and capture its baseline
   before implementation. A baseline is evidence, not a statement that the
   old system was correct.
3. Split work into phases that can be checked without trusting the next phase.
   Put the highest-risk unknown first. State what the phase predicts, make its
   output concrete, and measure the result against its check. Keep a change
   only when it advances the terminal predicate. Revert or replan a change
   that does not.
4. Use a design comparison only when the boundary is hard to reverse. Keep
   independent workers in separate worktrees or output paths when delegation is
   useful. Delegation is optional and never replaces the parent run's checks.
5. Record the design choice and its evidence with `decision`. Do not hide a
   discarded approach or an unresolved assumption.

The [Codex workflow contract](references/codex-workflow-contract.md) defines
the JSON fields and transition table. The [decision-trail guidance](../show-me-your-work/SKILL.md)
defines the canonical TSV shape for the exported audit trail.

## Run one phase at a time

Use `start-phase` only for the current pending phase. A phase is complete only
when both of these exist:

1. The actual output is recorded.
2. Its declared verification check returns `passed: true` with evidence.

After a phase passes, the helper selects the next phase but does not start it.
That boundary is deliberate. Inspect the output and run before advancing.

Use `checkpoint` during a phase. Every checkpoint records a label, evidence,
and a concrete next action. The configured checkpoint limit is hard. When it is
reached, stop and replan or close the run instead of extending the loop
silently.

Keep the decision trail append-only in the run state. Record a decision for a
fork, a completed unit, a replan, a revert, or a blocker. Export it to one TSV
path with `audit` when a reviewer needs a durable trail.

## Failure, replan, and interruption

When a phase fails, use `fail-phase` with the failure evidence and a concrete
next action. The run enters `failed`. Starting the failed phase again is
refused until `replan` records why the approach changed and what will be
checked next. Never continue past a failed phase by changing only the prose
status.

When the process or session stops, use `interrupt` after a checkpoint. A fresh
process can call `resume` on the same state file. Resume preserves completed
outputs, decisions, checkpoints, failure history, and the current phase. It
does not restart completed phases or erase the interruption evidence.

When progress cannot continue, use `block`. A blocked state requires evidence
of the blocker and a concrete next action or owner. A missing tool, permission,
fixture, or product decision is an explicit gap, not a passing phase.

Use [never-block-on-the-human](../principle-never-block-on-the-human/SKILL.md)
for reversible execution choices. Still stop and record a blocked state for a
real permission, ownership, safety, or product decision boundary.

## Refuse out-of-scope actions

Before any action with an external or durable effect, call `guard-action`. It
allows only actions and relative targets named by the run scope. It refuses
undeclared deployment, push, merge, deletion, credential, message, production,
or path-escaping actions and records the refusal in the decision trail. A
refusal must include why the action is outside scope and the safe next action.
Do not reinterpret a refusal as approval and do not perform the action through
another worker.

Keep the [laziness protocol](../principle-laziness-protocol/SKILL.md) and
[boundary discipline](../principle-boundary-discipline/SKILL.md) in view when
adding guards. Put recurring failures in the helper or a check, as described by
[encode-lessons-in-structure](../principle-encode-lessons-in-structure/SKILL.md),
rather than repeating a warning in every phase.

## Finish and hand back

Call `predicate` for every named terminal check with its evidence. Call
`finish` only after every phase is passed and every predicate check is passed.
The helper returns a completed state only when the terminal predicate is true.
An inconclusive, missing, or failed check keeps the run open or blocked.

Hand back the state path, decision-trail path, exact changed files, phase
outputs, verification commands and results, unresolved gaps, and the next
action. Distinguish `verified`, `failed`, `blocked`, and `not run`. Do not
claim completion from a self-report or a generated checklist.

## Provenance

This skill is a Codex-native adaptation of the upstream pstack skill at
[`pstack/skills/figure-it-out/SKILL.md`](https://github.com/cursor/plugins/blob/53e579f1481697931fc44f5445171397cfa2b24b/pstack/skills/figure-it-out/SKILL.md)
from commit `53e579f1481697931fc44f5445171397cfa2b24b`. The upstream project
is MIT licensed; the repository [LICENSE](../../LICENSE) remains the license
for this port. The phase loop, scientific hypothesis checks, audit trail, and
verification handoff are preserved. Runtime-specific routing and storage are
replaced by the local Codex contract above.
