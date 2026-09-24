---
name: how
description: Explain how an existing code path, subsystem, or repository works. Use for code walkthroughs, placement questions, and ownership or layering decisions.
---

# How

Explore a codebase and produce an explanation that gives another engineer a
working mental model. Use the local prompt contracts in
[explorer-prompt.md](references/explorer-prompt.md) and
[explainer-prompt.md](references/explainer-prompt.md) when the question is
complex enough to split.

## Step 1. Assess complexity

State the question and your interpretation. A small question covers one
module or symbol and uses one direct pass. A complex question spans files,
services, state transitions, or external boundaries and uses two to four
distinct local exploration angles before synthesis.

## Step 2. Explore

For each angle, find the entry point, trace the path, name the data shape at
each boundary, separate normal and error paths, list side effects, and cite
exact files and symbols. Give each worker an independent output path when
delegation is available. If it is not, run the angles in one session and keep
the scope bounded.

Do not replace a walkthrough with a list of filenames. Read implementations,
callers, persistence, retries, and boundary adapters.

## Step 3. Explain and synthesize

For a simple question, write the explanation from the direct pass. For a
complex question, reconcile the angles and check contradictions against the
repository. A missing trace becomes an explicit open question.

Use these sections when they apply:

- Overview
- Key Concepts
- How It Works
- Where Things Live
- Gotchas

Run a focused local check when a claim depends on runtime behavior. If the
matching surface is unavailable, state BLOCKED or INCONCLUSIVE.
