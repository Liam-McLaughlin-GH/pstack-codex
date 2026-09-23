"""Small, dependency-free validation helpers for skill frontmatter."""

from __future__ import annotations

import re


REQUIRED_FIELDS = ("name", "description")
NULL_VALUES = {"", "~", "null", "true", "false", "yes", "no", "on", "off"}
NUMBER_RE = re.compile(
    r"[-+]?(?:0|[1-9][0-9]*)(?:\.[0-9]+)?(?:[eE][-+]?[0-9]+)?"
)
YAML_BASE_PREFIX_RE = re.compile(
    r"[-+]?0[xX][0-9a-fA-F]+|[-+]?0[oO][0-7]+|[-+]?0[bB][01]+"
)
YAML_SPECIAL_FLOAT_RE = re.compile(r"[-+]?\.(?:inf|nan)", re.IGNORECASE)
YAML_NON_STRING_TAG_RE = re.compile(
    r"!!(?:int|float|timestamp|bool)\b", re.IGNORECASE
)


def _without_inline_comment(value: str) -> str:
    """Remove a YAML comment without treating a quoted ``#`` as a comment."""

    quote = None
    escaped = False
    for index, character in enumerate(value):
        if quote == '"' and escaped:
            escaped = False
            continue
        if quote == '"' and character == "\\":
            escaped = True
            continue
        if character in {"'", '"'}:
            if quote is None:
                quote = character
            elif quote == character:
                quote = None
            continue
        if character == "#" and quote is None and (
            index == 0 or value[index - 1].isspace()
        ):
            return value[:index].rstrip()
    return value.strip()


def _scalar_value_error(field: str, raw_value: str) -> str | None:
    value = _without_inline_comment(raw_value).strip()
    if not value:
        return f"{field} must be a non-empty scalar string"

    if value.startswith("["):
        return f"{field} must be a scalar string, not a list"
    if value.startswith("{"):
        return f"{field} must be a scalar string, not a mapping"
    if value.startswith(("!!null", "!!seq", "!!map")):
        return f"{field} must be a scalar string, not a YAML collection or null"

    if value[0] in {"'", '"'}:
        if len(value) < 2 or value[-1] != value[0]:
            return f"{field} must be a valid scalar string"
        inner = value[1:-1]
        if value[0] == "'":
            inner = inner.replace("''", "'")
        if not inner.strip():
            return f"{field} must be a non-empty scalar string"
        return None

    lowered = value.lower()
    if lowered in NULL_VALUES:
        if lowered in {"true", "false", "yes", "no", "on", "off"}:
            return f"{field} must be a scalar string, not a boolean"
        return f"{field} must be a non-null scalar string"
    if NUMBER_RE.fullmatch(value):
        return f"{field} must be a scalar string, not a number"
    if (
        YAML_BASE_PREFIX_RE.fullmatch(value)
        or YAML_SPECIAL_FLOAT_RE.fullmatch(value)
        or YAML_NON_STRING_TAG_RE.match(value)
    ):
        return f"{field} must be a scalar string, not a number"
    if value.startswith(("|", ">", "&", "*")):
        return f"{field} must be a non-empty scalar string"
    return None


def required_frontmatter_errors(metadata: str) -> list[str]:
    """Return readable errors for required top-level scalar frontmatter fields."""

    values: dict[str, str] = {}
    errors: list[str] = []
    for line in metadata.splitlines():
        match = re.fullmatch(
            r"(?P<field>name|description):(?:[ \t]*(?P<value>.*))?", line
        )
        if match is None:
            continue
        field = match.group("field")
        if field in values:
            errors.append(f"{field} is duplicated")
            continue
        values[field] = match.group("value") or ""

    for field in REQUIRED_FIELDS:
        if field not in values:
            errors.append(f"{field} is missing")
            continue
        value_error = _scalar_value_error(field, values[field])
        if value_error:
            errors.append(value_error)
    return errors
