# pstack for Codex

This repository is a Codex port of the `pstack` Cursor plugin. It keeps the parts that describe how an agent should reason and work. It does not pretend that Cursor's model router, transcript store, UI controls, or automation runtime exist in Codex.

The active port is intentionally focused. It gives Codex the portable principles and the core workflow skills first. Cursor-specific features stay listed below as open work.

## Source and license

The source snapshot came from the `pstack` directory in [cursor/plugins](https://github.com/cursor/plugins/tree/main/pstack) at commit [`53e579f1481697931fc44f5445171397cfa2b24b`](https://github.com/cursor/plugins/commit/53e579f1481697931fc44f5445171397cfa2b24b).

The upstream project is MIT licensed. The original copyright and license text are in [LICENSE](LICENSE).

## What is preserved

The following 30 upstream skills are active in this repo without changing their core guidance.

- All 23 `principle-*` skills.
- `blast-radius`.
- `bro`.
- `tdd`.
- `teach`.
- `technical-writing`.
- `typescript-best-practices`.
- `unslop`.

These skills carry over the parts of pstack that do not depend on Cursor APIs. They cover design principles, root-cause debugging, behavior-focused verification, plain explanations, TypeScript modeling, and clean writing.

The following 10 skills are present as Codex-native rewrites.

| Skill | Codex port |
| --- | --- |
| `poteto-mode` | Routes work through the portable principles and workflow checks. |
| `how` | Explains code structure and runtime flow without Cursor subagents. |
| `why` | Separates observed evidence from inference and unknowns. |
| `architect` | Requires a small design sketch before non-trivial implementation. |
| `arena` | Compares independent candidates using separate output paths. |
| `swarm` | Splits broad work into explicit, non-overlapping work units. |
| `interrogate` | Runs an adversarial review of a design or diff. |
| `show-me-your-work` | Keeps an optional append-only decision trail. |
| `no-comments` | Provides a Codex-compatible review pass for stale or misleading comments. |
| `setup-pstack` | Installs the skill and agent bundle with guarded, reversible configuration. |

The port preserves the names of these workflows where possible. In Codex, invoke them as skills such as `$poteto-mode`, `$how`, `$architect`, or `$tdd`.

## What is missing

The following 7 upstream skills are not active in the port.

- `automate-me`. It depends on Cursor's built-in skill-authoring flow and transcript paths.
- `create-verification-skill`. Its feature-map workflow assumes Cursor control skills and `.cursor/skills`.
- `figure-it-out`. Its playbook routing and subagent instructions need a Codex-specific rewrite.
- `maintain-verification-skill`. It assumes the same Cursor verification-skill layout.
- `make-bot-ui`. It depends on Cursor UI and control tooling.
- `recall`. It reads Cursor agent transcripts.
- `reflect`. Its multi-model transcript review needs a Codex transcript and delegation adapter.

The upstream `poteto-mode` playbooks are not copied into the active port. The missing playbook files are:

`authoring-a-skill.md`, `autonomous-run.md`, `autopilot-full.md`, `autopilot-stack.md`, `babysit.md`, `bug-fix.md`, `eval.md`, `feature.md`, `hillclimb.md`, `investigation.md`, `multi-phase-plan.md`, `opening-a-pr.md`, `orchestrate.md`, `pause-safely.md`, `perf-issue.md`, `prototype.md`, `refactoring.md`, `runtime-forensics.md`, `session-pickup.md`, `shipping.md`, `trace-forensics.md`, `visual-parity.md`, and `worktree-cleanup.md`.

The upstream agent and runtime pieces are also not ported.

- `agents/poteto-agent.md` is not converted to a Codex agent configuration.
- `agents/comment-sicko.md` is not converted to a Codex agent configuration.
- The 20 pstack scripts are not copied. This includes the PR watcher, worktree audit, plan checker, and orchestration helper.
- `automations/benny/` is not copied. It is a Cursor automation pack with Cursor UI adapters.
- Cursor model names and model-specific routing are not preserved.
- Cursor transcript paths under `~/.cursor/projects/.../agent-transcripts` are not preserved.
- Cursor UI control skills such as `control-ui`, `control-cli`, and `deslop` are not assumed.
- Cursor plugin metadata in `.cursor-plugin/plugin.json` is not included because Codex does not load it.

These are omissions, not hidden dependencies. A future port should add each feature only after its Codex equivalent has a clear runtime contract and a local verification check.

## Port decisions

The port uses Codex skill folders with a `SKILL.md` file. It does not copy Cursor frontmatter such as `mode`, `icon`, `color`, or `reminder`.

The port does not hard-code a model name. Codex model selection belongs to the Codex session or project configuration, not to this skill package.

The port treats delegation as optional. The workflow still requires independent reasoning for design reviews and parallel work, but it does not require a Cursor `Task` call or a particular model family.

The port keeps user-facing workflow names stable where that helps muscle memory. The instructions behind those names are rewritten when the upstream behavior depended on Cursor.

## Use it locally

This repo is the source tree. It is not linked into Codex automatically.

Review the port first.

```sh
cd /Users/liammclaughlin/PycharmProjects/pstack-codex
python3 scripts/verify_port.py
git status --short
```

When you are ready to activate it, link the skill folders into your local Codex skill directory after checking for name collisions.

The repository remains the source of truth. The entries under `~/.codex/skills` are only discoverability links for the local Codex runtime.

```sh
REPO=/Users/liammclaughlin/PycharmProjects/pstack-codex
mkdir -p "$HOME/.codex/skills"
for skill_dir in "$REPO"/skills/*; do
  skill_name=$(basename "$skill_dir")
  [ -e "$HOME/.codex/skills/$skill_name" ] && {
    printf 'Skipping existing skill %s\n' "$skill_name"
    continue
  }
  ln -s "$skill_dir" "$HOME/.codex/skills/$skill_name"
done
```

Start with `$poteto-mode` for a substantial task. Use `$how` for a code walkthrough, `$why` for design rationale, `$architect` before a cross-boundary change, `$tdd` when a focused regression test is practical, and `$show-me-your-work` when you want a durable decision trail.

## Verification

Run `python3 scripts/verify_port.py` after changing the active skill set. The check verifies that every active skill has a `SKILL.md`, has the required frontmatter fields, and does not contain the Cursor-only seams that this port promises to exclude.

This is a local source repository. No GitHub remote or runtime state is configured by default.
