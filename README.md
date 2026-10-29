# pstack for Codex

This repository is a Codex port of the `pstack` Cursor plugin. It contains skills for planning, design, implementation, review, verification, and repository routing. It does not provide Cursor's model router, transcript store, UI control layer, or automation runtime.

The repository is the source tree. Codex discovers the skills only after you install or link them into a Codex skill directory, or when you run from a project that has copied selected skills into `.agents/skills`.

## Source and license

The source snapshot came from the `pstack` directory in [cursor/plugins](https://github.com/cursor/plugins/tree/main/pstack) at commit [`53e579f1481697931fc44f5445171397cfa2b24b`](https://github.com/cursor/plugins/commit/53e579f1481697931fc44f5445171397cfa2b24b).

The upstream project is MIT licensed. The original copyright and license text are in [LICENSE](LICENSE).

## Quick start

Run these commands from the repository root:

```sh
cd /path/to/pstack-codex
python3 scripts/verify_port.py
```

The verifier checks every active skill for its `SKILL.md`, required frontmatter, local Markdown references, and the runtime seams this port excludes.

Link all skills into the user Codex directory after checking for name collisions:

```sh
REPO="${PSTACK_CODEX_REPO:-$(pwd)}"
CODEX_HOME="${CODEX_HOME:-$HOME/.codex}"
CODEX_SKILLS_DIR="$CODEX_HOME/skills"
mkdir -p "$CODEX_SKILLS_DIR"

for skill_dir in "$REPO"/skills/*; do
  skill_name=$(basename "$skill_dir")
  if [ -e "$CODEX_SKILLS_DIR/$skill_name" ]; then
    printf 'Skipping existing skill %s\n' "$skill_name"
    continue
  fi
  ln -s "$skill_dir" "$CODEX_SKILLS_DIR/$skill_name"
done
```

Then start Codex from the project and invoke a skill in your request:

```text
Use $poteto-mode to plan this change. Use $how to trace the current path and $why to explain the design choice before editing.
```

The project configuration in `.codex/config.toml` registers `poteto-agent` and `Comment Sicko`. The matching definitions live under `.codex/agents/`. Run Codex from the repository so the project configuration is in scope.

## Companion skills

The Showpass backend and frontend playbooks also refer to `$ponytail`, which
is maintained outside this repository in
[`CodexMaxxing/skills/ponytail`](https://github.com/Liam-McLaughlin-GH/CodexMaxxing/tree/main/skills/ponytail).
Download that repository separately and link the skill into the Codex skills
directory if you want those playbooks to invoke it:

```sh
CODEX_HOME="${CODEX_HOME:-$HOME/.codex}"
mkdir -p "$CODEX_HOME/skills"
ln -s /path/to/CodexMaxxing/skills/ponytail "$CODEX_HOME/skills/ponytail"
```

Pstack does not fetch companion repositories or install their skills
automatically. The playbook remains readable without `$ponytail`, but its
Ponytail-specific tracing workflow requires that skill to be installed.

## Active port

The merged port has 49 active skills:

- 23 principle skills for design, state, concurrency, testing, and delivery decisions.
- 26 workflow and repository skills for planning, implementation, review, verification, and routing.
- The `poteto-mode` playbooks under `skills/poteto-mode/playbooks`.
- Two project agent definitions under `.codex/agents/`.

The 23 upstream playbooks keep their source workflow contracts while using the local capability and side-effect rules in `skills/poteto-mode/references/capability-boundaries.md`. Four Showpass-specific playbooks cover support hotfixes, backend fixes, frontend fixes, and paired backend/frontend work. Forge, watcher, deployment, message, delegation, and deletion steps stay fixture-only or require explicit authorization.

## Active skill guide

All 49 directories under `skills/` are active. Invoke a skill by name in a Codex request, for example `$how` or `$principle-fix-root-causes`. The skill tells Codex when to use it, what evidence to gather, and where to stop.

The workflow skills answer a direct need. The principle skills are smaller rules that shape a design, implementation, review, or verification pass.

### Workflow skills

| Skill | Why it exists | How to use it |
| --- | --- | --- |
| `architect` | A non-trivial change can start with the wrong shape. A small design sketch makes the mistake visible before code spreads it. | Use `$architect` before a new boundary, state model, public interface, or cross-module change. It runs `$how`, names inputs and failure states, compares an alternative, then gives you a design to review. |
| `arena` | One plausible design can hide a bad assumption. Independent candidates make the tradeoff visible. | Use `$arena` when the first approach may lock in the wrong structure. Set the criteria, produce separate candidates, cross-judge them, choose one, and verify the result. |
| `automate-me` | People often ask for a skill, script, preference, or no artifact without naming which one. Choosing the artifact first prevents a general request from becoming the wrong permanent file. | Use `$automate-me` when you want to automate a repeated working style, create or update a project skill, save a durable preference, or decide whether a request needs an artifact. It routes the request and keeps private history out of the process. |
| `blast-radius` | A diff can pass its own tests and still break a caller, sibling workflow, or external boundary. A list of references does not prove safety. | Use `$blast-radius` before shipping a change you do not trust. Trace the affected paths and run the one or two checks that prove the main safety claim. |
| `bro` | Jargon makes a technical answer harder to act on. A plain restatement exposes whether the request was understood. | Use `$bro` when the last answer was too dense. It restates the last message in ordinary language and stops. |
| `create-verification-skill` | A project without a repeatable product check makes every future review start from scratch. A generated checklist also does not prove that the product works until someone runs it. | Use `$create-verification-skill` when a UI, CLI, API, desktop, mobile, or service path lacks reliable proof. It reads the repository, creates `.agents/skills/verify-<app>/`, and records how to start, drive, observe, and save evidence. |
| `figure-it-out` | Large ambiguous work fails when its end state and resume point are unclear. A finite state artifact keeps the run bounded after interruption. | Use `$figure-it-out` when no narrower workflow fits. Define the result and proof, split the work into phases, persist checkpoints with the helper, and record completed, blocked, or out-of-scope states. |
| `how` | A filename list does not explain a system. A useful walkthrough must show the entry point, data shapes, normal path, error path, and side effects. | Use `$how` for a code path or subsystem walkthrough. It traces callers and boundaries, names the state at each step, cites the files and symbols, and runs a focused check when the explanation depends on behavior. |
| `interrogate` | Agreement with the first design is weak evidence when the cost of a missed edge case is high. Adversarial review makes the assumptions explicit. | Use `$interrogate` before shipping a contested plan or risky diff. Give it the changed files, nearby callers, tests, and rubric. It returns a synthesized verdict and does not edit code automatically. |
| `maintain-verification-skill` | Verification skills drift when selectors, commands, fixtures, or expected results change. A structural check alone cannot prove the feature still works. | Use `$maintain-verification-skill` for an existing `verify-<app>` skill. Read the current source and live behavior, classify each feature as clean, changed, or blocked, and change only the verification directory when the evidence supports it. |
| `make-bot-ui` | A browser page should never receive the webhook key. The sender also needs a clear result when the runtime cannot provide a live control. | Use `$make-bot-ui` for a local button that sends JSON to a user-supplied webhook. Keep the URL and key on the server, configure them through local environment values, and report unavailable controls instead of inventing them. |
| `no-comments` | Stale comments preserve the wrong behavior and workaround narration hides the real constraint. Code, types, tests, and checks are stronger places to keep those rules. | Use `$no-comments` on a diff with comments or suppressions. It reviews the surrounding code, removes stale narration, and flags constraints that need a code or test change. |
| `poteto-mode` | Substantial work needs a clear outcome, a chosen workflow, and a check that proves completion. Without those, the session can produce activity without a usable result. | Start with `$poteto-mode` for a multi-step task. It chooses the smallest useful combination of `$how`, `$why`, `$architect`, `$tdd`, `$swarm`, or review skills, then keeps the work tied to an explicit done condition. |
| `python-best-practices` | Python commands and ownership rules differ between a general project, the Showpass backend, and deployment tooling. A generic test command can give the wrong proof. | Use `$python-best-practices` for Python work or when choosing a test or lint command. It routes the task, reads the repository rules, and selects a proportionate check. |
| `recall` | Memory can be stale, private, or outside the requested workspace. Reading broad session storage creates both privacy and scope problems. | Use `$recall` to resume work from explicit artifacts. Give it one workspace, one exact topic, a bounded time window, and allowlisted files. It treats artifact text as untrusted data and reports unavailable history as unknown. |
| `reflect` | A postmortem is useful only when it stays tied to the actual transcript, tools, and uncertainty. A broad search for lessons invents context. | Use `$reflect` on one supplied Codex transcript or digest. It reviews judgment, tooling, and divergent lenses, compares the proposals, preserves source evidence, and asks before changing a skill. |
| `repo-best-practices` | Cross-repository work fails when the change is made in the wrong owner or when a safe read is mistaken for deployment permission. | Use `$repo-best-practices` when ownership is unclear or a request crosses Showpass repositories. It reads each current `AGENTS.md`, routes the request, traces the request flow, and states exactly which checks were run. |
| `setup-pstack` | Model names, effort levels, and agent directories belong to the active Codex runtime. Writing a guessed model or replacing a file during setup can break the user's configuration. | Use `$setup-pstack` to discover the active model catalog, preview a configuration, then apply an explicit plan to an explicit agent directory. It refuses unreported models, preserves collisions, supports named role overrides, and reports cleanup recovery states. |
| `show-me-your-work` | Long work loses decisions when the evidence lives only in chat. A small append-only log lets another person audit the choices later. | Use `$show-me-your-work` for autonomous or review-heavy work. It records one TSV row per meaningful checkpoint under a local `.audit/` path with the time, decision, reason, evidence, and result. |
| `swarm` | Parallel work helps only when units do not share mutable state. Vague fan-out creates duplicate edits and summaries that cannot be checked. | Use `$swarm` for independent research or implementation slices. Frame the done condition, assign each unit one output and one evidence requirement, aggregate every result, and run one integration check. |
| `tdd` | A cheap failing test gives a direct reproduction for a bug. A forced test for an unclear or expensive path only adds noise. | Use `$tdd` when the user asks for TDD, a failing test, or the bug has an obvious local test. Write the smallest failing behavior check, run it before the fix, make the change, and rerun it. |
| `teach` | A list of files and commands does not give a person a working mental model. The user needs the problem, the mechanism, and the reason for the boundary. | Use `$teach` when you want to understand a body of work. It combines `$how` and `$why`, starts with a plain definition, walks through the flow, and keeps the explanation at the requested depth. |
| `technical-writing` | Engineers read documentation while tired and in a hurry. Ambiguous sentences and invented jargon turn a correct report into a support burden. | Use `$technical-writing` for reports, READMEs, RFCs, PR descriptions, or commit messages. It chooses the document shape, uses direct sentences and repository names, and removes words that do no work. |
| `typescript-best-practices` | TypeScript can accept values that the domain cannot. Weak types move validation and failure into unrelated callers. | Use `$typescript-best-practices` for `.ts` or `.tsx` work. Apply type-system discipline first, parse external data at the boundary, use discriminated or branded types where needed, and avoid casts that hide invalid state. |
| `unslop` | Vague language hides the mechanism and makes technical writing sound generic. The reader needs facts, commands, and limits. | Use `$unslop` as the final editing pass. It removes filler, inflated vocabulary, forced lists, passive wording, em dashes, and claims that lack a concrete source. |
| `why` | Design reasons are easy to invent after the fact. The useful answer separates source evidence from inference and states what remains unknown. | Use `$why` when a design, tradeoff, or history is unclear. Anchor the question in code and artifacts, map the available evidence, compare credible alternatives, and label conclusions as observed, supported, inferred, or unknown. |

### Principle skills

| Skill | Why it exists | How to use it |
| --- | --- | --- |
| `principle-attack-the-premise` | Repeating fixes that fail the same gate usually means the shared assumption is wrong. Another patch built on that assumption will repeat the failure. | Use it after two or more related fixes fail. Count which actors hold the imbalance, identify what assigns that role, and question that assignment before writing another fix. |
| `principle-boundary-discipline` | Untrusted data enters through CLIs, config, files, networks, and framework adapters. Scattering checks through business logic makes the system harder to reason about. | Use it when wiring validation or adapters. Parse and reject at the boundary, then keep internal functions operating on trusted domain values. |
| `principle-build-the-lever` | Hand work is hard to rerun and hard to review. The repeatable tool or check is the durable result. | Use it for a non-trivial edit, migration, analysis, or review. Build the smallest script, generator, codemod, or skill that performs or proves the work. |
| `principle-encode-lessons-in-structure` | A reminder written twice is a rule that belongs in the system. Prose alone will be missed on the next run. | Use it when the same correction recurs. Turn the rule into a lint, metadata field, runtime check, or script and test that guard. |
| `principle-exhaust-the-design-space` | A novel interaction or architecture has no local precedent to expose a bad choice. One prototype is not a comparison. | Use it for a new UI interaction or architectural decision. Build two or three small competing prototypes and compare them against the same criteria. |
| `principle-experience-first` | A technically convenient feature can still make the product unpleasant. A smaller polished path is easier for users to trust. | Use it when product or scope tradeoffs appear. Judge the user result first, then cut rough features until the remaining path is clear and usable. |
| `principle-fix-root-causes` | A nil check or retry can silence a symptom while leaving the cause in place. That shifts the failure to a later and harder-to-debug path. | Use it while debugging. Reproduce the failure, trace the data and control flow, ask why until the cause is reached, and fix that location. |
| `principle-foundational-thinking` | A wrong data structure forces special cases into every caller. Choosing the core shape first removes those branches. | Use it before writing stateful logic or a scaffold. Model the data, ownership, and concurrency first, then build the flow around that model. |
| `principle-guard-the-context-window` | Large output and repeated raw reads consume the space needed for decisions. More context can make the final reasoning worse. | Use it when a task produces large files, logs, or many work units. Route bulk inspection to bounded workers, keep summaries in the main thread, and read only the paths needed for the next decision. |
| `principle-laziness-protocol` | Extra abstractions and signal plumbing create code that has to be maintained before it has proved useful. Deletion is often the smaller fix. | Use it when a diff grows or a new layer feels tempting. Remove dead code and solve the observed problem with the smallest change that passes the behavior check. |
| `principle-make-operations-idempotent` | Commands run again after crashes, retries, and partial writes. A second run should converge instead of duplicating or damaging state. | Use it for commands, lifecycle steps, and processing loops. Define the end state, record ownership, and make a retry safe from every partial prior state. |
| `principle-migrate-callers-then-delete-legacy-apis` | Keeping an old API alive after its callers move creates two paths and delays cleanup. The compatibility layer becomes permanent by accident. | Use it when introducing an internal API. Move every caller in the same change wave, verify them, then remove the old API. |
| `principle-minimize-reader-load` | Every wrapper and hidden mutable field makes a reader reconstruct more of the system. Code that is technically small can still be hard to follow. | Use it during review or refactoring. Count the layers between the question and the answer, collapse one-caller wrappers, and shrink mutable scope. |
| `principle-model-the-domain` | Repeated conditionals usually mean the domain state has no named shape. The same assumption then drifts across files. | Use it when state branches or shape checks repeat. Name the domain variants and encode valid transitions in structures instead of copying conditions. |
| `principle-never-block-on-the-human` | Asking for permission on reversible work leaves useful progress idle. Irreversible actions need a confirmation gate, routine local work does not. | Use it when deciding whether to ask. Continue with reversible edits and checks, show the result, and ask only before an irreversible or externally visible action. |
| `principle-outcome-oriented-execution` | Rewrites often preserve temporary compatibility states that are no longer part of the target. The project then carries both designs. | Use it for planned migrations and rewrites. Define the target architecture, use explicit phase boundaries, and remove transition-only code when the target is in place. |
| `principle-prove-it-works` | Compilation and a green helper can pass while the user behavior is broken. The actual artifact is the only reliable final check. | Use it before declaring work complete. Run the feature or its closest real fixture, read the resulting value, inspect the final diff, and separate missing evidence from passing evidence. |
| `principle-redesign-from-first-principles` | Adding a requirement to an old design often creates a seam full of exceptions. Rebuilding the shape around the new requirement removes those exceptions. | Use it when a new requirement cuts across existing boundaries. Re-state the design as if the requirement had existed from the start, then move callers to that shape. |
| `principle-separate-before-serializing-shared-state` | A lock can hide a design that makes unrelated work share a file, branch, key, or state object. Serializing that sharing increases contention and failure scope. | Use it when concurrent actors write the same state. Give each actor separate state first, then serialize only the writer that is an actual invariant. |
| `principle-sequence-verifiable-units` | Large edits and stacked PRs hide the first failure. Small checked units make the next step depend on proof. | Use it for sweeps, migrations, repeated edits, and stacked delivery. Break the work into units, verify each one, and order the sequence so a reviewer can follow it. |
| `principle-subtract-before-you-add` | Dead code, duplicate validators, and stub references make new work harder to place. Adding on top of them preserves the confusion. | Use it before an addition or rewrite. Delete what no longer serves the target, then build the new behavior on the smaller base. |
| `principle-test-behavior-not-implementation` | A test that only mirrors helpers can stay green while the user-visible result breaks. Tests should call the code as its users do. | Use it when writing or reviewing a test. Drive the public path and assert a literal observed result. Rewrite tests that would pass with empty or undefined helper results. |
| `principle-type-system-discipline` | A compiler can accept values that the domain rejects. Strong domain types move that failure to the boundary and reduce runtime branching. | Use it for typed design in any language. Model variants explicitly, brand semantic primitives, parse `unknown` external data, derive types from schemas, and reject casts that hide a missing check. |

The same skill can serve two roles. For example, `$principle-prove-it-works` is a rule for the final check, while `$tdd` is a workflow for writing a cheap regression test. `$teach` combines `$how` and `$why` when the goal is understanding rather than a code change.
## Configure pstack agents

Use `setup-pstack` when you need to choose models or update the project agent files. It reads the active Codex model catalog. It does not guess model IDs or silently replace an unavailable model.

Discover the catalog:

```sh
python3 skills/setup-pstack/scripts/configure.py discover --json > /tmp/pstack-models.json
```

Replace `MODEL_ID` with an ID from that catalog, then preview the generated configuration:

```sh
python3 skills/setup-pstack/scripts/configure.py preview \
  --catalog /tmp/pstack-models.json \
  --agents-root .codex/agents \
  --budget medium \
  --default-model MODEL_ID \
  --json
```

Read the preview before applying it. Run the same command with `apply` in place of `preview` only after reviewing the plan. Use `--role ROLE=MODEL_ID` for a role-specific override. Use `--replace` only when the reviewed change should replace differing generated files.

Budget names set an effort ceiling. The helper chooses an effort reported by the selected model. It refuses unreported models and efforts.

For a skill installed under `CODEX_HOME`, use the installed script explicitly:

```sh
CODEX_HOME="${CODEX_HOME:-$HOME/.codex}"
SETUP_PSTACK_SKILL="$CODEX_HOME/skills/setup-pstack"
python3 "$SETUP_PSTACK_SKILL/scripts/configure.py" discover --json
```

## Install one skill into a project

Use the installer when a project should have a copy under `.agents/skills`. The default action previews the plan:

```sh
python3 skills/setup-pstack/scripts/install.py \
  --source skills/how \
  --destination .agents/skills \
  --json
```

After reviewing the output, add `--apply`:

```sh
python3 skills/setup-pstack/scripts/install.py \
  --source skills/how \
  --destination .agents/skills \
  --apply \
  --json
```

The installer rejects unsafe sources, symlinks, and differing collisions. It stages a complete copy before publication. If cleanup cannot finish, it reports the residue and an exact retry command. Do not remove a reported residue with a broad command.

Run the isolated setup checks after changing installation or configuration:

```sh
python3 skills/setup-pstack/scripts/install.py --self-test
python3 skills/setup-pstack/scripts/configure.py self-test
```

Possible transaction states include `published`, `unchanged`, `published_cleanup_pending`, `root_replacement_cleanup_pending`, and `publication_recovery_pending`. Inspect the reported residue and follow the exact retry or recovery instructions. On macOS, identity-bound cleanup may preserve a residue when the deletion primitive returns status `-47`. The port keeps that state visible and does not use unsafe pathname deletion.

## Common workflows

Use these entry points directly in Codex:

```text
$poteto-mode
$how
$why
$architect
$tdd
$swarm
$arena
$interrogate
$show-me-your-work
$no-comments
$teach
```

A practical sequence for a substantial change is:

1. Start with `$poteto-mode` and state the result that must be true.
2. Run `$how` over the existing path.
3. Run `$why` when the current design or tradeoff is unclear.
4. Use `$architect` before a new boundary or state model.
5. Use `$tdd` when the bug has a cheap local behavior check.
6. Use `$blast-radius` and `$interrogate` before shipping a change with a wide or uncertain impact.
7. Apply `$principle-prove-it-works`, then run the actual feature or fixture and inspect the final diff.

The skill name is a routing instruction. It does not grant permission to edit, deploy, contact a service, send a message, or change production data.

## Verification and contribution checks

After changing a skill, run:

```sh
python3 scripts/verify_port.py
python3 -B -m unittest discover -s tests -p 'test_*.py' -v
git diff --check
```

Add the narrowest skill-specific test command when the change has one. Keep tests behavior-focused. Use fixture adapters for external systems. Record unavailable live checks as `NOT RUN` or `UNSUPPORTED` instead of treating static checks as live proof.

The completed planning and issue-tracking material was removed from `plans/completion/`. The active skills remain the source of truth for their commands and boundaries.

## Using workspace context

These skills are designed to work with the context supplied by the consuming
workspace. Before acting on a project:

- Read workspace-level agent instructions and the target repository's
  `AGENTS.md` or equivalent.
- Load the repo-specific and project context relevant to the task.
- Check which skills are installed in the current runtime and use their
  canonical source when available.

Refresh project state when starting work. Keep credentials, runtime databases,
logs, raw sessions, and machine-local state out of this repository.

## Runtime boundaries

The port supports Codex-local reasoning, planning, implementation guidance, review, verification, and controlled fixture work.

It does not provide:

- Cursor's model router or Cursor model names.
- Cursor transcript paths or a transcript store.
- Cursor UI controls or the `Task` and `AskQuestion` runtime.
- A live PR watcher, provider wrapper, deployment helper, merge executor, or deletion service.
- The `automations/benny/` pack.

G1 playbooks, G2 agent definitions, and G3 helper tooling are implemented within offline boundaries. G4 Benny and controls remains unsupported because the required live adapter, target configuration, credentials, and role router are absent.

The repository does not install itself into a real user runtime automatically. Review the preview and use an isolated destination before applying a skill installation.
