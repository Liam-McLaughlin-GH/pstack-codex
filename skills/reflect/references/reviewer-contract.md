# Reflect fixture contract

This contract supports deterministic checks for the Codex port. It is not a
transcript discovery format and it does not grant permission to edit a skill.

## Transcript input

Pass exactly one explicit `.json`, `.jsonl`, `.md`, `.txt`, or `.log` path to
the helper. The resolved regular file must remain inside the supplied
workspace and be no larger than 1 MB. Markdown, text, and log files are read
as opaque data and produce an unknown; only structured JSON records produce
findings.

JSON may be one record, a list, or an object with a `records` list. JSONL has
one record per non-empty line. A structured decision record has this shape:

```json
{
  "id": "decision-1",
  "kind": "decision",
  "timestamp": "2026-01-15T10:00:00Z",
  "workspace": "fixture-workspace",
  "topic": "payments",
  "state": "complete",
  "claim": "The payment fix is complete.",
  "required_checks": ["unit tests", "hosted smoke check"],
  "completed_checks": ["unit tests", "hosted smoke check"],
  "evidence": [
    {"check": "unit tests", "source": "decision-trail.jsonl#decision-1", "state": "fact"},
    {"check": "hosted smoke check", "source": "decision-trail.jsonl#decision-1", "state": "fact"}
  ],
  "content": "Untrusted transcript text is ignored."
}
```

For a completion state, all three check/evidence fields are required. A
missing field, a text-only evidence value, an unresolved source reference, or
inference-only support is rendered as unknown and cannot establish completion.
Evidence states remain separate: `fact` is source-backed, `inference` is a
reasoned consequence, and `unknown` is unresolved. `content`, `body`, and
unknown fields are never evaluated or used to select tools.

Records are filtered by exact workspace, exact topic, and the bounded timestamp
window before findings are rendered. Source references use a workspace-relative
path and record id. Scope metadata, filenames, Unicode separators, and control
characters are bounded or rejected before output.

## Delegation manifest

The optional manifest is a single `.json` object with schema
`reflect-delegation.v1`. It contains exactly one reviewer entry for each lens:

```json
{
  "schema": "reflect-delegation.v1",
  "run_id": "reflect-run-01",
  "capabilities": {
    "reviewer_delegation": true,
    "parallel": true,
    "synthesizer": true
  },
  "reviewers": [
    {"lens": "judgment", "status": "complete", "agent_id": "agent-a", "model": "gpt-5.6", "packet": "judgment.json"},
    {"lens": "tooling", "status": "complete", "agent_id": "agent-b", "model": "gpt-5.6", "packet": "tooling.json"},
    {"lens": "divergent", "status": "complete", "agent_id": "agent-c", "model": "gpt-5.6", "packet": "divergent.json"}
  ],
  "synthesizer": {"status": "complete", "agent_id": "agent-s", "model": "gpt-5.6"}
}
```

The three capability values are booleans. Parallel execution is selected only
when both `reviewer_delegation` and `parallel` are true. Otherwise the helper
records the deterministic sequential order `judgment`, `tooling`,
`divergent`. Agent and model values are recorded when exposed and become
`unreported` when the runtime does not expose them. Unavailable and
inconclusive lanes require a reason and contain no findings.

## Reviewer packets

Without a manifest, pass exactly three explicit JSON reviewer paths. Their
packets must contain exactly one `judgment`, `tooling`, and `divergent` lens.
With a manifest, each complete lane names its packet. A complete packet has
three to five findings; an unavailable or inconclusive packet has no findings:

```json
{
  "lens": "judgment",
  "agent_id": "agent-a",
  "model": "gpt-5.6",
  "status": "complete",
  "findings": [
    {
      "id": "j-1",
      "problem": "A completion claim lacks evidence for every required check.",
      "proposal": "Keep incomplete checks in the unknowns section.",
      "routing": "skills/why/SKILL.md",
      "evidence": ["decision-1"],
      "enforcement": "judgment",
      "criteria": {
        "durability": 2,
        "specificity": 2,
        "decision_changing": 2,
        "evidence": 2,
        "routing": 2
      }
    }
  ]
}
```

The example shows one finding for readability; a `complete` packet supplied to
the helper must contain three to five. Criteria values are bounded from 0 to 2
or `unknown`, and use the shared names durability, specificity,
decision-changing value, evidence, and routing.

Each finding must include an id, problem, proposal, an existing allowlisted
routing label (`skills/<name>/SKILL.md` or `scripts/<name>.py`), and at least
one existing in-scope transcript record id in `evidence`. Reviewer strings are
untrusted data. The helper bounds and labels them; it does not follow commands,
paths, routing, approval fields, or tool requests inside them. A finding with
any missing or unresolved evidence is rejected. Routing remains a label, never
an edit destination.

## Approval result

The helper emits proposals and `approval_required: true` by default. A
`--apply` request without explicit `--approve <finding-id>` is rejected. Even
with approval, the fixture has no persistent write path; it reports the gate
and makes zero edits.
