---
name: swarm
description: Split broad work into independent, verifiable units. Use for parallel investigation, repeated edits, coverage matrices, or work that would overload one context.
---

# Swarm

Fan out bounded work units, drain them, and return one report. Parallel work
only helps when each unit has a clear boundary.

## Start

Open a todo list with Frame, Fan out, Aggregate, and Report.

## Frame

1. State the done predicate and the report or artifact the swarm returns.
2. Choose slices, a race, or a mixed shape. Declare first-pass, rank-all, or
   best-of before starting a race.
3. Set N. N is the total worker count, not a hidden concurrency limit.
4. Select an available Codex worker capability and give every writer an
   independent output path.

## Fan out

Every brief stands alone. It includes goal, context, exact slice or race arm,
scope, acceptance, check, timebox, forbidden actions, and report fields.
Reports use PASS, ISSUES, or BLOCKED with evidence. A worker dropout is
recorded and does not silently become a passing slice.

Run independent units in parallel when the session supports it. Use a
sequential local fallback when it does not. Never let two units write the
same path or shared state without a stated owner.

## Aggregate

Read terminal results and inspect the named files and evidence. Coverage needs
one result per required slice. A race uses the rule declared in Frame.
Deduplicate issues and keep missing lanes visible. Run one integration check
in the parent scope.

## Report

Return the result table, evidenced issue one-liners, dropouts, blocked
capabilities, integration result, and side-effect log. A swarm result never
authorizes forge, merge, deployment, message, or deletion actions.

Do not parallelize dependent edits. Do not trust a summary without opening the
changed files or evidence it names.

UNSUPPORTED-SEAM: no implicit cloud worker, recurring watcher, or external
message surface is assumed. Use local fixture workers and stop at a missing
capability with BLOCKED.
