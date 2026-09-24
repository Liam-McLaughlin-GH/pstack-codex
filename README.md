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

The following skills are present as Codex-native rewrites.

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
| `reflect` | Reviews one explicit transcript through three bounded lenses, records source evidence and uncertainty, compares proposals, and uses a manifest-driven delegation contract with sequential fallback. |
| `make-bot-ui` | Builds and verifies a UI relay with explicit local and remote boundaries. |
| `repo-best-practices` | Routes Showpass repository work using source-backed ownership, safe command boundaries, and explicit drift limits. |

The following seven issue-focused skills are also active Codex-native ports:

| Skill | Codex port |
| --- | --- |
| `automate-me` | Runs bounded local automation with explicit fixture and side-effect boundaries. |
| `create-verification-skill` | Builds verification skills from a local feature map and saved evidence. |
| `figure-it-out` | Routes structured investigation and records lifecycle state locally. |
| `maintain-verification-skill` | Maintains verification artifacts without assuming Cursor layout. |
| `python-best-practices` | Checks Python structure and review boundaries with local tooling. |
| `recall` | Reconstructs supplied local history without scanning private transcripts. |
| `setup-pstack` | Prepares Codex-local activation through temporary and explicit configuration. |

The port preserves the names of these workflows where possible. In Codex, invoke them as skills such as `$poteto-mode`, `$how`, `$architect`, or `$tdd`.

The 23 poteto-mode playbooks are active under
skills/poteto-mode/playbooks. They retain the upstream workflow contracts and
use the local capability and side-effect rules in
skills/poteto-mode/references/capability-boundaries.md. Forge, watcher,
deployment, message, delegation, and deletion steps remain fixture-only or
require explicit authorization.
The source and MIT attribution remain in the source and license section above.

## What is missing

The remaining upstream runtime pieces are not ported as live services.

- Automatic project-local role discovery is not claimed; use the explicit `.codex/config.toml` registration.
- The live provider and host wrappers from the pstack scripts remain unsupported; the useful offline subset and file-by-file dispositions are in `skills/poteto-mode/scripts/g3/DISPOSITION.md`.
- `automations/benny/` is not copied. It is a Cursor automation pack with Cursor UI adapters.
- Cursor model names and model-specific routing are not preserved.
- Cursor transcript paths under `~/.cursor/projects/.../agent-transcripts` are not preserved.
- Cursor UI control skills such as `control-ui`, `control-cli`, and `deslop` are not assumed.
- Cursor plugin metadata in `.cursor-plugin/plugin.json` is not included because Codex does not load it.

These are omissions, not hidden dependencies. A future port should add each feature only after its Codex equivalent has a clear runtime contract and a local verification check.

## Port decisions

The port uses Codex skill folders with a `SKILL.md` file. It does not copy Cursor frontmatter such as `mode`, `icon`, `color`, or `reminder`.

The port does not hard-code a model name. Codex model selection belongs to the Codex session or project configuration, not to this skill package.

The port treats delegation as a capability. `reflect` accepts a bounded Codex
delegation manifest with reviewer and synthesizer identity when exposed. It
uses a fixed sequential fallback when parallel reviewer delegation is not
available, and records unavailable or inconclusive lanes instead of treating
them as successful empty results. It does not require a Cursor `Task` call,
Cursor transcript path, or a particular model family.

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

The repository remains the source of truth. The entries under
`$CODEX_HOME/skills` are only discoverability links for the local Codex
runtime.

```sh
REPO="${PSTACK_CODEX_REPO:-$(pwd)}"
CODEX_HOME="${CODEX_HOME:-$HOME/.codex}"
CODEX_SKILLS_DIR="$CODEX_HOME/skills"
mkdir -p "$CODEX_SKILLS_DIR"
for skill_dir in "$REPO"/skills/*; do
  skill_name=$(basename "$skill_dir")
  [ -e "$CODEX_SKILLS_DIR/$skill_name" ] && {
    printf 'Skipping existing skill %s\n' "$skill_name"
    continue
  }
  ln -s "$skill_dir" "$CODEX_SKILLS_DIR/$skill_name"
done
```

Start with `$poteto-mode` for a substantial task. Use `$how` for a code walkthrough, `$why` for design rationale, `$architect` before a cross-boundary change, `$tdd` when a focused regression test is practical, and `$show-me-your-work` when you want a durable decision trail.

## Verification

Run `python3 scripts/verify_port.py` after changing the active skill set. The check verifies that every active skill has a `SKILL.md`, has the required frontmatter fields, and does not contain the Cursor-only seams that this port promises to exclude.

This is a local source repository. No GitHub remote or runtime state is configured by default.
