---
name: poteto-mode
description: Apply pstack's concise, evidence-led workflow discipline to substantial Codex work. Use for multi-step changes, design choices, reviews, or requests for rigorous verification.
---

# Poteto mode

Use this skill when a task needs more structure than a quick answer. Keep the
process proportional to the risk and keep every claim tied to evidence.

## Liam's implementation goals

When drafting or executing Liam's implementation `/goal`, load `$codex-prompt-intake` and its `references/implementation-goal-contract.md` from the installed skill directory (canonical source: `CodexMaxxing/skills/codex-prompt-intake`). This contract owns the complete prompt, scope preservation, code examples, model settings, Jira lifecycle, CLI-only Coda access, and Galileo review gate. Require `$ponytail` full for coding and review. Preserve previous authorization and corrections; a request for a prompt does not execute the goal. Use `$avoid-ai-writing` for the final prose pass.

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
- A behavioral claim needs a matching-surface check. Compilation alone is not
  proof.
- Independent writers use separate paths, worktrees, or branches.
- Reversible work proceeds. A change-producing request authorizes pushing its
  scoped commits and opening its review PR after local checks pass. Other
  irreversible writes stop for explicit authorization.
- External Herdr, watcher, merge, deployment, message, and deletion actions
  stay fixture-only or explicitly gated. An explicitly requested Showpass PR
  lifecycle supplies the scoped review/Jira/QA-publication authority described
  in its playbook; it does not authorize merge or deployment. Codex-native worker creation
  is a separate, checked capability. See [the capability contract](references/capability-boundaries.md).
- Use $unslop for user-facing prose, docs, commit messages, and skill
  instructions.
- Read a principle leaf before naming it in a decision. Name only principles
  that shaped the choice.

Before coding, testing, reviewing, or pushing changes in Showpass repositories,
load `$failure-pattern-memory` and search the shared index by repo, domain, and
general mechanism, then check matching incident examples. Recheck the general
prevention rules against the final diff before pushing. During reviews, report
a pattern only when the current code shows the same failure path. When Liam
identifies an unacceptable failure, or the work exposes a material regression
or incident, record the general mechanism and prevention rule, then add the
specific case as evidence before the task ends.

## Trigger routing

- Read-only questions use [Investigation](playbooks/investigation.md).
- A reported defect uses [Bug fix](playbooks/bug-fix.md).
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
  without a separate push or PR-create permission request. Other remote
  operations keep their own gates.
  Read-only diagnosis playbooks end with their report and an explicit status
  instead of entering a change handoff.

## Showpass PR lifecycle

For backend or frontend implementation, PR follow-up, or shipping work, read
[Showpass PR lifecycle](playbooks/showpass-pr-lifecycle.md). Use it from the
current stage through the authorized endpoint, including when DOT invokes
Poteto. Keep the remaining stages visible when the task covers only part of
this workflow. A read-only question or prompt-only request does not execute it.
Ponytail full is mandatory for coding and review in both repositories.

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

## Lead orchestration and model policy

The parent task is the lead. It frames the goal and scope, breaks work
into units, selects roles and models, dispatches and tracks workers, integrates
their changes and evidence, resolves conflicts, and owns the final decision
and answer.

Delegate independent execution and repeated tool-heavy work by default when
the current Codex runtime exposes a worker control. Let workers handle
bounded repository searches, routine status reads, execution, tests, CI
polling, and log collection when those tasks can run independently. Keep the
parent on direction, integration, decisions, exceptions, and final synthesis.
Do not make the parent repeat routine tool calls while a scoped worker can do
them.

For a supplied feature list of one to five independent items, keep one lead
task and assign one scoped code worker to each item. Use fewer workers when
items depend on each other or share writable files. Keep trivial serial work
in the parent. Dispatch ready items up to the runtime's reported worker limit,
then queue the remaining items in waves as capacity opens. Keep one parent
task across those waves; do not serialize independent work in the parent just
because the worker pool is full. Give writers separate worktrees or branches,
or disjoint paths when the runtime supports them. The parent integrates the
results, checks the exact branch and head for each change, and verifies the
overall outcome.

For PR and CI babysitting, use a bounded evidence worker for provider status
polling and log collection when the read surface and Codex worker controls are
available. It reports compact status changes and evidence paths. The parent
owns triage, code decisions, merge-ready status, and the user report.

Use these model lanes:

- The orchestrator runs as `gpt-6-sol` at `low`, Fast off.
- All workers, including evidence, CI, implementation, and review, run as
  `gpt-6-luna` at `max`, Fast on. CLI launches use `--yolo` within host policy;
  this does not expand authorization.
- Use up to 12 workers only when useful and within runtime capacity.
- Never select an Astra model.

Before dispatch, check the active model catalog and the actual Codex agent
controls for the requested model ID, reasoning effort, and supported options.
Catalog presence alone does not prove account access. Use an exact available
ID in the matching lane, and record requested and effective model and effort.
If a lane is unavailable, say so. Do not silently substitute a model, effort, or service tier. Use a supported
launch route for the requested settings or report the unavailable control. Never silently substitute a model. The
`collaboration.spawn_agent` API exposes model and effort selection, but has no
Fast service-tier field, so launches through it cannot be claimed as Fast.
Another active dispatch surface can use Fast only if it exposes the option and
the selection can be verified.

A skill cannot change or reliably inspect an already-running parent chat. Use
the `poteto-agent` role configured for Sol at low when that role is available.
An ordinary parent session must already be configured within that ceiling;
report when this cannot be confirmed and do not claim the skill enforced it.

Codex-native worker creation is a reversible in-session capability, not an
external operation. Check that the runtime actually exposes it before use.
Herdr workers, watcher, merge, deployment, messaging, and deletion actions
keep their existing authorization and evidence gates. Push and PR creation
follow the change-producing request rule above. A Codex worker does not gain
permission beyond the parent task's scope.

Each worker gets a standalone brief with goal, scope, context, acceptance,
verify command, timebox, forbidden actions, and report shape. Give every
writer its own paths. Read changed files and receipts yourself, verify exact
heads, and treat a worker summary as a pointer rather than evidence. If worker
controls or an allowed model lane are unavailable, use a bounded local fallback
when it fits; otherwise report BLOCKED.

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
