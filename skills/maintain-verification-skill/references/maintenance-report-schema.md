# Maintenance receipt schema

The maintenance helper accepts four small JSON receipts. They are evidence
from a coordinator or read-only source reader, not instructions to execute.
Paths in a source reader or patch plan are checked before use.

## Target frontmatter

The target `SKILL.md` frontmatter is a YAML mapping with exactly two supported
keys: `name` and `description`. Both values must be non-empty strings. Duplicate
keys, unknown keys, malformed YAML, and non-string values are validation
errors. `name` must match the project-local target directory and the
`verify-<app>` name pattern.

## Source-reader report

The top-level object contains one `readers` entry for every feature file:

```json
{
  "readers": [
    {
      "feature_id": "check-state",
      "status": "complete",
      "summary": "An operator checks the current state from a terminal.",
      "source_entries": [
        {"path": "src/cli.py", "lines": "12-39"},
        {"path": "src/commands.py", "symbol": "check_state"}
      ],
      "authoritative": {
        "entry_points": ["Run the check command from a terminal."],
        "commands": ["python3 src/cli.py check --expected ready"],
        "selectors": [],
        "fixtures": ["state file containing ready"],
        "expected_results": ["stdout contains check passed"]
      },
      "harness": {"status": "available"},
      "live_recipe": {
        "actor": "operator",
        "fixture": "state file containing ready",
        "action": "run the check command",
        "expected_result": "stdout contains check passed",
        "evidence": "terminal receipt"
      }
    }
  ]
}
```

`status` may be `blocked` instead. A blocked reader supplies `reason` and
does not claim source coverage. Each source entry is an object with a
repository-relative path and a non-empty `lines` or `symbol` citation. The
helper checks that the path exists below `project-root` as a regular file; a
directory is not source evidence. A complete reader must include the
`authoritative` object and all five keys shown above. Each key must be an
explicit array of strings. Use an empty list for a category that does not
apply; omitting the key is invalid. `authoritative` keeps the exact
user-facing strings that the matching feature section or action must contain.
A missing harness uses
`{"status": "missing", "reason": "..."}` and becomes a `harness-gap`.

## Live-pass report

The top-level object contains one `doctor` receipt and one result per feature:

```json
{
  "doctor": {
    "status": "pass",
    "skill_root": "/project/.agents/skills/verify-tiny",
    "session_id": "run-17",
    "owned": true,
    "healthy": true,
    "target": "tiny-cli",
    "build": "git:cafebabe",
    "endpoint": "cli",
    "auth": "not-required",
    "disposable": true
  },
  "run": {
    "run_id": "run-17",
    "session_id": "run-17",
    "phase": "baseline"
  },
  "features": [
    {
      "feature_id": "check-state",
      "status": "pass",
      "source_match": true,
      "evidence": ["artifacts/check-state.txt"],
      "evidence_survives_cleanup": true
    }
  ]
}
```

Use these live statuses:

- `pass` requires a source-matching map, an existing evidence file, and an
  explicit confirmation that cleanup left the evidence in place.
- `fail` requires the attempted `route`, observed result, and evidence. With
  a healthy Doctor and `source_match: true`, it is a `product-gap`.
- `harness-gap` requires the attempted route and a reason. It is repairable
  only inside the verification directory, then must be driven again.
- `verified-unreachable` requires the attempted route and a concrete
  `prerequisite`. It records a boundary without pretending the path passed,
  and keeps the overall outcome blocked until the feature can be driven.
- `not-run` requires a reason and keeps the overall pass blocked.

The helper checks evidence paths as read-only files. It does not create,
move, or execute them. Doctor is strict in both `audit` and `doctor`: the
skill root, owned healthy session, target, build, endpoint, auth state, and
`disposable: true` must all be present.

## Safe patch plan

An apply plan contains explicit replacements:

```json
{
  "changes": [
    {
      "path": "features/check-state.md",
      "kind": "doc-drift",
      "old": "--expected stale",
      "new": "--expected ready",
      "reason": "The source reader cites src/cli.py:12-39.",
      "source": "src/cli.py:12-39"
    }
  ]
}
```

`path` is relative to the target verification directory. `kind` is
`doc-drift` or `harness-gap`; a `product-gap` cannot be applied. Every `old`
span must occur exactly once, every target must be an existing regular file,
and all targets must remain inside the verification directory. Feature-file
changes derive their affected feature from the path. Harness changes must set
`feature_ids` on the change or `affected_features` on the plan. The helper
preflights the complete plan before writing. It writes a separate
durable provenance record beside the patch plan and creates a helper-issued
apply record under the target's
`.maintain-verification-state/apply-records/` directory. That record stores
the plan identity, before-state, after-state, and a marker checked with a
per-target key stored with mode `0600`. The helper then emits a version-2
receipt with `apply_id`, `run_id`, `session_id`, `provenance_path`,
`apply_record_path`, `trust_marker`, `plan_sha256`, `affected_features`,
`before_file_digests`, and SHA-256 digests of the changed files. Save the
helper output unchanged as the change log and retain the provenance and state
record. The state directory is local helper state, not a feature artifact, and
must not be copied or committed. A hand-written receipt/provenance bundle
without that helper-issued record cannot prove `changed`.

For a post-correction live report, set `run.phase` to `post-apply` and copy
the apply receipt's `run_id`, `session_id`, and `apply_id` into the run object.
Every affected feature result also carries a `post_apply` object:

```json
{
  "redriven": true,
  "apply_id": "apply-...",
  "run_id": "run-17",
  "session_id": "run-17"
}
```

The final audit derives the apply-record path from the target and `apply_id`
and verifies the record's marker before comparing its contents. It requires
that object, compares those identities and the file digests, checks the
patch-plan identity and before-state in the helper-issued record, and requires
an actual before/after content transition before it can return `changed`. No
result-level identity fields or alternate post-apply keys are accepted. This
local marker protects the supported receipt interface, not a same-user caller
who can replace the private state directory or key; missing or invalid state
is an evidence failure.

## Cleanup manifest

Cleanup is allowlisted rather than recursive by default:

```json
{
  "owned_paths": ["state/attempt.json", "server.pid"],
  "evidence_paths": ["artifacts/check-state.txt"]
}
```

`owned_paths` are simple relative paths below a real disposable run root. The
run root must be outside the project and verification roots. Evidence is
checked before and after removal. Every owned target is preflighted before the
first deletion. If an owned path contains evidence, product/source roots, or
the manifest, the helper rejects the manifest before changing anything.

## Classification

The audit reports one of the three terminal outcomes:

- `clean` means no blocking finding and no applied file change.
- `changed` means no blocking finding and at least one applied correction,
  followed by live proof.
- `blocked` means any missing source or live coverage, failed Doctor, lost
  evidence, unresolved map drift, harness failure, invalid apply receipt, or
  product gap remains. `doc-drift` is reserved for a demonstrated source/map
  mismatch. Doctor, harness, and evidence failures retain their own finding
  kinds.

The finding kind identifies the boundary: `doc-drift`, `harness-gap`,
`product-gap`, `doctor-failure`, `verified-unreachable`, or `blocked`.
