# Codex capability and side-effect contract

The playbooks describe decisions and evidence. They do not grant access to a
forge, a browser, a deployment target, another worker, or a destructive
filesystem operation.

## Result states

Use one of these states for every bounded lane:

- PASS: the named predicate passed on the matching local artifact.
- PASS+NOTES: the predicate passed and the report carries a non-blocking gap.
- FAIL: the predicate ran and failed.
- BLOCKED: a required capability or fixture is unavailable. Name it.
- NOT RUN: the operator did not authorize the lane or its precondition was
  not met.
- INCONCLUSIVE: the artifact or surface cannot support the claim.

BLOCKED, NOT RUN, and INCONCLUSIVE never become a pass by omission.

## Capability selection

Prefer the surface the task names. A local terminal, repository checkout,
temporary directory, supplied artifact, and the current Codex session are
available when the environment exposes them. Check Codex-native worker
controls, the active model catalog, browser or image capture, live-process
instrumentation, forge access, and recurring wakeups before a playbook uses
them. A model catalog entry alone does not prove account access.

Codex-native worker creation is a reversible in-session capability. It is
separate from an external Herdr worker or provider service. Use the native
control when available and follow Poteto's model lanes. Do not claim that a
Herdr, forge, or watcher adapter exists because Codex worker controls do.

Record the capability check in the decision trail. If it is missing, finish
the read-only preparation, stop at the boundary, and give the smallest local
fallback. A fallback may inspect a supplied artifact or run a fixture. It may
not invent a screenshot, model identity, CI verdict, source mapping, merge,
deployment, or worker result.

## Showpass lifecycle scope

For an explicitly requested Showpass PR lifecycle, use
[Showpass PR lifecycle](../playbooks/showpass-pr-lifecycle.md) to determine
scoped PR edits, reviewer messages, QA evidence publication, and Jira updates.
That request supplies authority for those lifecycle actions, overriding their
NOT RUN defaults below. Loading Poteto alone does not. Merge, deployment
mutations, and Production writes retain separate explicit authorization.

## Side-effect gate

Tests of external operations use a fixture adapter by default. The adapter
writes an append-only local log and accepts an operation only when the fixture
includes the authorization field for that operation. The log is evidence of
an attempt, not proof that a remote operation succeeded. Live scoped push and
review PR creation follow the change-producing request rule below.

UNSUPPORTED-SEAM: no implicit Herdr, forge, watcher, merge, deployment,
message, or deletion adapter is supplied by these playbooks. Codex-native
worker availability is checked separately. Worktree, simulator, cache,
runtime, and repository deletion remain separately gated.

Push and review PR creation are authorized by a change-producing request once
local checks pass and the exact remote, branch, base, head, and body are known.
The following operations have their own gates:

| Operation | Safe default | Required evidence before an authorized action |
| --- | --- | --- |
| Forge read | local fixture or an explicitly supplied read surface | repository and object identity |
| Push scoped commits and create a review PR | proceed after local checks | exact remote, branch, base, head, and body; change-producing request |
| PR edit, ready, or comment | NOT RUN | exact target, payload, and user authorization |
| Merge or auto-merge | fixture log only | independent verdict at the exact head and explicit landing authorization |
| Deployment or external message | NOT RUN | target, payload, rollback path, and explicit authorization |
| Watcher or recurring monitor | bounded local poll or BLOCKED | stop predicate and bounded interval |
| Worktree deletion | dry-run inventory | exact worktree path, active-use check, and explicit worktree deletion authorization |
| Simulator deletion | dry-run inventory | exact simulator path, active-use check, and explicit simulator deletion authorization |
| Cache deletion | dry-run inventory | exact cache path, active-use check, and explicit cache deletion authorization |
| Runtime deletion | dry-run inventory | exact runtime path, active-use check, and explicit runtime deletion authorization |
| Repository deletion | dry-run inventory | exact repository path, active-use check, and explicit repository deletion authorization |

Descriptions of commands below are plans, not permission to run them. A test
must inject a fake adapter and assert that the log stays empty when the gate
is absent.

## Evidence shape

Each lane reports:

status: PASS | PASS+NOTES | FAIL | BLOCKED | NOT RUN | INCONCLUSIVE
predicate: one sentence
surface: local fixture, supplied artifact, or named available capability
evidence: file or command output path
side_effects: empty or exact fixture-log entries
next_action: one sentence

Keep the state in a local file or fixture record when a loop spans turns.
Never treat a prose claim in a reply as durable state.

For a retained change handoff, keep the external lane state canonical. When a
forge operation is unavailable or outside the authorized scope, report `status: NOT RUN` and carry
`local_handoff_ready: true` only when the local diff, exact head SHA, checks,
and proposed body are recorded. Do not combine those fields into a composite
status such as `READY LOCAL; PR NOT RUN`.
