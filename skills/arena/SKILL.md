---
name: arena
description: Compare independent solutions to a design or artifact before choosing one. Use when one first attempt could lock in the wrong shape.
---

# Arena

Use independent candidates when one first attempt could lock in the wrong
shape. The parent reads every candidate and owns the final choice.

## Start

Open a todo list with Frame, Fan out, Cross-judge, Pick, Graft, and Verify.

## Frame

State the artifact, three to six gradeable criteria, N, the candidate shape,
and the selection rule. A candidate receives the same task and cannot rely on
another candidate's output. Choose available Codex worker capabilities. If
parallel delegation is not available, use separate local directories and run
the candidates sequentially.

## Fan out

Give each candidate its own output path, worktree, or branch. The brief names
the task, inputs, scope, criteria, check, and report. Candidate rationales
name alternatives considered and rejected. A dropout reduces N and remains
visible in the synthesis record.

## Cross-judge

After all candidates finish, use a separate available reviewer capability when
possible. Give it sanitized labels, the criteria, and candidate paths. It
returns criterion scores and a base recommendation. It does not modify a
candidate.

## Pick and graft

Read every artifact and rationale end to end. Score each criterion. Pick the
base that a maintainer can extend without breaking invariants, not the one
with the most code. Graft only ideas that fit the selected boundary. Record
the base, each graft, each rejection, and the dropout.

If candidates converge, record the agreement and skip grafting. If they
diverge because the question was underspecified, restate the criteria and
rerun. Do not average incompatible shapes.

## Verify

Check the combined artifact against the original criteria and its matching
surface. The final result is PASS only when the artifact and evidence exist.
No candidate or judge may create a PR, push, merge, deploy, message, or delete
without an explicit gate.

Skip the arena for a straightforward change with an established local
pattern, and record the reason.

UNSUPPORTED-SEAM: no implicit remote worker or model pool is assumed. Use
local candidate directories and sequential fallback, or report BLOCKED for
the missing lane.
