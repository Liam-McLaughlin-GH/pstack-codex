#!/usr/bin/env python3
"""Audit and safely update a project-local verification skill.

The helper consumes source-reader and live-pass receipts. It never runs a
command from a receipt, edits product files, or deletes evidence. Applying a
patch requires an explicit plan and a path inside the verification skill.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import tempfile
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
AUTHORITATIVE_FIELDS = (
    "entry_points",
    "commands",
    "selectors",
    "fixtures",
    "expected_results",
)
LIVE_STATUSES = {"pass", "fail", "verified-unreachable", "harness-gap", "not-run"}
PATCH_KINDS = {"doc-drift", "harness-gap"}


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


def _has_label(lines: Iterable[str], label: str) -> bool:
    return any(line.strip().lstrip("- ").startswith(label) for line in lines)


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
        lines = path.read_text(encoding="utf-8").splitlines()
        h1 = [line for line in lines if line.startswith("# ")]
        if len(h1) != 1:
            findings.append(_finding("doc-drift", "feature file must contain exactly one H1", feature_id=path.stem, path=relative))
        sections = _feature_sections(lines)
        if len(sections) != 4 or sections[:2] != list(REQUIRED_FEATURE_SECTIONS[:2]) or not sections[2].startswith(REQUIRED_FEATURE_SECTIONS[2]) or sections[3] != REQUIRED_FEATURE_SECTIONS[3]:
            findings.append(_finding("doc-drift", "feature file has the wrong four-section contract", feature_id=path.stem, path=relative))
        if "Preconditions:" not in lines:
            findings.append(_finding("doc-drift", "feature driving section must start with Preconditions:", feature_id=path.stem, path=relative))
        for label in ("Actors:", "Fixtures:", "Action:", "Expected result:", "Evidence:"):
            if not _has_label(lines, label):
                findings.append(_finding("doc-drift", f"feature file is missing {label}", feature_id=path.stem, path=relative))

    return feature_ids, findings


def _source_entries(reader: dict[str, Any], label: str) -> list[dict[str, str]]:
    raw_entries = reader.get("source_entries")
    if not isinstance(raw_entries, list) or not raw_entries:
        raise MaintainError(f"{label}.source_entries must contain at least one entry")
    entries: list[dict[str, str]] = []
    for index, raw in enumerate(raw_entries):
        if isinstance(raw, str):
            entries.append({"path": _text(raw, f"{label}.source_entries[{index}]")})
            continue
        entry = _mapping(raw, f"{label}.source_entries[{index}]")
        entries.append(
            {
                "path": _text(entry.get("path"), f"{label}.source_entries[{index}].path"),
                "lines": _text(entry.get("lines", ""), f"{label}.source_entries[{index}].lines", allow_empty=True),
            }
        )
    return entries


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
                _safe_path(project_root, entry["path"], "source entry", require_exists=True)
            except MaintainError as exc:
                findings.append(_finding("blocked", str(exc), feature_id=feature_id, source=entry["path"]))
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


def _validate_live_report(
    report: dict[str, Any], project_root: Path, skill_root: Path, feature_ids: list[str]
) -> tuple[dict[str, dict[str, Any]], list[dict[str, Any]]]:
    doctor = _mapping(report.get("doctor"), "live report doctor")
    findings: list[dict[str, Any]] = []
    reported_root = doctor.get("skill_root")
    if reported_root is not None:
        try:
            if _resolved(Path(_text(reported_root, "live report doctor.skill_root"))) != _resolved(skill_root):
                findings.append(_finding("doctor-failure", "Doctor receipt names a different verification skill", path="SKILL.md"))
        except (OSError, TypeError) as exc:
            findings.append(_finding("doctor-failure", f"Doctor receipt has an invalid skill root: {exc}", path="SKILL.md"))
    if doctor.get("status") != "pass" or doctor.get("owned") is not True or doctor.get("healthy") is not True:
        findings.append(_finding("doctor-failure", "Doctor did not prove an owned, healthy instance", path="SKILL.md"))

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
        if status == "pass":
            evidence = _text_list(result.get("evidence"), f"{label}.evidence", allow_empty=False)
            for raw_path in evidence:
                try:
                    _external_existing(raw_path, "evidence artifact", project_root)
                except MaintainError as exc:
                    findings.append(_finding("blocked", str(exc), feature_id=feature_id))
            if result.get("evidence_survives_cleanup") is not True:
                findings.append(_finding("blocked", "live proof did not confirm evidence survives cleanup", feature_id=feature_id))
            if result.get("source_match") is not True:
                findings.append(_finding("doc-drift", "live pass did not confirm the map matches the current source", feature_id=feature_id))
        elif status == "fail":
            _text(result.get("route"), f"{label}.route")
            _text(result.get("observed"), f"{label}.observed")
            evidence = _text_list(result.get("evidence"), f"{label}.evidence", allow_empty=False)
            evidence_ok = True
            for raw_path in evidence:
                try:
                    _external_existing(raw_path, "evidence artifact", project_root)
                except MaintainError as exc:
                    evidence_ok = False
                    findings.append(_finding("blocked", str(exc), feature_id=feature_id))
            doctor_healthy = (
                doctor.get("status") == "pass"
                and doctor.get("owned") is True
                and doctor.get("healthy") is True
            )
            if result.get("source_match") is True and doctor_healthy and evidence_ok:
                findings.append(_finding("product-gap", "live behavior failed after source and map agreement", feature_id=feature_id))
            else:
                findings.append(_finding("doc-drift", "live failure was not proven against a source-matching map", feature_id=feature_id))
        elif status == "harness-gap":
            _text(result.get("route"), f"{label}.route")
            findings.append(_finding("harness-gap", _text(result.get("reason"), f"{label}.reason"), feature_id=feature_id))
        elif status == "verified-unreachable":
            _text(result.get("route"), f"{label}.route")
            prerequisite = _text(result.get("prerequisite"), f"{label}.prerequisite")
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
            findings.append(_finding("blocked", _text(result.get("reason"), f"{label}.reason"), feature_id=feature_id))
        by_id[feature_id] = result

    for feature_id in sorted(expected - set(by_id)):
        findings.append(_finding("blocked", "no live result was recorded for this feature", feature_id=feature_id))
    return by_id, findings


def _load_change_log(path: Path | None, skill_root: Path) -> list[str]:
    if path is None:
        return []
    raw = _load_json(path)
    values = raw.get("changed_files", [])
    changed_files = _text_list(values, "change log changed_files")
    for relative in changed_files:
        if Path(relative).is_absolute():
            raise MaintainError(f"change log path must be relative to the verification skill: {relative}")
        _safe_path(skill_root, relative, "change log path", require_exists=True)
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
    project_root = _resolved(project_root)
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
        feature_text = feature_path.read_text(encoding="utf-8")
        for field, values in reader["authoritative"].items():
            for value in values:
                if value not in feature_text:
                    source = ", ".join(entry["path"] for entry in reader["source_entries"])
                    reader_findings.append(
                        _finding(
                            "doc-drift",
                            f"{field} from the source reader is absent from the feature recipe: {value}",
                            feature_id=feature_id,
                            path=f"features/{feature_id}.md",
                            source=source,
                        )
                    )
    _, live_findings = _validate_live_report(live_report, project_root, skill_root, feature_ids)
    findings = map_findings + reader_findings + live_findings
    changed_files = _load_change_log(change_log_path, skill_root)
    blocking = [item for item in findings if item.get("blocking") is True]
    if blocking:
        outcome = "blocked"
    elif changed_files:
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


def apply_patch_plan(skill_root: Path, patch_plan_path: Path) -> dict[str, Any]:
    """Apply only explicit doc or harness replacements below skill_root."""

    skill_root = _real_directory(skill_root, "skill root")
    plan = _load_json(patch_plan_path)
    changes = plan.get("changes")
    if not isinstance(changes, list) or not changes:
        raise MaintainError("patch plan must contain at least one change")

    originals: dict[Path, str] = {}
    proposed: dict[Path, str] = {}
    metadata: list[dict[str, str]] = []
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
        if path not in originals:
            originals[path] = path.read_text(encoding="utf-8")
            proposed[path] = originals[path]
        count = proposed[path].count(old)
        if count != 1:
            raise MaintainError(f"{label}.old must occur exactly once in {raw_path}, found {count}")
        proposed[path] = proposed[path].replace(old, new, 1)
        metadata.append({"path": raw_path, "kind": kind})

    written: list[Path] = []
    try:
        for path, text in proposed.items():
            _atomic_write(path, text)
            written.append(path)
    except Exception:
        for path in written:
            _atomic_write(path, originals[path])
        raise
    return {
        "outcome": "changed",
        "changed_files": [path.relative_to(skill_root).as_posix() for path in proposed],
        "changes": metadata,
    }


def doctor(skill_root: Path, report_path: Path) -> dict[str, Any]:
    """Validate an already-run app Doctor receipt without probing the app."""

    skill_root = _real_directory(skill_root, "skill root")
    report = _load_json(report_path)
    checks: list[dict[str, Any]] = []
    checks.append({"name": "skill-root", "pass": skill_root.is_dir() and not skill_root.is_symlink()})
    reported_root = report.get("skill_root")
    root_matches = False
    if isinstance(reported_root, str):
        root_matches = _resolved(Path(reported_root)) == skill_root
    checks.append({"name": "reported-skill-root", "pass": root_matches})
    checks.append({"name": "owned-instance", "pass": report.get("owned") is True})
    checks.append({"name": "healthy-instance", "pass": report.get("healthy") is True})
    checks.append({"name": "session-id", "pass": isinstance(report.get("session_id"), str) and bool(report["session_id"].strip())})
    status = "pass" if all(check["pass"] for check in checks) else "fail"
    return {"status": status, "checks": checks}


def _cleanup_path(run_root: Path, raw: str) -> Path:
    path = Path(raw)
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise MaintainError(f"owned cleanup path must be a simple relative path: {raw}")
    return _safe_path(run_root, raw, "owned cleanup path")


def cleanup(run_root: Path, manifest_path: Path, project_root: Path | None = None) -> dict[str, Any]:
    """Remove only owned scratch paths and verify evidence remains."""

    run_root = _real_directory(run_root, "run root")
    manifest = _load_json(manifest_path)
    owned = _text_list(manifest.get("owned_paths"), "owned_paths", allow_empty=True)
    evidence = _text_list(manifest.get("evidence_paths"), "evidence_paths", allow_empty=False)
    owned_paths = [_cleanup_path(run_root, raw) for raw in owned]
    evidence_paths = [_external_existing(raw, "evidence artifact", project_root) for raw in evidence]
    for evidence_path in evidence_paths:
        if any(evidence_path == owned_path or _is_relative_to(evidence_path, owned_path) for owned_path in owned_paths):
            raise MaintainError(f"cleanup would remove evidence: {evidence_path}")

    removed: list[str] = []
    for path in owned_paths:
        if not os.path.lexists(path):
            continue
        if path.is_symlink() or path.is_file():
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
    apply_parser.add_argument("--json", action="store_true")

    doctor_parser = subparsers.add_parser("doctor", help="validate an owned, healthy Doctor receipt")
    doctor_parser.add_argument("--skill-root", required=True)
    doctor_parser.add_argument("--report", required=True)
    doctor_parser.add_argument("--json", action="store_true")

    cleanup_parser = subparsers.add_parser("cleanup", help="remove owned scratch paths and retain evidence")
    cleanup_parser.add_argument("--run-root", required=True)
    cleanup_parser.add_argument("--manifest", required=True)
    cleanup_parser.add_argument("--project-root")
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
            payload = apply_patch_plan(Path(args.skill_root), Path(args.patch_plan))
            _print_payload(payload, args.json)
            return 0
        if args.command == "doctor":
            payload = doctor(Path(args.skill_root), Path(args.report))
            _print_payload(payload, args.json)
            return 0 if payload["status"] == "pass" else 1
        payload = cleanup(
            Path(args.run_root),
            Path(args.manifest),
            Path(args.project_root) if args.project_root else None,
        )
        _print_payload(payload, args.json)
        return 0
    except (MaintainError, OSError) as exc:
        print(f"maintain-verification-skill error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
