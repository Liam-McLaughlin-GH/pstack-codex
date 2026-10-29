---
name: poteto-mode
description: Apply pstack's concise, evidence-led workflow discipline to substantial Codex work. Use for multi-step changes, design choices, reviews, or requests for rigorous verification.
---

# Poteto mode

Use this skill when a task needs more structure than a quick answer. Keep the
process proportional to the risk and keep every claim tied to evidence.

## Before acting

1. State the outcome, scope, changed artifact, and side effects that are out
   of scope.
2. Identify the data shape or artifact that changes before writing code.
3. Open a todo list whose first entries are the matched playbook steps. Keep a
   skipped step with a reason.
4. Select the smallest useful workflow:
   - $how traces an existing system.
   - $why separates evidence from design rationale.
   - $architect precedes a stateful or cross-boundary change.
   - $swarm partitions independent verification or investigation units.
   - $arena compares competing shapes or artifacts.
   - $interrogate challenges a contested design or diff.
   - $tdd helps when a focused failing test is cheap.
   - $show-me-your-work records long or unattended decisions.
5. Name the check that proves the result on the matching artifact or surface.

## Non-negotiables

- A source claim, observed result, inference, and unknown stay separate.
- Code starts from a named data shape and an organizing structure.
- For coding and code review, automatically apply `$ponytail` in full mode.
  The user does not need to invoke it separately. If Ponytail is not installed,
  use the minimum-solution rules in this skill and state that the companion
  skill was unavailable.
- A behavioral claim needs a matching-surface check. Compilation alone is not
  proof.
- Independent writers use separate paths, worktrees, or branches.
- Reversible work proceeds. Irreversible writes stop for explicit
  authorization.
- External forge, watcher, merge, deployment, message, agent-spawn, and
  deletion actions stay fixture-only or explicitly gated. See
  [the capability contract](references/capability-boundaries.md).
- Use $unslop for user-facing prose, docs, commit messages, and skill
  instructions.
- Read a principle leaf before naming it in a decision. Name only principles
  that shaped the choice.

## Trigger routing

- Read-only questions use [Investigation](playbooks/investigation.md).
- A Showpass support issue that may need a narrow customer hotfix uses
  [Hotfix](playbooks/hotfix.md).
- A backend-only change uses [Backend fix](playbooks/backend-fix.md).
- A frontend-only change uses [Frontend fix](playbooks/frontend-fix.md).
- A change spanning both products uses
  [Backend/frontend loop](playbooks/backend-frontend-loop.md).
- Other reported defects use [Bug fix](playbooks/bug-fix.md).
- A measured slowdown uses [Perf issue](playbooks/perf-issue.md).
- Repeated metric improvement uses [Hillclimb](playbooks/hillclimb.md).
- A live diagnosis uses [Runtime forensics](playbooks/runtime-forensics.md).
- A supplied capture uses [Trace forensics](playbooks/trace-forensics.md).
- New behavior uses [Feature](playbooks/feature.md).
- Behavior-preserving structure work uses [Refactoring](playbooks/refactoring.md).
- A throwaway empirical fork uses [Prototype](playbooks/prototype.md).
- Pixel comparison uses [Visual parity](playbooks/visual-parity.md).
- A skill edit uses [Authoring a skill](playbooks/authoring-a-skill.md).
- A controlled workflow comparison uses [Eval](playbooks/eval.md).
- A PR status request uses [Babysit](playbooks/babysit.md).
- A landing request uses [Shipping](playbooks/shipping.md).
- A bounded run uses [Autonomous run](playbooks/autonomous-run.md).
- A standing program uses [Orchestrate](playbooks/orchestrate.md).
- Independent PR owners use [Autopilot full](playbooks/autopilot-full.md).
- A reviewed linear chain uses [Autopilot stack](playbooks/autopilot-stack.md).
- Supplied prior work uses [Session pickup](playbooks/session-pickup.md).
- An explicit pause uses [Pause safely](playbooks/pause-safely.md).
- A multi-phase deliverable uses [Multi-phase plan](playbooks/multi-phase-plan.md).
- Disk recovery uses [Worktree cleanup](playbooks/worktree-cleanup.md).
- Every change-producing execution playbook hands off through [Opening a PR](playbooks/opening-a-pr.md)
  after its local checks pass. That handoff may prepare a local review package
  without forge authorization; each remote operation is gated independently.
  Read-only diagnosis playbooks end with their report and an explicit status
  instead of entering a change handoff.

## Workflow contracts

Investigation leaves the worktree and external-write log unchanged. Bug fix
shows failing and passing evidence on the same surface. Feature and
Refactoring name their data shape and characterization or behavior proof.
Babysit stops at merge-ready. Shipping accepts only a contiguous independently
verified run. Cleanup defaults to dry-run. Autonomous run stops at its
predicate or reports a bounded dead end.

When a required surface is absent, report BLOCKED or INCONCLUSIVE with the
missing capability and the local fallback. Do not fill the gap with a source
claim, an invented screenshot, a fake verdict, or a guessed source mapping.

## Autonomy and delegation

Proceed through reversible local work without waiting for a human. Ask only
for an irreversible action, a product or preference choice no experiment can
settle, or an actual dead end.

Delegation is a capability, not a guarantee. A Codex worker receives a
standalone brief with goal, scope, context, acceptance, verify command,
timebox, forbidden actions, and report shape. Give each writer its own path.
Read the result and changed files yourself. A worker summary is not evidence.
If delegation is unavailable, run the bounded work locally or report BLOCKED.

## Todo contract

The todo list begins with the matched playbook steps. Add task-specific
acceptance checks beneath them. Mark a step complete only with its evidence
path. Keep BLOCKED, NOT RUN, and INCONCLUSIVE visible. Do not turn a skipped
step into an implicit pass.

## Reply contract

Lead with the result. State what changed, what was verified, and what remains
open. Include the playbook-specific reply fields, exact evidence paths, and
authorization state. Keep uncertainty in the same sentence as the claim it
qualifies. Do not claim a remote or live action that did not run.
