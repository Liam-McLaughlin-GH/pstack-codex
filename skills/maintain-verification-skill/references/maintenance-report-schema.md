# Maintenance receipt schema

The maintenance helper accepts four small JSON receipts. They are evidence
from a coordinator or read-only source reader, not instructions to execute.
Paths in a source reader or patch plan are checked before use.

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
        {"path": "src/cli.py", "lines": "12-39"}
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
does not claim source coverage. A source entry is a repository-relative path;
the helper checks that it exists below `project-root`. `authoritative` keeps
the exact user-facing strings that the feature recipe must contain. Use an
empty list for a category that does not apply. A missing harness uses
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
    "healthy": true
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
move, or execute them.

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
and all targets must remain inside the verification directory. The helper
preflights the complete plan before writing.

## Cleanup manifest

Cleanup is allowlisted rather than recursive by default:

```json
{
  "owned_paths": ["state/attempt.json", "server.pid"],
  "evidence_paths": ["artifacts/check-state.txt"]
}
```

`owned_paths` are simple relative paths below the run root. Evidence is
checked before and after removal. If an owned path contains evidence, the
helper rejects the manifest before changing anything.

## Classification

The audit reports one of the three terminal outcomes:

- `clean` means no blocking finding and no applied file change.
- `changed` means no blocking finding and at least one applied correction,
  followed by live proof.
- `blocked` means any missing source or live coverage, failed Doctor, lost
  evidence, unresolved map drift, harness failure, or product gap remains.

The finding kind identifies the boundary: `doc-drift`, `harness-gap`,
`product-gap`, `doctor-failure`, `verified-unreachable`, or `blocked`.
