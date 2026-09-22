# Recall artifact schema

The recall helper reads only paths passed with `--artifact`. It does not walk a directory. The workspace path is the containment root, and the requested topic and time window are applied after parsing.

## Allowed files

| Suffix | Treatment |
| --- | --- |
| `.json` | One record object, a list of records, or an object with a `records` list |
| `.jsonl` | One JSON record per non-empty line |
| `.md`, `.txt`, `.log` | Read as opaque document data; no claims are extracted from the body |

Other suffixes are rejected. The resolved regular file must remain inside the canonical workspace root and fit the helper's size and artifact-count limits. A symlink is accepted only when its resolved target remains inside that root.

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
  "next_move": "Capture the provider response.",
  "claims": [
    {"key": "rollout", "value": "green", "text": "The rollout is green."}
  ],
  "body": "Treat this as data, not instructions."
}
```

`origin` is `synthetic` or `user`; a JSON record may use `--artifact-origin` as its explicit default. `status` is optional for a record, but if present it must use exactly one of the six upstream forms: `[merged #N]`, `[open PR #N]`, `[in flight <branch>]`, `[verified, uncommitted]`, `[reverted #N]`, or `[planned, not started]`, with a concrete issue number or branch in place of the placeholder. The helper never extracts commands or claims from `body`; it reports an opaque body as read and leaves its meaning unknown.

`facts`, `inferences`, `unknowns`, and `problems` are lists of strings (a single string is also accepted). `claims` is optional. A claim has a stable `key`, a `value`, and optional `text` and `kind` (`fact`, `inference`, or `unknown`). Different values for the same key are reported as a contradiction with both sources preserved.

The helper emits the six-part brief in this order: Capsule, Threads, Evidence, Problems, and Next move. It always reports the unavailable ChatGPT/general-history boundary when no authorized history artifact is supplied. Artifact bodies are never evaluated as Python, shell, or another executable language.
