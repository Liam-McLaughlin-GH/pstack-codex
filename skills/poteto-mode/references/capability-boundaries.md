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
available when the environment exposes them. Named delegation, browser or
image capture, live-process instrumentation, forge access, and recurring
wakeups are capabilities that must be checked before a playbook uses them.

Record the capability check in the decision trail. If it is missing, finish
the read-only preparation, stop at the boundary, and give the smallest local
fallback. A fallback may inspect a supplied artifact or run a fixture. It may
not invent a screenshot, model identity, CI verdict, source mapping, merge,
deployment, or worker result.

## Side-effect gate

External operations use a fixture adapter by default. The adapter writes an
append-only local log and accepts an operation only when the fixture includes
the exact authorization field for that operation. The log is evidence of an
attempt, not proof that a remote operation succeeded.

UNSUPPORTED-SEAM: no implicit forge, watcher, merge, deployment, message,
agent-spawn, or deletion adapter is supplied by these playbooks. Worktree,
simulator, cache, runtime, and repository deletion remain separately gated.

The following operations always stop or stay fixture-only unless the user
explicitly authorizes the exact action in the current request:

| Operation | Safe default | Required evidence before an authorized action |
| --- | --- | --- |
| Forge read | local fixture or an explicitly supplied read surface | repository and object identity |
| PR create, edit, ready, push, or comment | NOT RUN | exact target, body, and user authorization |
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

For a retained change handoff, keep the external lane state canonical. When no
forge operation is authorized, report `status: NOT RUN` and carry
`local_handoff_ready: true` only when the local diff, exact head SHA, checks,
and proposed body are recorded. Do not combine those fields into a composite
status such as `READY LOCAL; PR NOT RUN`.
