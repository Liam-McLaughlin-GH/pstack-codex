---
name: show-me-your-work
description: Keep an append-only decision trail for long, autonomous, or review-heavy work. Use when someone needs to audit the choices and evidence later.
---

# Show me your work

Keep one canonical, append-only TSV file with one row per meaningful decision
or checkpoint. Use
[the header template](references/decision-log-template.tsv) and the local
[log helper](scripts/log.sh) when they fit the environment.

Use .audit/task-slug.tsv for a local trail. Keep it out of git unless the
reviewer needs it to trust the result. Columns are:

    ts	phase	decision	why	evidence	result

ts is an ISO 8601 timestamp. Phase names the workstream. Decision and why are
single-line facts. Evidence is a pointer. Result is PASS, FAIL, REVERTED,
BLOCKED, NOT RUN, or another explicit predicate state.

Log a fork, completed unit, pivot, revert, blocker, or gate. Do not log every
command. Evidence points to a file, line, commit, test output, or artifact.

The helper strips tabs, newlines, and carriage returns and prefixes formula
leading cells. Test the helper with fixture data before using generated text.
Append a correction row instead of editing history.

## Audit

Before handoff, read the trail top to bottom. Every row must map to an action,
and every evidence pointer must resolve. Add missing forks or gaps. Do not
rewrite the story to fit the trail.

When an independent reviewer capability is available, ask it to inspect the
trail and the supplied run record. It checks weak evidence, skipped checks,
risky choices, and gaps. If no independent reviewer is available, record
BLOCKED and perform the local audit yourself.

Every run that produces a trail ends with an Attention section. Name the
reviewer capability or local fallback and list flags, or state no flags. The
trail is local by default and carries no external message permission.

UNSUPPORTED-SEAM: private session records and remote transcript stores are not
read implicitly. Use the supplied local run record, branch, and evidence paths.
