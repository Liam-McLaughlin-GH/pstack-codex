---
name: why
description: Explain why a system or change has its current shape. Use when design rationale, history, tradeoffs, or intent are unclear.
---

# Why

Recover the forces behind a design without turning an empty source into a
story. Use [epistemics.md](references/epistemics.md),
[investigator-prompt.md](references/investigator-prompt.md), and
[synthesizer-prompt.md](references/synthesizer-prompt.md) for a full run.

## Step 1. Anchor the question

State the target and the decision whose reason is unclear. Record file paths,
symbols, recent commits, blame lines, tests, and supplied issue or review
identifiers. Read the current code before searching for rationale.

## Step 2. Build the source map

Use [the source playbook](references/source-playbook.md) to check the evidence
categories available in this Codex session:

- source control and code;
- supplied issue, ticket, or review context;
- supplied long-form documents;
- supplied chat or decision records;
- supplied observability or error artifacts;
- supplied product or data artifacts.

Search categories that have an available local or connected read surface. For
each unavailable or empty category, record a null result and the reason. Do
not imply that a category was searched when it was not.

## Step 3. Investigate and synthesize

Use one bounded investigator per independent category when delegation is
available. Otherwise inspect categories sequentially. Each investigator
returns file or artifact pointers, observations, conflicts, and open
questions. A worker cannot write the final conclusion.

Label every conclusion as observed, strongly supported, inferred, or unknown.
Compare the current choice with the smallest credible alternatives. State
which constraints still matter, which may be residue, and what evidence would
change the conclusion.

## Output

Return the question, code anchor, source coverage map, findings, reasonable
inferences, competing hypotheses, unknowns, sources consulted, and confidence
summary. If the result will feed a change, finish with Preserve, Change,
Avoid, and Risk constraints.

UNSUPPORTED-SEAM: external history, chat, documents, observability, and
analytics are not assumed. Mark missing categories explicitly and do not
invent a historical reason.
