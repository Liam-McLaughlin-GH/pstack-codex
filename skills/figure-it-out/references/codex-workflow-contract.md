# Codex workflow contract

This reference defines the artifact that `figure_it_out.py` validates and the
state transitions that it permits. Keep the contract finite and local. The
helper does not execute commands, delegate work, publish branches, or contact
external systems.

## Plan shape

The input to `init` is one JSON object with these fields.

```json
{
  "desired_outcome": "A measurable result for the task",
  "scope": {
    "included": ["src/feature", "tests/feature"],
    "excluded": ["deployment", "unrelated refactors"],
    "allowed_actions": ["read", "write", "run-tests"],
    "allowed_paths": ["src/", "tests/"],
    "out_of_scope_actions": ["deploy", "push", "send-message"]
  },
  "constraints": ["Keep the standard library only"],
  "terminal_predicate": {
    "description": "All phases pass and the named checks have evidence",
    "checks": ["focused-tests", "artifact-inspection"]
  },
  "limits": {
    "max_attempts_per_phase": 2,
    "max_checkpoints": 8,
    "max_replans": 3,
    "max_decisions": 128
  },
  "phases": [
    {
      "id": "frame",
      "name": "Frame the task",
      "hypothesis": "A bounded contract will expose the first risky unknown",
      "output": "A scoped contract and baseline",
      "verification": {
        "id": "frame-check",
        "method": "Read the contract and compare it with the baseline"
      }
    }
  ]
}
```

`desired_outcome`, `scope`, `constraints`, `terminal_predicate`, `limits`, and
`phases` are required. Each phase also requires a hypothesis, an output
contract, and a distinct verification check. The helper requires a positive
verification result and at least one evidence item before marking a phase
passed.

`allowed_paths` are relative prefixes. An empty list permits logical actions
that have no path target. When paths are listed, a targeted action must name a
path below one of those prefixes. Absolute paths, parent traversal, URLs,
credential targets, and undeclared actions are refused.

## State and transitions

The state file is JSON under the chosen workspace root. Writes use a temporary
file in the same directory followed by an atomic replacement. A fresh process
can load the file with `Workflow.load` or the helper CLI.

```text
planned -> running -> completed
planned -> running -> failed -> planned (only through replan)
running -> interrupted -> running (only through resume)
planned/running -> blocked
```

The helper never advances automatically after a phase passes. It selects the
next pending phase and records `next_action: start phase <id>`. A failed phase
cannot be started again. `replan` must record the failure evidence, why the
approach changed, and the next check. If a limit is exhausted, the helper
records a blocked state with evidence and a concrete next action.

An interruption requires at least one checkpoint for the current phase. Resume
preserves the checkpoint, output, decisions, and failure history. It resumes
only the interrupted phase.

## Decision trail

Each decision stores `phase`, `decision`, `why`, `evidence`, and one of
`open`, `verified`, `failed`, `reverted`, or `blocked`. `audit` exports the
current records with this header:

```text
ts\tphase\tdecision\twhy\tevidence\tresult
```

Evidence is concrete. Use a test name and result, a file path and line, a
command output artifact, or a blocker reference. Do not replace evidence with
"checked" or an agent report.

## Safe boundaries

`guard-action` is a refusal boundary, not an executor. A refused operation is
recorded with `status: refused`, a reason, and a safe next action. The caller
must stop that operation and either choose an allowed local action or replan
with the owner. The helper does not treat a refusal as a completed phase.

The plan and state contain task data only. They do not accept runtime model
settings, private session locations, or hidden delegation configuration.
