# Models and usage controls

Use the local task-model-routing policy, adjusted for the user's scarce remaining usage. The installed models_cache.json advertises gpt-5.6-luna, gpt-5.6-terra and gpt-5.6-sol with the reasoning levels below. codex login status reports ChatGPT authentication. Catalog presence does not prove quota availability; verify the first launch and record errors.

| Role | Model | Effort | Allocation |
| --- | --- | --- | --- |
| Coordinator using this fixed plan | gpt-5.6-luna | max | One persistent coordinator |
| Ten issue implementations | gpt-5.6-luna | max | One owner per issue, two concurrent maximum |
| Three repo scouts | gpt-5.6-luna | max | Sequential subtasks inside #10 worker, reuse findings for #9 |
| Independent review | gpt-5.6-luna | max | Three bounded batches, different agent from authors |
| Architecture escalation | gpt-5.6-terra | max | Zero routine calls; at most two short consultations for primary ten issues |
| Security/correctness escalation | gpt-5.6-sol | xhigh | Zero routine calls; at most one focused review across primary ten issues |

Do not dispatch Terra just to repeat this plan. Two meaningful failed Luna repairs on the same cause, or an unresolved cross-skill contract that blocks multiple issues, earns one Terra consultation. Return implementation to Luna with its decision. Sol is reserved for unresolved unsafe install behavior, cross-session disclosure, or similarly consequential contradictions that Luna/Terra cannot clear. It receives only the disputed diff and evidence.

Planning estimate: 10 implementation sessions + 3 batch reviews + 1 combined-tree validation = 14 primary worker sessions, plus the coordinator. At most 3 primary escalations makes 17. Repairs and supplemental parity work add sessions; record each before launch. These counts are scheduling estimates, not token or quota guarantees. A single Max session can consume more than several small tasks.

Optimize duplicate context and retries before reducing correctness: one shared source inventory, one #10 repo reconnaissance reused by #9, narrow diffs for review, no four-model arena, no repeated full-repo scans. Use shell checks without LLM calls for metadata, PR state and file validation. Do not assume parallel execution saves quota.

At start, use any supported usage display exposed by the current runtime; if unavailable record UNKNOWN. No billing APIs or auth-file inspection. After each completion update sessions, reported token usage if available, retries and escalations. On a limit, preserve state and exit without retry loops or paid API substitution. The supplemental wave requires a fresh usage checkpoint; publish primary PRs before spending on it.

Example worker command, after checking installed CLI help:
```sh
codex exec --yolo -m gpt-5.6-luna -c model_reasoning_effort='"max"' -c service_tier='"default"' -C /absolute/issue-worktree - < /absolute/worker-prompt.md
```
The flags are process-local. Verify actual accepted model/effort from launch output; do not report a requested model as observed. Use the full bypass flag if --yolo alias is unavailable. Do not combine it with -a never. Use managed subprocesses/native waits, not an abandoned background shell. Read Herdr guidance first if using Herdr for visible persistent workers. No unattended execution survives a sleeping/offline host by promise alone.
