---
name: show-me-your-work
description: Keep an append-only decision trail for long, autonomous, or review-heavy work. Use when someone needs to audit the choices and evidence later.
---

# Show me your work

Keep one TSV file with one row per meaningful decision or checkpoint.

Use `.audit/<task-slug>.tsv` for a local trail. Keep it out of git unless the reviewer needs the record to trust the result. Each row has these columns.

```text
ts\tphase\tdecision\twhy\tevidence\tresult
```

Log a fork, a completed unit with its verification, a pivot, a revert, or a blocker. Do not log every command.

Evidence must point to a file, line, commit, test output, or artifact. The result must say what happened. Use `open`, `verified`, `failed`, `reverted`, or `blocked` when those states fit.

Before handing back, read the trail from top to bottom and remove rows that do not describe a real action. Check that every evidence pointer resolves. Report any unresolved gap instead of smoothing it over.
