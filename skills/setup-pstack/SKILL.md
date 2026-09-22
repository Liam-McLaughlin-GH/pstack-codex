---
name: setup-pstack
description: Discover models from the active Codex runtime and preview or apply pstack per-role custom agents with verified model IDs and bounded reasoning effort. Use for setup-pstack, configuring pstack models, changing the pstack budget, or reviewing an existing pstack agent configuration.
---

# Setup Pstack

Configure pstack through Codex custom-agent TOML. Keep the repository skill as the source of truth, and require an explicit user request before applying files outside the current workspace.

## Runtime contract

Use the current Codex surfaces documented in the [skill guide](https://developers.openai.com/plugins/build/skills) and [configuration schema](https://developers.openai.com/codex/config-schema.json):

- Project skills are discovered from `.agents/skills`.
- User skills are discovered from `${CODEX_HOME:-$HOME/.codex}/skills`; the same `CODEX_HOME` value owns user custom agents at `${CODEX_HOME:-$HOME/.codex}/agents`.
- Project custom agents live in `.codex/agents`. Set `CODEX_HOME` explicitly when an isolated user discovery root is required; do not assume that `~/.agents` is a Codex user root.
- A custom agent can set `model` and `model_reasoning_effort`. Omitting either field lets the parent session supply it.
- A skill does not grant permission, change the parent sandbox, or authorize external actions.

The upstream pstack skill at commit `53e579f1481697931fc44f5445171397cfa2b24b` writes an always-applied Cursor rule and routes work through Cursor controls. Cursor's `~/.cursor/rules/pstack-models.mdc` target is unsupported here; never write it. Cursor `Task` calls and `AskQuestion` are unsupported here; use the parent Codex session, explicit prompt input, or a documented manual fallback. <!-- UNSUPPORTED-SEAM: Cursor path; Cursor Task; AskQuestion --> Preserve this provenance and the repository [MIT license](../../LICENSE).

## Role boundary

This port generates the two upstream `reflect` records as dormant configuration placeholders: `reflect-tooling` and `reflect-judgment` have no active Codex `reflect` consumer in this repository, so their files do not route work or create a missing workflow. The upstream `divergent` and `synthesizer` mappings are not generated; requesting either role is rejected until a Codex consumer exists. This is a deliberate boundary, not full upstream role parity.

## Discover models

Resolve the helper from the source checkout or an installed skill before running it. From the repository root:

```sh
python3 skills/setup-pstack/scripts/configure.py discover --json
```

For a project-local installation, run the same helper from the repository root with `SETUP_PSTACK_SKILL=.agents/skills/setup-pstack`. For a user installation, use `SETUP_PSTACK_SKILL="${CODEX_HOME:-$HOME/.codex}/skills/setup-pstack"`. In either case, the runnable form is:

```sh
python3 "$SETUP_PSTACK_SKILL/scripts/configure.py" discover --json
```

The helper runs the documented experimental [`codex debug models`](https://learn.chatgpt.com/docs/developer-commands?surface=cli#cli-codex-debug-models) command and accepts only the model IDs and reasoning efforts returned as JSON. `--bundled` reads the catalog shipped with the CLI and is useful offline, but it is not proof that the signed-in surface or API identity can use every entry. The [Codex app-server](https://learn.chatgpt.com/docs/app-server) `model/list` contract is another supported source; pass its JSON response to the configuration command. If neither source is available, report the gap and ask for the active runtime's model list. Do not invent a model ID or copy a model setting from another Codex surface.

## Preview and apply

Ask for the budget and any role changes before applying. The helper maps `small`, `medium`, `large`, and `unlimited` to `medium`, `high`, `xhigh`, and `max`, then selects the highest effort that the chosen model actually reports at or below that ceiling. It never upgrades to an unsupported effort.

Preview against an explicit agent directory. The source-checkout command is runnable from the repository root:

```sh
python3 skills/setup-pstack/scripts/configure.py preview \
  --catalog /tmp/pstack-models.json \
  --agents-root .codex/agents \
  --budget medium \
  --default-model MODEL_ID \
  --json
```

Use `--role ROLE=MODEL_ID` for a role-specific choice. Panel roles accept one model for every runner or a comma-separated list with the panel's exact fan-out. `inherit-parent` and `auto` mean that the generated agent omits `model`; they are input aliases, not Codex configuration values. On a rerun, existing role overrides, panel lists, and aliases remain in place unless the current invocation explicitly changes that role or default.

Show the full preview and obtain user confirmation before running the same command with `apply`. The helper refuses an unreported model, a broken or valid symlink, and an existing agent file with different contents. Re-running an unchanged plan is a no-op. A changed generated file is replaced only when the reviewed apply includes `--replace`; the complete staged set is then published with rollback on failure.

Use [scripts/configure.py](scripts/configure.py) for model discovery and agent configuration. Use [scripts/install.py](scripts/install.py) only to install this skill into an explicit Codex discovery directory. It defaults to preview, rejects all source symlinks, snapshots source bytes through no-follow handles, stages copies before atomic directory publication, leaves collisions untouched, and reports cleanup residue instead of silently discarding it. Configuration replacement holds the parent-directory transaction lock and uses an atomic directory exchange when the platform provides one; its recovery fallback uses no-replace publication and preserves a raced replacement. Readers see complete generations rather than a mixed set; the fallback can briefly expose an absent root while it recovers.

An apply reports `transaction.status` as `published`, `unchanged`, `published_cleanup_pending`, `root_replacement_cleanup_pending`, or `publication_recovery_pending`. The cleanup-pending states identify a live publication or a detected root replacement and retain the remaining generation residue at `transaction.residue`; rerun the exact `transaction.retry` command after the transient cleanup failure. `publication_recovery_pending` is intentionally not an automatic cleanup: inspect the reported root and residue, then preview again. A pre-publication failure reports rollback; if cleanup itself fails, the error names the staging residue and gives the same retry command. Do not remove a reported residue by an unrelated path or broad cleanup command.

From the repository root, run the helpers with their source paths:

```sh
python3 skills/setup-pstack/scripts/install.py --self-test
python3 skills/setup-pstack/scripts/configure.py self-test
```

After installation, run the installed helper from the repository root with an explicit prefix:

```sh
# Project-local install.
SETUP_PSTACK_SKILL=.agents/skills/setup-pstack
python3 "$SETUP_PSTACK_SKILL/scripts/install.py" --self-test
python3 "$SETUP_PSTACK_SKILL/scripts/configure.py" self-test

# User install, using the same CODEX_HOME contract as discovery.
CODEX_HOME="${CODEX_HOME:-$HOME/.codex}"
SETUP_PSTACK_SKILL="$CODEX_HOME/skills/setup-pstack"
python3 "$SETUP_PSTACK_SKILL/scripts/install.py" --self-test
python3 "$SETUP_PSTACK_SKILL/scripts/configure.py" self-test
```

These commands do not select or mutate a discovery root implicitly.

If a pstack workflow needs a Cursor-only UI, transcript store, automation, or model router, stop at that boundary. State `UNSUPPORTED` or `NOT RUN`, then continue with the closest Codex skill, parent-session delegation, local evidence, or a user-provided artifact. Do not replace a missing capability with a pretend adapter.
