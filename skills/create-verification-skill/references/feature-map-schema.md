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

## Generated flow contract

The app-level specification supplies `setup`, `environment`, `seed_auth`,
`isolation`, `ownership`, `evidence_scope`, and `failure_signals`, plus typed
`launch`, `doctor`, `drive`, and `cleanup` commands. A command is an object
with an `executable`, an `argv` array, and an optional relative `cwd`. It is
not a shell string. Shell launchers and interpreter code-string forms are not
supported. The generator renders `cwd` as a first-class `Working directory`
instruction next to the quoted command. Cleanup targets are normalized against
that cwd and must be the disposable root or a declared disposable path. Cleanup
cannot target the project root, the evidence root, or a path below it. The app
also declares relative, non-overlapping `disposable_root` and `evidence_root`
paths and lists the disposable paths below the disposable root.

The generated Launch, Doctor, Drive, Evidence, and Cleanup sections repeat the
relevant run context. Cleanup may remove only run-owned disposable state. It
must leave evidence below the evidence root in place.

## Feature file contract

Each feature file contains one H1 title, one short user-facing summary, and
exactly four H2 headings in this order:

1. `Sub-features`
2. `How to get to it (user POV)`
3. `Driving it with <harness>`
4. `Gotchas`

The driving section starts with `Preconditions:`. Every feature also records
the run context explicitly. Do not rely on a reader to infer these values from
the app-level baseline.

| Field | Required meaning |
| --- | --- |
| `setup` | How this feature's starting state is established. |
| `environment` | Runtime, ports, build, endpoint, and other required environment facts. |
| `seed_auth` | Seed data, credentials, authentication, and permission state. |
| `isolation` | Disposable instance, data directory, profile, port, and whether the run owns them. |
| `ownership` | Which process or actor started each resource and what may be stopped. |
| `evidence_scope` | What the evidence can prove and what remains outside its scope. |
| `failure_signals` | Observable signals that make the feature or action fail. |

Every action record contains these labels:

| Field | Required meaning |
| --- | --- |
| `Actors` | The user, operator, or system actor performing the action. |
| `Fixtures` | Seed data, credentials, files, process state, or other starting state. |
| `Action` | The user action and the exact executable plus argv command or harness operation. |
| `Expected result` | The observable result that should follow the action. |
| `Failure signals` | Observable output, state, or exit conditions that invalidate the action. |
| `Evidence` | An artifact path below the declared evidence root and what it proves, including stdout, stderr, exit code, visible result, or a read-only side-effect check when relevant. |

Feature-level `Actors` and `Fixtures` may be inherited by each action, but an
action-specific value wins when it differs. Do not omit a field because the
value is obvious. Use `NOT RUN` or `UNSUPPORTED` with a reason when a path
cannot be exercised.

## Index contract

The feature index records:

- baseline preconditions and isolated state;
- setup, environment, seed/auth, isolation, ownership, evidence scope, and
  failure signals;
- harness conventions and stable selector or command rules;
- evidence and skip-reporting rules; and
- one relative link for every feature file.

The index must start with one H1 and a summary paragraph. It must link every
feature exactly once. A feature file must start with one H1 and a summary
paragraph, use the four H2 sections in order, start its driving section with
`Preconditions:`, and include every action contract. Missing links, duplicate
links, malformed frontmatter, and incomplete action records are validation
errors.

Commands in the generated flow use an executable plus argv. They are rendered
as quoted text for the reader, never as free-form shell instructions. The
disposable root and evidence root are relative, non-overlapping paths. No
generated root or nested file may be a symlink, and the project-local location
must be `.agents/skills/verify-<app>/`.

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
