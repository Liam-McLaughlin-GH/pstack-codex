#!/usr/bin/env python3
"""Audit and safely update a project-local verification skill.

The helper consumes source-reader and live-pass receipts. It never runs a
command from a receipt, edits product files, or deletes evidence. Applying a
patch requires an explicit plan and a path inside the verification skill.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any, Iterable


LINK_PATTERN = re.compile(r"\[[^\]]+\]\(([^)\s]+)")
REQUIRED_SKILL_SECTIONS = ("Launch", "Doctor", "Drive", "Evidence", "Cleanup", "Helpers")
REQUIRED_FEATURE_SECTIONS = (
    "Sub-features",
    "How to get to it (user POV)",
    "Driving it with",
    "Gotchas",
)
ACTION_FIELDS = ("Actors", "Fixtures", "Action", "Expected result", "Evidence")
ACTION_HEADING_PATTERN = re.compile(r"^\s*(?:[-*+]\s+|\d+[.)]\s+)\*\*(.+?)\*\*\s*$")
ACTION_FIELD_PATTERN = re.compile(
    r"^\s*(?:(?:[-*+]\s+)|(?:\d+[.)]\s+))?(?:\*\*)?"
    r"(Actors|Fixtures|Action|Expected result|Evidence):"
    r"(?:\*\*)?\s*(.*?)\s*$"
)
AUTHORITATIVE_FIELDS = (
    "entry_points",
    "commands",
    "selectors",
    "fixtures",
    "expected_results",
)
LIVE_STATUSES = {"pass", "fail", "verified-unreachable", "harness-gap", "not-run"}
PATCH_KINDS = {"doc-drift", "harness-gap"}
RECEIPT_VERSION = 2
DOCTOR_METADATA_FIELDS = ("target", "build", "endpoint", "auth", "disposable")


class MaintainError(ValueError):
    """Raised when a receipt or safe filesystem boundary is invalid."""


def _mapping(value: Any, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise MaintainError(f"{label} must be an object")
    return value


def _text(value: Any, label: str, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str):
        raise MaintainError(f"{label} must be a string")
    if not allow_empty and not value.strip():
        raise MaintainError(f"{label} must not be empty")
    return value


def _text_list(value: Any, label: str, *, allow_empty: bool = True) -> list[str]:
    if not isinstance(value, list):
        raise MaintainError(f"{label} must be an array")
    if not allow_empty and not value:
        raise MaintainError(f"{label} must contain at least one item")
    return [_text(item, f"{label}[{index}]") for index, item in enumerate(value)]


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise MaintainError(f"could not read JSON {path}: {exc}") from exc
    return _mapping(value, str(path))


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _resolved(path: Path) -> Path:
    return path.expanduser().resolve(strict=False)


def _real_directory(path: Path, label: str) -> Path:
    path = path.expanduser()
    if path.is_symlink() or not path.is_dir():
        raise MaintainError(f"{label} must be a real directory: {path}")
    return path.resolve()


def _safe_path(root: Path, raw: str, label: str, *, require_exists: bool = False) -> Path:
    """Resolve a path and require it to stay below root without symlink hops."""

    if Path(raw).is_absolute():
        raise MaintainError(f"{label} must be relative: {raw}")
    root = _resolved(root)
    candidate = _resolved(root / raw)
    if not _is_relative_to(candidate, root) or candidate == root:
        raise MaintainError(f"{label} escapes its allowed root: {raw}")

    relative = candidate.relative_to(root)
    current = root
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            raise MaintainError(f"{label} crosses a symlink: {raw}")
    if require_exists and not candidate.exists():
        raise MaintainError(f"{label} does not exist: {raw}")
    return candidate


def _external_existing(raw: str, label: str, project_root: Path | None = None) -> Path:
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = (project_root or Path.cwd()) / path
    path = _resolved(path)
    if not path.is_file():
        raise MaintainError(f"{label} does not exist as a file: {raw}")
    return path


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex}"


def _metadata_present(value: Any) -> bool:
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, dict):
        return bool(value)
    return False


def _finding(
    kind: str,
    message: str,
    *,
    feature_id: str | None = None,
    path: str | None = None,
    blocking: bool = True,
    source: str | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "kind": kind,
        "message": message,
        "blocking": blocking,
    }
    if feature_id is not None:
        result["feature_id"] = feature_id
    if path is not None:
        result["path"] = path
    if source is not None:
        result["source"] = source
    return result


def _frontmatter_fields(text: str) -> dict[str, str]:
    if not text.startswith("---\n"):
        return {}
    end = text.find("\n---", 4)
    if end == -1:
        return {}
    fields: dict[str, str] = {}
    for line in text[4:end].splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        if key.strip() in {"name", "description"}:
            fields[key.strip()] = value.strip().strip("\"'")
    return fields


def _feature_sections(lines: Iterable[str]) -> list[str]:
    return [line[3:].strip() for line in lines if line.startswith("## ")]


def _h2_ranges(lines: list[str]) -> list[tuple[str, int, int]]:
    headings = [(index, line[3:].strip()) for index, line in enumerate(lines) if line.startswith("## ")]
    ranges: list[tuple[str, int, int]] = []
    for position, (start, title) in enumerate(headings):
        end = headings[position + 1][0] if position + 1 < len(headings) else len(lines)
        ranges.append((title, start, end))
    return ranges


def _action_blocks(lines: list[str]) -> tuple[list[str], list[tuple[str, list[str]]]]:
    """Return the driving preamble and bold action blocks from one feature."""

    driving = next((item for item in _h2_ranges(lines) if item[0].startswith("Driving it with")), None)
    if driving is None:
        return [], []
    _, start, end = driving
    section_lines = lines[start + 1 : end]
    action_starts = [
        (index, match.group(1).strip())
        for index, line in enumerate(section_lines)
        if (match := ACTION_HEADING_PATTERN.match(line))
    ]
    if not action_starts:
        return section_lines, []
    preamble = section_lines[: action_starts[0][0]]
    actions: list[tuple[str, list[str]]] = []
    for position, (action_start, title) in enumerate(action_starts):
        action_end = action_starts[position + 1][0] if position + 1 < len(action_starts) else len(section_lines)
        actions.append((title, section_lines[action_start + 1 : action_end]))
    return preamble, actions


def _validate_action_blocks(
    lines: list[str], feature_id: str, path: str
) -> list[dict[str, Any]]:
    preamble, actions = _action_blocks(lines)
    findings: list[dict[str, Any]] = []
    if not any(line.strip() == "Preconditions:" for line in preamble):
        findings.append(
            _finding(
                "doc-drift",
                "feature driving section must start with Preconditions:",
                feature_id=feature_id,
                path=path,
            )
        )
    if not actions:
        findings.append(
            _finding(
                "doc-drift",
                "feature driving section must contain at least one named action",
                feature_id=feature_id,
                path=path,
            )
        )
        return findings

    for title, block in actions:
        fields: list[tuple[str, str]] = []
        for line in block:
            match = ACTION_FIELD_PATTERN.match(line)
            if match:
                fields.append((match.group(1), match.group(2).strip()))
        names = [name for name, _ in fields]
        if names != list(ACTION_FIELDS):
            findings.append(
                _finding(
                    "doc-drift",
                    f"action {title!r} must contain {', '.join(ACTION_FIELDS)} in that order",
                    feature_id=feature_id,
                    path=path,
                )
            )
            continue
        for name, value in fields:
            if not value:
                findings.append(
                    _finding(
                        "doc-drift",
                        f"action {title!r} has an empty {name} field",
                        feature_id=feature_id,
                        path=path,
                    )
                )
    return findings


def _feature_regions(lines: list[str]) -> dict[str, str]:
    ranges = _h2_ranges(lines)
    regions: dict[str, str] = {}
    for title, start, end in ranges:
        regions[title] = "\n".join(lines[start + 1 : end])
    _, actions = _action_blocks(lines)
    regions["__actions__"] = "\n".join("\n".join(block) for _, block in actions)
    return regions


def validate_feature_map(skill_root: Path) -> tuple[list[str], list[dict[str, Any]]]:
    """Validate the issue #2 feature-map contract without changing files."""

    skill_root = _real_directory(skill_root, "skill root")
    findings: list[dict[str, Any]] = []

    skill_file = skill_root / "SKILL.md"
    features_root = skill_root / "features"
    index = features_root / "README.md"
    if not skill_file.is_file() or skill_file.is_symlink():
        findings.append(_finding("doc-drift", "verification skill is missing SKILL.md", path="SKILL.md"))
    else:
        skill_text = skill_file.read_text(encoding="utf-8")
        fields = _frontmatter_fields(skill_text)
        for field in ("name", "description"):
            if not fields.get(field):
                findings.append(_finding("doc-drift", f"SKILL.md frontmatter is missing {field}", path="SKILL.md"))
        sections = _feature_sections(skill_text.splitlines())
        missing = [section for section in REQUIRED_SKILL_SECTIONS if section not in sections]
        if missing:
            findings.append(
                _finding(
                    "doc-drift",
                    "verification skill is missing required section(s): " + ", ".join(missing),
                    path="SKILL.md",
                )
            )

    if not features_root.is_dir() or features_root.is_symlink():
        findings.append(_finding("doc-drift", "verification skill is missing a real features directory", path="features"))
        return [], findings
    if not index.is_file() or index.is_symlink():
        findings.append(_finding("doc-drift", "feature map is missing features/README.md", path="features/README.md"))
        return [], findings

    feature_files = sorted(path for path in features_root.glob("*.md") if path.name != "README.md")
    feature_ids = [path.stem for path in feature_files]
    index_text = index.read_text(encoding="utf-8")
    linked: list[str] = []
    for raw_target in LINK_PATTERN.findall(index_text):
        if re.match(r"^[A-Za-z][A-Za-z0-9+.-]*:", raw_target) or raw_target.startswith("#"):
            continue
        target = raw_target.split("#", 1)[0]
        try:
            target_path = _safe_path(features_root, target, "feature-map link")
        except MaintainError as exc:
            findings.append(_finding("doc-drift", str(exc), path="features/README.md"))
            continue
        relative = target_path.relative_to(features_root).as_posix()
        if relative in linked:
            findings.append(_finding("doc-drift", f"feature map links {relative} more than once", path="features/README.md"))
        linked.append(relative)
        if not target_path.is_file():
            findings.append(_finding("doc-drift", f"feature map link target does not exist: {target}", path="features/README.md"))

    expected_links = {f"{feature_id}.md" for feature_id in feature_ids}
    linked_set = set(linked)
    for missing in sorted(expected_links - linked_set):
        findings.append(_finding("doc-drift", f"feature file is not linked from the index: {missing}", path="features/README.md"))
    for extra in sorted(linked_set - expected_links):
        findings.append(_finding("doc-drift", f"index contains a non-feature entry: {extra}", path="features/README.md"))

    for path in feature_files:
        relative = path.relative_to(skill_root).as_posix()
        if path.is_symlink():
            findings.append(_finding("doc-drift", "feature file must not be a symlink", feature_id=path.stem, path=relative))
            continue
        if not path.is_file():
            findings.append(_finding("doc-drift", "feature entry must be a regular file", feature_id=path.stem, path=relative))
            continue
        lines = path.read_text(encoding="utf-8").splitlines()
        h1 = [line for line in lines if line.startswith("# ")]
        if len(h1) != 1:
            findings.append(_finding("doc-drift", "feature file must contain exactly one H1", feature_id=path.stem, path=relative))
        sections = _feature_sections(lines)
        if len(sections) != 4 or sections[:2] != list(REQUIRED_FEATURE_SECTIONS[:2]) or not sections[2].startswith(REQUIRED_FEATURE_SECTIONS[2]) or sections[3] != REQUIRED_FEATURE_SECTIONS[3]:
            findings.append(_finding("doc-drift", "feature file has the wrong four-section contract", feature_id=path.stem, path=relative))
        findings.extend(_validate_action_blocks(lines, path.stem, relative))

    return feature_ids, findings


def _source_entries(reader: dict[str, Any], label: str) -> list[dict[str, str]]:
    raw_entries = reader.get("source_entries")
    if not isinstance(raw_entries, list) or not raw_entries:
        raise MaintainError(f"{label}.source_entries must contain at least one entry")
    entries: list[dict[str, str]] = []
    for index, raw in enumerate(raw_entries):
        entry = _mapping(raw, f"{label}.source_entries[{index}]")
        entry_label = f"{label}.source_entries[{index}]"
        path = _text(entry.get("path"), f"{entry_label}.path")
        lines = entry.get("lines", "")
        symbol = entry.get("symbol", "")
        if lines is None:
            lines = ""
        if symbol is None:
            symbol = ""
        lines = _text(lines, f"{entry_label}.lines", allow_empty=True)
        symbol = _text(symbol, f"{entry_label}.symbol", allow_empty=True)
        if not lines.strip() and not symbol.strip():
            raise MaintainError(f"{entry_label} must include a non-empty lines or symbol citation")
        normalized = {"path": path, "lines": lines, "symbol": symbol}
        entries.append(normalized)
    return entries


def _source_reference(entry: dict[str, str]) -> str:
    citation = entry.get("lines", "").strip() or entry.get("symbol", "").strip()
    return f"{entry['path']}:{citation}"


def _validate_reader_report(
    report: dict[str, Any], project_root: Path, feature_ids: list[str]
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    readers = report.get("readers")
    if not isinstance(readers, list):
        raise MaintainError("reader report must contain a readers array")
    expected = set(feature_ids)
    by_id: dict[str, dict[str, Any]] = {}
    findings: list[dict[str, Any]] = []
    for index, raw_reader in enumerate(readers):
        label = f"readers[{index}]"
        reader = _mapping(raw_reader, label)
        feature_id = _text(reader.get("feature_id"), f"{label}.feature_id")
        if feature_id not in expected:
            findings.append(_finding("blocked", f"source reader returned an unknown feature: {feature_id}", feature_id=feature_id))
            continue
        if feature_id in by_id:
            findings.append(_finding("blocked", f"more than one source reader returned {feature_id}", feature_id=feature_id))
            continue
        status = _text(reader.get("status"), f"{label}.status").lower()
        if status not in {"complete", "blocked"}:
            raise MaintainError(f"{label}.status must be complete or blocked")
        reader["status"] = status
        reader["summary"] = _text(reader.get("summary"), f"{label}.summary")
        if status == "blocked":
            reader["reason"] = _text(reader.get("reason"), f"{label}.reason")
            by_id[feature_id] = reader
            findings.append(_finding("blocked", f"source reader is blocked: {reader['reason']}", feature_id=feature_id))
            continue
        entries = _source_entries(reader, label)
        reader["source_entries"] = entries
        for entry in entries:
            try:
                source_parts = Path(entry["path"]).parts
                if any(part in {"", ".", ".."} for part in source_parts):
                    raise MaintainError(f"source entry must be a repository-relative path: {entry['path']}")
                source_path = _safe_path(project_root, entry["path"], "source entry", require_exists=True)
                if not source_path.is_file() or source_path.is_symlink():
                    raise MaintainError(f"source entry must be a regular file: {entry['path']}")
            except MaintainError as exc:
                findings.append(_finding("blocked", str(exc), feature_id=feature_id, source=_source_reference(entry)))
        authoritative = _mapping(reader.get("authoritative", {}), f"{label}.authoritative")
        normalized: dict[str, list[str]] = {}
        for field in AUTHORITATIVE_FIELDS:
            normalized[field] = _text_list(authoritative.get(field, []), f"{label}.authoritative.{field}")
        reader["authoritative"] = normalized
        harness = _mapping(reader.get("harness", {}), f"{label}.harness")
        harness_status = _text(harness.get("status"), f"{label}.harness.status").lower()
        if harness_status not in {"available", "missing"}:
            raise MaintainError(f"{label}.harness.status must be available or missing")
        if harness_status == "missing":
            findings.append(
                _finding(
                    "harness-gap",
                    f"source reader reports no usable harness: {_text(harness.get('reason'), f'{label}.harness.reason')}",
                    feature_id=feature_id,
                )
            )
        reader["harness"] = harness
        live_recipe = _mapping(reader.get("live_recipe"), f"{label}.live_recipe")
        for field in ("actor", "fixture", "action", "expected_result", "evidence"):
            _text(live_recipe.get(field), f"{label}.live_recipe.{field}")
        reader["live_recipe"] = live_recipe
        by_id[feature_id] = reader

    for feature_id in sorted(expected - set(by_id)):
        findings.append(_finding("blocked", "no source reader returned a summary for this feature", feature_id=feature_id))
    return by_id, findings


def _normalize_live_status(value: Any, label: str) -> str:
    status = _text(value, label).lower().replace("_", "-").replace(" ", "-")
    if status not in LIVE_STATUSES:
        raise MaintainError(f"{label} must be one of {sorted(LIVE_STATUSES)}")
    return status


def _doctor_value(report: dict[str, Any], field: str) -> Any:
    aliases = {
        "target": ("target", "target_root"),
        "build": ("build", "build_id"),
        "endpoint": ("endpoint", "endpoint_url"),
        "auth": ("auth", "auth_state"),
        "disposable": ("disposable", "disposable_state"),
    }
    for key in aliases[field]:
        if key in report:
            return report[key]
    return None


def _doctor_validation(skill_root: Path, report: dict[str, Any]) -> dict[str, Any]:
    """Validate the one Doctor contract used by both audit and doctor."""

    checks: list[dict[str, Any]] = []
    checks.append({"name": "skill-root", "pass": skill_root.is_dir() and not skill_root.is_symlink()})

    reported_root = report.get("skill_root")
    root_matches = False
    if isinstance(reported_root, str) and reported_root.strip():
        root_matches = _resolved(Path(reported_root)) == skill_root
    checks.append({"name": "reported-skill-root", "pass": root_matches})
    checks.append({"name": "doctor-status", "pass": report.get("status") == "pass"})
    checks.append({"name": "owned-instance", "pass": report.get("owned") is True})
    checks.append({"name": "healthy-instance", "pass": report.get("healthy") is True})
    checks.append(
        {
            "name": "session-id",
            "pass": isinstance(report.get("session_id"), str) and bool(report["session_id"].strip()),
        }
    )
    for field in DOCTOR_METADATA_FIELDS:
        value = _doctor_value(report, field)
        passed = value is True if field == "disposable" else _metadata_present(value)
        checks.append({"name": f"{field}-metadata", "pass": passed})

    status = "pass" if all(check["pass"] for check in checks) else "fail"
    return {"status": status, "checks": checks}


def _doctor_findings(validation: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        _finding("doctor-failure", f"Doctor check failed: {check['name']}", path="SKILL.md")
        for check in validation["checks"]
        if check["pass"] is not True
    ]


def _report_context(report: dict[str, Any]) -> dict[str, Any]:
    run = report.get("run", {})
    if not isinstance(run, dict):
        run = {}
    context: dict[str, Any] = {}
    for field in ("run_id", "session_id", "phase", "apply_id"):
        value = report.get(field)
        if value is None:
            value = run.get(field)
        context[field] = value
    return context


def _context_findings(
    report: dict[str, Any], doctor: dict[str, Any], change_receipt: dict[str, Any] | None
) -> list[dict[str, Any]]:
    context = _report_context(report)
    findings: list[dict[str, Any]] = []
    for field in ("run_id", "session_id", "phase"):
        value = context.get(field)
        if not isinstance(value, str) or not value.strip():
            kind = "doctor-failure" if field == "session_id" else "evidence-failure"
            findings.append(_finding(kind, f"live report is missing run metadata: {field}", path="SKILL.md"))
    if context.get("phase") not in {"baseline", "post-apply"}:
        findings.append(_finding("evidence-failure", "live report phase must be baseline or post-apply", path="SKILL.md"))

    doctor_session = doctor.get("session_id")
    if isinstance(doctor_session, str) and context.get("session_id") != doctor_session:
        findings.append(_finding("doctor-failure", "live report session does not match the Doctor session", path="SKILL.md"))

    if change_receipt is not None:
        for field in ("run_id", "session_id", "apply_id"):
            expected = change_receipt.get(field)
            actual = context.get(field)
            if actual != expected:
                kind = "doctor-failure" if field == "session_id" else "evidence-failure"
                findings.append(
                    _finding(
                        kind,
                        f"post-apply live receipt {field} does not match the apply receipt",
                        path="SKILL.md",
                    )
                )
        if context.get("phase") != "post-apply":
            findings.append(_finding("evidence-failure", "changed proof requires a post-apply live receipt", path="SKILL.md"))
    return findings


def _evidence_for_result(
    result: dict[str, Any], label: str, project_root: Path, feature_id: str, findings: list[dict[str, Any]]
) -> bool:
    try:
        evidence = _text_list(result.get("evidence"), f"{label}.evidence", allow_empty=False)
    except MaintainError as exc:
        findings.append(_finding("evidence-failure", str(exc), feature_id=feature_id))
        return False
    evidence_ok = True
    for raw_path in evidence:
        try:
            _external_existing(raw_path, "evidence artifact", project_root)
        except MaintainError as exc:
            evidence_ok = False
            findings.append(_finding("evidence-failure", str(exc), feature_id=feature_id))
    if result.get("evidence_survives_cleanup") is not True:
        evidence_ok = False
        findings.append(
            _finding(
                "evidence-failure",
                "live proof did not confirm evidence survives cleanup",
                feature_id=feature_id,
            )
        )
    return evidence_ok


def _post_apply_feature_findings(
    result: dict[str, Any], feature_id: str, change_receipt: dict[str, Any] | None
) -> list[dict[str, Any]]:
    if change_receipt is None or feature_id not in change_receipt.get("affected_features", []):
        return []
    findings: list[dict[str, Any]] = []
    binding = result.get("post_apply")
    if binding is None:
        binding = result.get("re_drive")
    if not isinstance(binding, dict):
        binding = {}

    redriven = binding.get("redriven")
    if redriven is None:
        redriven = binding.get("re_driven")
    if redriven is None:
        redriven = result.get("redriven_after_apply")
    if redriven is not True:
        findings.append(
            _finding(
                "evidence-failure",
                "affected feature was not explicitly re-driven after the correction",
                feature_id=feature_id,
            )
        )

    for field in ("apply_id", "run_id", "session_id"):
        actual = binding.get(field, result.get(field))
        if actual != change_receipt.get(field):
            findings.append(
                _finding(
                    "evidence-failure",
                    f"affected feature post-apply receipt does not match {field}",
                    feature_id=feature_id,
                )
            )
    return findings


def _validate_live_report(
    report: dict[str, Any],
    project_root: Path,
    skill_root: Path,
    feature_ids: list[str],
    change_receipt: dict[str, Any] | None = None,
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    findings: list[dict[str, Any]] = []
    raw_doctor = report.get("doctor")
    if isinstance(raw_doctor, dict):
        doctor = raw_doctor
    else:
        doctor = {}
        findings.append(_finding("doctor-failure", "live report is missing a Doctor receipt", path="SKILL.md"))
    doctor_validation = _doctor_validation(skill_root, doctor)
    findings.extend(_doctor_findings(doctor_validation))
    findings.extend(_context_findings(report, doctor, change_receipt))

    raw_features = report.get("features")
    if not isinstance(raw_features, list):
        raise MaintainError("live report must contain a features array")
    expected = set(feature_ids)
    by_id: dict[str, dict[str, Any]] = {}
    for index, raw_result in enumerate(raw_features):
        label = f"features[{index}]"
        result = _mapping(raw_result, label)
        feature_id = _text(result.get("feature_id"), f"{label}.feature_id")
        if feature_id not in expected:
            findings.append(_finding("blocked", f"live report returned an unknown feature: {feature_id}", feature_id=feature_id))
            continue
        if feature_id in by_id:
            findings.append(_finding("blocked", f"live report returned {feature_id} more than once", feature_id=feature_id))
            continue
        status = _normalize_live_status(result.get("status"), f"{label}.status")
        result["status"] = status
        findings.extend(_post_apply_feature_findings(result, feature_id, change_receipt))
        if status == "pass":
            _evidence_for_result(result, label, project_root, feature_id, findings)
            if result.get("source_match") is not True:
                findings.append(
                    _finding(
                        "evidence-failure",
                        "live pass did not prove source/map agreement",
                        feature_id=feature_id,
                    )
                )
        elif status == "fail":
            route_ok = True
            for field in ("route", "observed"):
                try:
                    _text(result.get(field), f"{label}.{field}")
                except MaintainError as exc:
                    route_ok = False
                    findings.append(_finding("evidence-failure", str(exc), feature_id=feature_id))
            evidence_ok = _evidence_for_result(result, label, project_root, feature_id, findings)
            doctor_healthy = doctor_validation["status"] == "pass"
            if result.get("source_match") is True and doctor_healthy and evidence_ok:
                findings.append(_finding("product-gap", "live behavior failed after source and map agreement", feature_id=feature_id))
            elif result.get("source_match") is not True and route_ok:
                findings.append(
                    _finding(
                        "evidence-failure",
                        "live failure did not include source/map agreement proof",
                        feature_id=feature_id,
                    )
                )
        elif status == "harness-gap":
            try:
                route = _text(result.get("route"), f"{label}.route")
                reason = _text(result.get("reason"), f"{label}.reason")
            except MaintainError as exc:
                findings.append(_finding("harness-gap", str(exc), feature_id=feature_id))
            else:
                findings.append(_finding("harness-gap", f"{route}: {reason}", feature_id=feature_id))
        elif status == "verified-unreachable":
            try:
                _text(result.get("route"), f"{label}.route")
                prerequisite = _text(result.get("prerequisite"), f"{label}.prerequisite")
            except MaintainError as exc:
                findings.append(_finding("evidence-failure", str(exc), feature_id=feature_id))
                by_id[feature_id] = result
                continue
            feature_path = skill_root / "features" / f"{feature_id}.md"
            if feature_path.is_symlink() or not feature_path.is_file():
                findings.append(_finding("blocked", "feature file disappeared before the live receipt was reconciled", feature_id=feature_id))
            elif prerequisite not in feature_path.read_text(encoding="utf-8"):
                findings.append(
                    _finding(
                        "doc-drift",
                        "the feature map does not record the unreachable prerequisite",
                        feature_id=feature_id,
                        path=f"features/{feature_id}.md",
                    )
                )
            findings.append(
                _finding(
                    "verified-unreachable",
                    "feature route was recorded with an unmet prerequisite",
                    feature_id=feature_id,
                )
            )
        else:
            try:
                reason = _text(result.get("reason"), f"{label}.reason")
            except MaintainError as exc:
                reason = str(exc)
            findings.append(_finding("blocked", reason, feature_id=feature_id))
        by_id[feature_id] = result

    for feature_id in sorted(expected - set(by_id)):
        findings.append(_finding("blocked", "no live result was recorded for this feature", feature_id=feature_id))
    return by_id, findings


def _validate_change_receipt(
    path: Path | None, skill_root: Path, feature_ids: list[str]
) -> tuple[dict[str, Any] | None, list[str], bool, list[dict[str, Any]]]:
    """Validate the receipt emitted by apply before it can produce changed."""

    if path is None:
        return None, [], False, []
    receipt = _load_json(path)
    findings: list[dict[str, Any]] = []

    if receipt.get("receipt_version") != RECEIPT_VERSION:
        findings.append(_finding("evidence-failure", "change receipt has an unsupported receipt_version"))
    if receipt.get("outcome") != "changed":
        findings.append(_finding("evidence-failure", "change receipt does not record a changed apply"))
    for field in ("apply_id", "run_id", "session_id"):
        if not isinstance(receipt.get(field), str) or not receipt[field].strip():
            findings.append(_finding("evidence-failure", f"change receipt is missing {field}"))

    reported_root = receipt.get("skill_root")
    if not isinstance(reported_root, str) or _resolved(Path(reported_root)) != skill_root:
        findings.append(_finding("evidence-failure", "change receipt names a different verification skill"))

    try:
        changed_files = _text_list(receipt.get("changed_files"), "change receipt changed_files", allow_empty=False)
    except MaintainError as exc:
        findings.append(_finding("evidence-failure", str(exc)))
        changed_files = []
    for relative in changed_files:
        try:
            changed_path = _safe_path(skill_root, relative, "change receipt path", require_exists=True)
            if changed_path.is_symlink() or not changed_path.is_file():
                raise MaintainError(f"change receipt path must be a regular file: {relative}")
        except MaintainError as exc:
            findings.append(_finding("evidence-failure", str(exc)))

    try:
        affected_features = _text_list(
            receipt.get("affected_features"), "change receipt affected_features", allow_empty=False
        )
    except MaintainError as exc:
        findings.append(_finding("evidence-failure", str(exc)))
        affected_features = []
    for feature_id in affected_features:
        if feature_id not in feature_ids:
            findings.append(_finding("evidence-failure", f"change receipt names unknown feature {feature_id}", feature_id=feature_id))

    raw_changes = receipt.get("changes")
    if not isinstance(raw_changes, list) or not raw_changes:
        findings.append(_finding("evidence-failure", "change receipt must contain changes"))
    else:
        change_paths: list[str] = []
        change_feature_ids: set[str] = set()
        for index, raw_change in enumerate(raw_changes):
            label = f"change receipt changes[{index}]"
            if not isinstance(raw_change, dict):
                findings.append(_finding("evidence-failure", f"{label} must be an object"))
                continue
            try:
                change_path = _text(raw_change.get("path"), f"{label}.path")
            except MaintainError as exc:
                findings.append(_finding("evidence-failure", str(exc)))
                continue
            if raw_change.get("kind") not in PATCH_KINDS:
                findings.append(_finding("evidence-failure", f"{label}.kind is not an applicable correction kind"))
            if change_path not in changed_files:
                findings.append(_finding("evidence-failure", f"{label}.path is not in changed_files"))
            change_paths.append(change_path)
            try:
                change_features = _text_list(
                    raw_change.get("feature_ids"), f"{label}.feature_ids", allow_empty=False
                )
            except MaintainError as exc:
                findings.append(_finding("evidence-failure", str(exc)))
                continue
            if not set(change_features).intersection(affected_features):
                findings.append(_finding("evidence-failure", f"{label} does not identify an affected feature"))
            change_feature_ids.update(change_features)
        if set(change_paths) != set(changed_files):
            findings.append(_finding("evidence-failure", "change receipt changes do not cover changed_files"))
        if change_feature_ids != set(affected_features):
            findings.append(_finding("evidence-failure", "change receipt affected_features do not match its changes"))

    raw_digests = receipt.get("file_digests")
    if not isinstance(raw_digests, dict):
        findings.append(_finding("evidence-failure", "change receipt must contain file_digests"))
    else:
        for relative in changed_files:
            digest = raw_digests.get(relative)
            if not isinstance(digest, str) or not digest.strip():
                findings.append(_finding("evidence-failure", f"change receipt is missing the digest for {relative}"))
                continue
            try:
                changed_path = _safe_path(skill_root, relative, "change receipt path", require_exists=True)
                if _sha256(changed_path) != digest:
                    findings.append(_finding("evidence-failure", f"changed file does not match its apply receipt: {relative}"))
            except (MaintainError, OSError) as exc:
                findings.append(_finding("evidence-failure", str(exc)))

    return receipt, changed_files, not findings, findings


def _load_change_log(path: Path | None, skill_root: Path) -> list[str]:
    """Compatibility wrapper for callers that only need the changed paths."""

    if path is None:
        return []
    raw = _load_json(path)
    affected = raw.get("affected_features", [])
    feature_ids = _text_list(affected, "change receipt affected_features", allow_empty=True)
    _, changed_files, valid, findings = _validate_change_receipt(path, skill_root, feature_ids)
    if not valid:
        message = findings[0]["message"] if findings else "change receipt is invalid"
        raise MaintainError(message)
    return changed_files


def audit(
    skill_root: Path,
    project_root: Path,
    reader_report_path: Path,
    live_report_path: Path,
    change_log_path: Path | None = None,
) -> dict[str, Any]:
    """Return a source/live classification without writing to the project."""

    skill_root = _real_directory(skill_root, "skill root")
    project_root = _real_directory(project_root, "project root")
    feature_ids, map_findings = validate_feature_map(skill_root)
    reader_report = _load_json(reader_report_path)
    live_report = _load_json(live_report_path)
    readers, reader_findings = _validate_reader_report(reader_report, project_root, feature_ids)
    for feature_id, reader in readers.items():
        if reader.get("status") != "complete":
            continue
        feature_path = skill_root / "features" / f"{feature_id}.md"
        if not feature_path.is_file() or feature_path.is_symlink():
            continue
        regions = _feature_regions(feature_path.read_text(encoding="utf-8").splitlines())
        for field, values in reader["authoritative"].items():
            region = regions.get("How to get to it (user POV)", "") if field == "entry_points" else regions.get("__actions__", "")
            for value in values:
                if value not in region:
                    source = ", ".join(_source_reference(entry) for entry in reader["source_entries"])
                    reader_findings.append(
                        _finding(
                            "doc-drift",
                            f"{field} from the source reader is absent from the action recipe: {value}",
                            feature_id=feature_id,
                            path=f"features/{feature_id}.md",
                            source=source,
                        )
                    )
    change_receipt, changed_files, receipt_valid, receipt_findings = _validate_change_receipt(
        change_log_path, skill_root, feature_ids
    )
    _, live_findings = _validate_live_report(
        live_report, project_root, skill_root, feature_ids, change_receipt if change_log_path else None
    )
    findings = map_findings + reader_findings + receipt_findings + live_findings
    blocking = [item for item in findings if item.get("blocking") is True]
    if blocking:
        outcome = "blocked"
    elif change_log_path is not None and receipt_valid and changed_files:
        outcome = "changed"
    else:
        outcome = "clean"
    return {
        "outcome": outcome,
        "target": str(skill_root),
        "features": feature_ids,
        "changed_files": changed_files,
        "findings": findings,
        "source_readers": len(readers),
        "live_results": len(_mapping(live_report, "live report").get("features", [])),
    }


def _atomic_write(path: Path, text: str) -> None:
    temporary: str | None = None
    mode = path.stat().st_mode
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
            temporary = handle.name
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass


def apply_patch_plan(
    skill_root: Path,
    patch_plan_path: Path,
    *,
    run_id: str | None = None,
    session_id: str | None = None,
) -> dict[str, Any]:
    """Apply only explicit doc or harness replacements below skill_root."""

    skill_root = _real_directory(skill_root, "skill root")
    plan = _load_json(patch_plan_path)
    changes = plan.get("changes")
    if not isinstance(changes, list) or not changes:
        raise MaintainError("patch plan must contain at least one change")

    originals: dict[Path, str] = {}
    proposed: dict[Path, str] = {}
    metadata: list[dict[str, Any]] = []
    plan_features = plan.get("affected_features")
    if plan_features is None:
        plan_features = plan.get("feature_ids")
    if plan_features is not None:
        plan_features = _text_list(plan_features, "patch plan affected_features", allow_empty=False)
    for index, raw_change in enumerate(changes):
        label = f"changes[{index}]"
        change = _mapping(raw_change, label)
        kind = _text(change.get("kind", "doc-drift"), f"{label}.kind")
        if kind not in PATCH_KINDS:
            raise MaintainError(f"{label}.kind cannot apply {kind}; product gaps stay out of this skill")
        raw_path = _text(change.get("path"), f"{label}.path")
        if Path(raw_path).is_absolute():
            raise MaintainError(f"{label}.path must be relative to the verification skill")
        path = _safe_path(skill_root, raw_path, f"{label}.path", require_exists=True)
        if path.is_symlink() or not path.is_file():
            raise MaintainError(f"{label}.path must be a regular file inside the verification skill")
        old = _text(change.get("old"), f"{label}.old")
        new = _text(change.get("new"), f"{label}.new", allow_empty=True)
        _text(change.get("reason"), f"{label}.reason")
        _text(change.get("source"), f"{label}.source")
        if old == new:
            raise MaintainError(f"{label} does not change its target")
        change_features = change.get("feature_ids")
        if change_features is None:
            change_features = change.get("affected_features")
        if change_features is None:
            change_features = plan_features
        if change_features is None and raw_path.startswith("features/") and raw_path.endswith(".md"):
            change_features = [Path(raw_path).stem]
        change_features = _text_list(change_features, f"{label}.feature_ids", allow_empty=False)
        if path not in originals:
            originals[path] = path.read_text(encoding="utf-8")
            proposed[path] = originals[path]
        count = proposed[path].count(old)
        if count != 1:
            raise MaintainError(f"{label}.old must occur exactly once in {raw_path}, found {count}")
        proposed[path] = proposed[path].replace(old, new, 1)
        metadata.append({"path": raw_path, "kind": kind, "feature_ids": change_features})

    affected_features = sorted({feature_id for change in metadata for feature_id in change["feature_ids"]})
    if not affected_features:
        raise MaintainError("patch plan must identify at least one affected feature")
    apply_id = _new_id("apply")
    run_id = run_id or plan.get("run_id") or _new_id("run")
    session_id = session_id or plan.get("session_id") or _new_id("session")
    run_id = _text(run_id, "apply run_id")
    session_id = _text(session_id, "apply session_id")

    written: list[Path] = []
    try:
        for path, text in proposed.items():
            _atomic_write(path, text)
            written.append(path)
    except Exception:
        for path in written:
            _atomic_write(path, originals[path])
        raise
    changed_files = [path.relative_to(skill_root).as_posix() for path in proposed]
    return {
        "receipt_version": RECEIPT_VERSION,
        "outcome": "changed",
        "apply_id": apply_id,
        "run_id": run_id,
        "session_id": session_id,
        "skill_root": str(skill_root),
        "affected_features": affected_features,
        "changed_files": changed_files,
        "file_digests": {relative: _sha256(skill_root / relative) for relative in changed_files},
        "changes": metadata,
    }


def doctor(skill_root: Path, report_path: Path) -> dict[str, Any]:
    """Validate an already-run app Doctor receipt without probing the app."""

    skill_root = _real_directory(skill_root, "skill root")
    report = _load_json(report_path)
    return _doctor_validation(skill_root, report)


def _cleanup_path(run_root: Path, raw: str) -> Path:
    path = Path(raw)
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise MaintainError(f"owned cleanup path must be a simple relative path: {raw}")
    return _safe_path(run_root, raw, "owned cleanup path")


def _path_overlaps(left: Path, right: Path) -> bool:
    return _is_relative_to(left, right) or _is_relative_to(right, left)


def _cleanup_protected_roots(project_root: Path, skill_root: Path | None) -> list[Path]:
    roots = [project_root]
    if skill_root is not None:
        roots.append(skill_root)
    for relative in (".agents/skills", ".cursor/skills", ".codex/skills"):
        candidate = project_root / relative
        if candidate.is_dir() and not candidate.is_symlink():
            roots.append(candidate.resolve())
    return roots


def cleanup(
    run_root: Path,
    manifest_path: Path,
    project_root: Path | None = None,
    skill_root: Path | None = None,
) -> dict[str, Any]:
    """Remove only owned scratch paths and verify evidence remains."""

    run_root = _real_directory(run_root, "run root")
    if project_root is None:
        raise MaintainError("cleanup requires --project-root to protect product files")
    project_root = _real_directory(project_root, "project root")
    if skill_root is not None:
        skill_root = _real_directory(skill_root, "skill root")
    protected_roots = _cleanup_protected_roots(project_root, skill_root)
    if any(_is_relative_to(run_root, protected) for protected in protected_roots):
        raise MaintainError("run root must be outside the project and verification roots")

    manifest_path = manifest_path.expanduser()
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise MaintainError(f"cleanup manifest must be a regular file: {manifest_path}")
    manifest_path = manifest_path.resolve()
    if not _is_relative_to(manifest_path, run_root):
        raise MaintainError("cleanup manifest must be inside the disposable run root")
    manifest = _load_json(manifest_path)
    owned = _text_list(manifest.get("owned_paths"), "owned_paths", allow_empty=True)
    evidence = _text_list(manifest.get("evidence_paths"), "evidence_paths", allow_empty=False)
    owned_paths = [_cleanup_path(run_root, raw) for raw in owned]
    evidence_paths = [_external_existing(raw, "evidence artifact", project_root) for raw in evidence]
    for index, path in enumerate(owned_paths):
        if any(_path_overlaps(path, protected) for protected in protected_roots):
            raise MaintainError(f"owned cleanup target is inside or contains a protected root: {owned[index]}")
        if any(index != other and _path_overlaps(path, other_path) for other, other_path in enumerate(owned_paths)):
            raise MaintainError(f"owned cleanup targets overlap: {owned[index]}")
        if path == manifest_path or _is_relative_to(manifest_path, path):
            raise MaintainError(f"owned cleanup target would remove its manifest: {owned[index]}")
        if (os.path.lexists(path) and not (path.is_file() or path.is_dir())) or path.is_symlink():
            raise MaintainError(f"owned cleanup target is not a regular file or directory: {owned[index]}")
    for evidence_path in evidence_paths:
        if any(evidence_path == owned_path or _is_relative_to(evidence_path, owned_path) for owned_path in owned_paths):
            raise MaintainError(f"cleanup would remove evidence: {evidence_path}")

    removed: list[str] = []
    for path in owned_paths:
        if not os.path.lexists(path):
            continue
        if path.is_file():
            path.unlink()
        elif path.is_dir():
            shutil.rmtree(path)
        else:
            raise MaintainError(f"unsupported owned cleanup path: {path}")
        removed.append(path.relative_to(run_root).as_posix())
    missing = [str(path) for path in evidence_paths if not path.is_file()]
    if missing:
        raise MaintainError("evidence disappeared during cleanup: " + ", ".join(missing))
    return {"outcome": "cleaned", "removed": removed, "evidence_preserved": [str(path) for path in evidence_paths]}


def _print_payload(payload: dict[str, Any], as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return
    if "outcome" in payload:
        print(f"{payload['outcome']}")
    for finding in payload.get("findings", []):
        feature = f" [{finding['feature_id']}]" if finding.get("feature_id") else ""
        print(f"{finding['kind']}{feature}: {finding['message']}")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    audit_parser = subparsers.add_parser("audit", help="classify source and live verification evidence")
    audit_parser.add_argument("--skill-root", required=True)
    audit_parser.add_argument("--project-root", required=True)
    audit_parser.add_argument("--reader-report", required=True)
    audit_parser.add_argument("--live-report", required=True)
    audit_parser.add_argument("--change-log")
    audit_parser.add_argument("--json", action="store_true")

    apply_parser = subparsers.add_parser("apply", help="apply an explicit safe patch plan")
    apply_parser.add_argument("--skill-root", required=True)
    apply_parser.add_argument("--patch-plan", required=True)
    apply_parser.add_argument("--run-id")
    apply_parser.add_argument("--session-id")
    apply_parser.add_argument("--json", action="store_true")

    doctor_parser = subparsers.add_parser("doctor", help="validate an owned, healthy Doctor receipt")
    doctor_parser.add_argument("--skill-root", required=True)
    doctor_parser.add_argument("--report", required=True)
    doctor_parser.add_argument("--json", action="store_true")

    cleanup_parser = subparsers.add_parser("cleanup", help="remove owned scratch paths and retain evidence")
    cleanup_parser.add_argument("--run-root", required=True)
    cleanup_parser.add_argument("--manifest", required=True)
    cleanup_parser.add_argument("--project-root", required=True)
    cleanup_parser.add_argument("--skill-root")
    cleanup_parser.add_argument("--json", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        if args.command == "audit":
            payload = audit(
                Path(args.skill_root),
                Path(args.project_root),
                Path(args.reader_report),
                Path(args.live_report),
                Path(args.change_log) if args.change_log else None,
            )
            _print_payload(payload, args.json)
            return 1 if payload["outcome"] == "blocked" else 0
        if args.command == "apply":
            payload = apply_patch_plan(
                Path(args.skill_root),
                Path(args.patch_plan),
                run_id=args.run_id,
                session_id=args.session_id,
            )
            _print_payload(payload, args.json)
            return 0
        if args.command == "doctor":
            payload = doctor(Path(args.skill_root), Path(args.report))
            _print_payload(payload, args.json)
            return 0 if payload["status"] == "pass" else 1
        payload = cleanup(
            Path(args.run_root),
            Path(args.manifest),
            Path(args.project_root),
            Path(args.skill_root) if args.skill_root else None,
        )
        _print_payload(payload, args.json)
        return 0
    except (MaintainError, OSError) as exc:
        print(f"maintain-verification-skill error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
