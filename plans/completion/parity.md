# Remaining upstream parity

Deliver the ten existing issue PRs first. Then assess these packages against the pinned upstream tree. Create one deduplicated supplemental issue and PR per package when executing. Never classify absent work as complete merely because it lacks an existing issue.

| Package | Scope | Acceptance |
| --- | --- | --- |
| G1 playbooks | All 23 upstream playbooks listed in README, plus restore needed behavior in the nine abbreviated workflow ports | Per-playbook mapping of preserved/adapted/unsupported steps; links resolve; representative execution paths exercised; no unconditional external writes |
| G2 agent definitions | poteto-agent and comment-sicko; optional packaging | Current Codex agent schema verified; discovery exercised in isolated project; fallback when named agent/model unavailable |
| G3 helper tooling | Inventory all 21 files under upstream scripts, including watcher, orchestrator, plan checker, worktree audit and log helper | Separate code from manifests/tests/lockfiles; port useful tools with offline fixtures; replace Cursor adapters; no real merge/deletion in tests |
| G4 Benny and controls | Inspect actual automation source and external control dependencies | Supported Codex adapter or documented unsupported boundary with evidence and follow-up; runtime/credentials absence is a blocker, not a stub presented as parity |

Default all supplemental workers to Luna Max, max two concurrent. Reuse primary contract/reconnaissance. Terra consultation only within the remaining escalation allowance, not automatically per package. If quota prevents G1–G4, leave explicit residual scope and resumable tasks. Report primary issue completion separately from full parity.

Do not activate real PR watchers, auto-merge, deployment automation, or worktree deletion to test this port. Test side effects with controlled fixtures. Preserve original license and attribution on adapted material.
