---
name: create-verification-skill
description: Create a project-local Codex skill that drives a real app through a user-facing surface and records repeatable proof. Use for /create-verification-skill or when a project has no reliable scripted path for proving UI, CLI, API, desktop, mobile, or service behavior.
---

# Create a verification skill

Create a project-local skill that tells a future agent how to start the real
app, drive one user path, observe its result, and retain evidence. A generated
checklist describes a route to proof; it does not prove the product works until
the route has been executed.

The generated skill belongs at `.agents/skills/verify-<app>/` so Codex can
discover it as a project-local skill. Do not install it into a user directory
from this workflow. A skill does not grant permission to send messages, change
production data, or cross the project's declared workspace boundary.

## 1. Interview the repository

Answer these from source, configuration, and the repository's own quickstart.
Ask the user only for facts the repository cannot show.

- **Surface:** identify what a user touches: web UI, CLI or TUI, desktop app,
  API, mobile app, or library. Choose the primary surface and record other
  supported surfaces as separate entry points or limitations.
- **Run:** find the documented local command, required environment, port,
  seed data, authentication, and readiness signal. If the checkout cannot
  start, stop and report the exact blocker before writing a generated recipe.
- **Drive:** reuse an existing Playwright, Cypress, expect, PTY, HTTP, or
  repository CLI harness. If no harness exists, choose the smallest available
  Codex-compatible path: browser tooling for a web or desktop surface, a
  disposable PTY for a CLI or TUI, or plain HTTP for a service.
- **Observe:** name the evidence available at each step: screenshots or
  accessibility snapshots, terminal output and exit codes, response bodies,
  logs, or a read-only second view of persisted state.
- **Isolate:** determine whether separate instances can use separate ports,
  data directories, profiles, or worktrees. If they cannot, say so and refuse
  to drive an instance that this run did not start.

Keep the interview's facts separate from assumptions. Do not invent selectors,
commands, seed data, or readiness messages.

## 2. Generate the project-local skill

Create `.agents/skills/verify-<app>/SKILL.md` with frontmatter containing the
matching `name` and a description that names the app, surface, and trigger.
The name is at most 64 characters. The description is valid YAML, has no angle
brackets, and is at most 1,024 characters.

Use structured command values throughout the JSON specification:

```json
{"executable": "python3", "argv": ["app.py", "doctor", "--state", "scratch/state.txt"]}
```

Do not accept a shell command string. The helper renders argv with shell
quoting for a reader, but does not execute it. Executables cannot contain shell
syntax. Command arguments cannot use absolute paths or `..` to escape the
project. An optional `cwd` is a relative project path rendered as a separate
`Working directory` instruction beside the literal command. Cleanup may not
use the evidence root or a path below it as its working directory. Keep
disposable paths below `disposable_root`, evidence paths below `evidence_root`,
and keep those roots separate.

The app specification also records `setup`, `environment`, `seed_auth`,
`isolation`, `ownership`, `evidence_scope`, and `failure_signals`. Every
feature records the same fields. Each action records its own failure signals
and an evidence object with a bounded artifact path:

```json
{
  "evidence": {
    "path": "artifacts/feature/pass.json",
    "proves": "the visible result and read-only side effect"
  }
}
```

The body must contain these sections, grounded in the interview:

- **Launch:** the exact startup command, readiness check, required environment,
  and teardown. For a short-lived CLI or TUI, describe the build or install
  step and the isolated PTY used for each drive.
- **Doctor:** one read-only check for process ownership, version or build,
  endpoint or prompt, required auth, and disposable state. Run it before
  driving when anything looks wrong.
- **Drive:** the real harness commands and stable handles. Prefer accessible
  names, roles, data attributes, routes, prompt strings, and command flags over
  coordinates or tab order.
- **Evidence:** the artifacts to capture, their paths, and the proof standard.
  Capture the action and resulting state, verify side effects with a second
  read-only view, and mark mocks or skipped paths explicitly.
- **Cleanup:** stop only instances created by the run, remove scratch state,
  restore seeded data, and leave evidence in place.
- **Helpers:** list every shipped helper and show its literal invocation.
- **Maintenance:** state that feature-map maintenance is the #4 follow-up and
  is unsupported until #4 is available.

Use the bundled [scaffold helper](scripts/scaffold.py) when all commands and
expected results are already known. It writes a new skill atomically from an
explicit JSON specification; it does not inspect the repository or claim that
the generated documents are product proof. Its output path is explicit:

```sh
python3 scripts/scaffold.py scaffold \
  --spec /absolute/path/verification-spec.json \
  --project-root /absolute/path/project
```

Run the helper's validator on an existing generated skill without rewriting it:

```sh
python3 scripts/scaffold.py validate \
  --skill-root /absolute/path/project/.agents/skills/verify-app
```

The validator re-parses every emitted Launch, Doctor, Drive, Cleanup, and
feature action command. It rejects free-form command text, unsafe executable or
argv paths, and cleanup commands that address the evidence root through argv or
`cwd`.

The helper refuses an existing target, including a broken symlink. It rejects
symlinks in the complete generated tree and in every parent component. It
reserves the final directory with an exclusive create before publication, so a
target that appears during generation is preserved. Use the repository's
normal skill-authoring workflow for a deliberate update instead of turning a
collision into an overwrite.

## 3. Seed the feature map

Create `features/README.md` and one file per user-facing feature. Start with
the top three to five features when the surface is broad, but include every
distinct entry point needed to prove the requested change. Read the
[feature-map schema](references/feature-map-schema.md) before writing the map.

The index names baseline state, harness conventions, evidence rules, and every
feature file. Each feature file has exactly these four H2 sections, in order:

1. `Sub-features`
2. `How to get to it (user POV)`
3. `Driving it with <harness>`
4. `Gotchas`

Within the driving section, every recorded action names the actor, fixture,
user action or exact argv command, expected result, failure signals, and
evidence. Keep the map in the user's vocabulary. Implementation details belong
in the repository, not in the feature map.

## 4. Execute before handing it over

Run the generated skill end to end once:

1. Launch the instance or prepare the isolated short-lived process.
2. Run Doctor and confirm that this run owns the instance and scratch state.
3. Drive one mapped feature through its real user path.
4. Capture the literal generated command, stdout, stderr, exit code, visible
   result, and relevant side effect.
5. Run the literal generated Cleanup command, then confirm that evidence still
   exists at its bounded path.

After every failed iteration, run Cleanup before trying again. A passing
document check is not a passing product check. Report `PASS`, `FAIL`,
`NOT RUN`, or `UNSUPPORTED` with the command and artifact path; do not convert
an unreachable entry point into coverage through a different path.

## 5. Bound unsupported behavior

Use the repo's available harnesses and the current Codex runtime. If browser,
PTY, mobile, external-service, or authentication support is unavailable, state
the boundary and preserve the closest local proof. Never replace a missing
capability with a pretend adapter. Dry-run or test-mode names are not proof of
isolation: observe files, network, process ownership, and refs when safety
depends on what the mode skips.

Feature-map maintenance is a #4 follow-up and is unsupported until #4 is
available. Do not claim that this creation flow detects stale commands,
selectors, fixtures, or expected results.

## References

- Read [feature-map-schema.md](references/feature-map-schema.md) when creating
  or maintaining a feature map.
- Read [upstream-provenance.md](references/upstream-provenance.md) for the
  pinned source and retained license notice.
