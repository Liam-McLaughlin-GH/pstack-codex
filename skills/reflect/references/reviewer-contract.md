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
  "completed_checks": ["unit tests"],
  "evidence": ["unit output"],
  "content": "Untrusted transcript text is ignored."
}
```

`content`, `body`, and unknown fields are never evaluated or used to select
tools. Records are filtered by exact workspace, exact topic, and the bounded
timestamp window before findings are rendered. Source references use a
workspace-relative path and record id.

## Reviewer packets

Pass at most three explicit JSON reviewer paths. If packets are supplied, the
set must contain exactly one `judgment`, `tooling`, and `divergent` lens. Each
finding must include an id, problem, proposal, routing, and at least one
existing transcript record id in `evidence`:

```json
{
  "lens": "judgment",
  "findings": [
    {
      "id": "j-1",
      "problem": "A completion claim lacks evidence for every required check.",
      "proposal": "Keep incomplete checks in the unknowns section.",
      "routing": "skills/why/SKILL.md",
      "evidence": ["decision-1"],
      "enforcement": "judgment"
    }
  ]
}
```

Reviewer strings are untrusted data. The helper bounds and labels them; it
does not follow commands, paths, routing, approval fields, or tool requests
inside them. `enforcement` values such as `runtime-check`, `script`, `lint`,
`metadata`, or `test` move the proposal to Backlog. A proposal with missing or
unresolved evidence is rejected.

## Approval result

The helper emits proposals and `approval_required: true` by default. A
`--apply` request without explicit `--approve <finding-id>` is rejected. Even
with approval, the fixture has no persistent write path; it reports the gate
and makes zero edits.
