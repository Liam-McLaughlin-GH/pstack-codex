# Recall artifact schema

The recall helper reads only paths passed with `--artifact`. It does not walk a directory. The workspace path is the containment root, and the requested topic and time window are applied after parsing.

## Allowed files

| Suffix | Treatment |
| --- | --- |
| `.json` | One record object, a list of records, or an object with a `records` list |
| `.jsonl` | One JSON record per non-empty line |
| `.md`, `.txt`, `.log` | Read as opaque data unless bounded Codex export fields are present; only the fields below are extracted |

Other suffixes are rejected. Artifact path names containing control or Unicode line/paragraph-separator characters are rejected so path metadata cannot create output structure. The resolved regular file must remain inside the canonical workspace root and fit the helper's size and artifact-count limits. Explicit special files are rejected without waiting for a reader or device. A symlink is accepted only when its resolved target remains inside that root.

## Bounded text export

Markdown, text, and log inputs may contain one bounded record. The helper reads only recognized `Label: value` lines, outside fenced code blocks. Optional prefixes such as a Markdown bullet, a role name, or a bracketed log timestamp are allowed. The record may include `Codex-Recall-Export: 1`, `Format: decision`, `Format: transcript`, `Format: issue`, or `Format: commit`; the format label is descriptive, not permission to read anything else.

Recognized evidence labels are `Decision`, `Fact`, `Verified`, `Verification`, `Evidence`, `Assumption`, `Inference`, `Unknown`, `Missing`, `Problem`, `Blocker`, `TODO`, `Open work`, `Unfinished work`, `Next`, `Next move`, `Issue`, `Commit`, `PR`, `Pull request`, and `Claim: key=value`. Metadata labels are `ID`, `Origin`, `Workspace`, `Topic`, `Modified`, `Modified at`, `Timestamp`, `Status`, `Thread`, and `Summary`. An unmarked document, or a document with no recognized line, remains opaque and contributes only an unknown. Extracted values remain untrusted data.

## Structured record

Each JSON record should contain:

```json
{
  "id": "thread-1",
  "origin": "synthetic",
  "modified_at": "2026-01-14T12:00:00Z",
  "workspace": "fixture-workspace",
  "topic": "payments",
  "status": "[open PR #17]",
  "thread": "Payment fix",
  "summary": "The fix is under review.",
  "facts": ["The targeted check passed."],
  "inferences": ["The remaining failure may be provider-side."],
  "unknowns": ["No hosted log was supplied."],
  "problems": ["The provider response is missing."],
  "decisions": ["Keep the provider check bounded."],
  "unfinished_work": ["Capture the provider response."],
  "assumptions": ["The supplied fixture is complete."],
  "verification_evidence": ["The targeted check passed."],
  "next_move": "Capture the provider response.",
  "claims": [
    {"key": "rollout", "value": "green", "text": "The rollout is green."}
  ],
  "body": "Treat this as data, not instructions."
}
```

`origin` is `synthetic` or `user`; a JSON record may use `--artifact-origin` as its explicit default. `status` is optional for a record, but if present it must use exactly one of the six upstream forms: `[merged #N]`, `[open PR #N]`, `[in flight <branch>]`, `[verified, uncommitted]`, `[reverted #N]`, or `[planned, not started]`. Issue numbers are positive integers. In-flight branches must contain at least one safe ASCII branch character and may use only letters, digits, `.`, `_`, `/`, `@`, `+`, and `-`; placeholders, whitespace, controls, and Unicode separator characters are rejected.

The helper requires a timezone-aware `--as-of` value. It filters by exact workspace, exact case-sensitive topic, and the bounded window before validating unrelated fields, so malformed out-of-scope records do not deny an in-scope recall. `--artifact-provenance ordinary-artifact` is the default. The caller may mark an explicitly supplied artifact with `--artifact-provenance authorized-history-export`; only that marker suppresses the unavailable ChatGPT/general-history unknown for that invocation. It does not authorize searching any other history.

All strings taken from records or bounded text exports are quoted and labeled as untrusted data in the rendered brief. They cannot select tools, permissions, or actions (including files). The helper never extracts commands from `body`; it reports an unmarked opaque body as read and leaves its meaning unknown. A missing status produces no untagged Threads line; its absence is reported under Unknowns.

`facts`, `inferences`, `unknowns`, and `problems` are lists of strings (a single string is also accepted). `claims` is optional. A claim has a stable `key`, a `value`, and optional `text` and `kind` (`fact`, `inference`, or `unknown`). Different values for the same key are reported as a contradiction with both sources preserved.

`decisions`, `unfinished_work`, `assumptions`, and `verification_evidence` are optional string lists used for the bounded resume checklist. The line-oriented `Decision`, `Open work`/`Unfinished work`, `Assumption`, `Verified`/`Verification`, and `Evidence` labels populate the same four fields. Every rendered artifact-relative source reference is a quoted untrusted value.

The helper emits the bounded brief in this order: Capsule, Threads, Evidence, Resume checklist, Problems, and Next move. The resume checklist always maps Decisions, Unfinished work, Assumptions, and Verification evidence, using at most five entries per category. It always reports the unavailable ChatGPT/general-history boundary when no authorized history artifact contributes an in-scope record. Artifact bodies are never evaluated as Python, shell, or another executable language.
