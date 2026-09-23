---
name: no-comments
description: Review comments and suppressions in a diff. Delete stale narration and encode real constraints in code, types, tests, or checks when that is in scope.
---

# No comments

Review comments as part of the code. A second reviewer can provide the
Comment Sicko perspective when that role is installed. Otherwise use the same
rubric locally and record the fallback.

## Scope

Use the caller's files or the current local diff against the declared base.
Read the surrounding code, callers, tests, and external contract before
classifying a comment.

## Review steps

1. Produce a finding table with path, comment, category, evidence, and action.
2. Delete comments that restate code or describe a step that no longer exists.
3. Keep a comment only when code cannot express the reason and the reason is
   still true.
4. Keep external-contract, safety, regulatory, or non-obvious constraint
   comments until a cheaper executable encoding exists.
5. For accepted findings, make the smallest in-scope deletion or encoding.
   Do not delete application code, widen the scope, or remove a safety
   suppression without proof.
6. If a finding is ambiguous, run $how or $why on the symbol. If it remains
   ambiguous, report it instead of restoring a stale comment.
7. Rerun once after rejecting a reviewer report. Name the rejected report and
   the reason. A second rejection is an open failure, not a pass.
8. Run $architect when an accepted encoding needs a new shape. Stop at the
   sketch before changing broader code.
9. Run the narrowest relevant check. No external write or review reply is
   implied.

## Approval boundary

An accepted low-risk finding can be fixed locally. A constraint comment needs
an owner or user decision when its encoding changes an external contract,
permission boundary, billing behavior, data safety, or deletion rule. Record
that gate and stop the external action.

Reply with scope, deletion count, restored comments and evidence, reruns,
architect sketch if any, encodings, unenforced constraints, open findings,
and the local check result.

UNSUPPORTED-SEAM: a named Comment Sicko role, reviewer pool, or remote thread
surface may be absent. Use a local second pass and report the missing lane.
