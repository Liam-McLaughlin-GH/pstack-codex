# Verification feature-map schema

This is the shared artifact contract for a project-local verification skill.
Issue #4 uses it to identify stale commands, selectors, fixtures, and expected
results without treating the map as proof that the application still works.

## Project-local layout

```text
.agents/skills/verify-<app>/
|-- SKILL.md
`-- features/
    |-- README.md
    `-- <feature>.md
```

`SKILL.md` contains exact launch, doctor, drive, evidence, cleanup, and helper
instructions. `features/README.md` is the index and baseline. Each feature
file is a user-facing recipe, not an implementation design.

## Feature file contract

Each feature file contains one H1 title, one short user-facing summary, and
exactly four H2 headings in this order:

1. `Sub-features`
2. `How to get to it (user POV)`
3. `Driving it with <harness>`
4. `Gotchas`

The driving section starts with `Preconditions:`. Every action record contains
these labels:

| Field | Required meaning |
| --- | --- |
| `Actors` | The user, operator, or system actor performing the action. |
| `Fixtures` | Seed data, credentials, files, process state, or other starting state. |
| `Action` | The user action and the exact command or harness operation. |
| `Expected result` | The observable result that should follow the action. |
| `Evidence` | The artifact path and what it proves, including exit code or a read-only side effect check when relevant. |

Feature-level `Actors` and `Fixtures` may be inherited by each action, but an
action-specific value wins when it differs. Do not omit a field because the
value is obvious. Use `NOT RUN` or `UNSUPPORTED` with a reason when a path
cannot be exercised.

## Index contract

The feature index records:

- baseline preconditions and isolated state;
- harness conventions and stable selector or command rules;
- evidence and skip-reporting rules; and
- one relative link for every feature file.

The index and feature files must not claim that a generated checklist proves
product behavior. A behavior is verified only after the recorded action runs
against the real user-facing surface and its evidence is retained.

## Maintenance contract

Issue #4 should compare the map with the current repository and flag:

- commands or routes that no longer exist;
- selectors, prompt strings, or accessible names that drift;
- fixtures that no longer establish the stated precondition;
- expected results that no longer match observable behavior; and
- evidence paths or side-effect checks that no longer capture the claim.

It should preserve valid feature coverage and change only the stale records it
can support with current source or execution evidence.
