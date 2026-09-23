"""Small, dependency-free validation helpers for skill frontmatter."""

from __future__ import annotations

import re


REQUIRED_FIELDS = ("name", "description")
NULL_VALUES = {"", "~", "null", "true", "false", "yes", "no", "on", "off"}
YAML_DECIMAL_DIGITS = r"[0-9]+(?:_[0-9]+)*"
YAML_HEX_DIGITS = r"[0-9a-fA-F]+(?:_[0-9a-fA-F]+)*"
YAML_OCTAL_DIGITS = r"[0-7]+(?:_[0-7]+)*"
YAML_BINARY_DIGITS = r"[01]+(?:_[01]+)*"
YAML_NUMBER_RE = re.compile(
    rf"""[-+]?(?:
        (?:0|[1-9](?:_[0-9]+)*)(?:\.(?:{YAML_DECIMAL_DIGITS})?)?
        (?:[eE][-+]?{YAML_DECIMAL_DIGITS})?
        |\.{YAML_DECIMAL_DIGITS}(?:[eE][-+]?{YAML_DECIMAL_DIGITS})?
    )""",
    re.VERBOSE,
)
YAML_BASE_PREFIX_RE = re.compile(
    rf"[-+]?(?:0[xX]{YAML_HEX_DIGITS}|0[oO]{YAML_OCTAL_DIGITS}|0[bB]{YAML_BINARY_DIGITS})"
)
YAML_LEGACY_OCTAL_RE = re.compile(rf"[-+]?0{YAML_OCTAL_DIGITS}")
YAML_SEXAGESIMAL_RE = re.compile(
    rf"[-+]?{YAML_DECIMAL_DIGITS}(?::[0-5]?[0-9])+(?:\.{YAML_DECIMAL_DIGITS})?"
)
YAML_SPECIAL_FLOAT_RE = re.compile(r"[-+]?\.(?:inf|nan)", re.IGNORECASE)
YAML_TIMESTAMP_RE = re.compile(
    r"""[0-9]{4}-[0-9]{1,2}-[0-9]{1,2}
    (?:(?:[Tt]|[ \t]+)[0-9]{1,2}:[0-9]{2}:[0-9]{2}
    (?:\.[0-9]*)?(?:[ \t]*(?:Z|[-+][0-9]{1,2}(?::?[0-9]{2})?))?)?""",
    re.VERBOSE,
)
YAML_TAG_RE = re.compile(
    r"^(?P<tag>!![A-Za-z0-9_-]+|!<tag:yaml\.org,2002:[^>]+>)"
    r"(?:[ \t]+(?P<payload>.*))?$",
    re.IGNORECASE,
)
YAML_COLLECTION_TAGS = {"map", "omap", "pairs", "seq", "set"}
YAML_NULL_TAGS = {"null"}
YAML_BOOLEAN_TAGS = {"bool"}
YAML_STRING_TAGS = {"str"}


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


def _yaml_tag_kind(tag: str) -> str:
    if tag.startswith("!!"):
        return tag[2:].lower()
    return tag[len("!<tag:yaml.org,2002:") : -1].lower()


def _tagged_value_error(field: str, value: str) -> str | None:
    match = YAML_TAG_RE.fullmatch(value)
    if match is None:
        if value.startswith("!"):
            return f"{field} must be a scalar string, not an unknown YAML tag"
        return None

    kind = _yaml_tag_kind(match.group("tag"))
    payload = (match.group("payload") or "").strip()
    if kind in YAML_STRING_TAGS:
        if not payload:
            return f"{field} must be a non-empty scalar string"
        if payload[0] in {"'", '"'}:
            return _scalar_value_error(field, payload)
        return None
    if kind in YAML_COLLECTION_TAGS:
        return f"{field} must be a scalar string, not a YAML collection"
    if kind in YAML_NULL_TAGS:
        return f"{field} must be a non-null scalar string"
    if kind in YAML_BOOLEAN_TAGS:
        return f"{field} must be a scalar string, not a boolean"
    return f"{field} must be a scalar string, not a YAML {kind}"


def _scalar_value_error(field: str, raw_value: str) -> str | None:
    value = _without_inline_comment(raw_value).strip()
    if not value:
        return f"{field} must be a non-empty scalar string"

    tagged_value_error = _tagged_value_error(field, value)
    if tagged_value_error:
        return tagged_value_error
    if value.startswith("["):
        return f"{field} must be a scalar string, not a list"
    if value.startswith("{"):
        return f"{field} must be a scalar string, not a mapping"

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
    if (
        YAML_NUMBER_RE.fullmatch(value)
        or YAML_BASE_PREFIX_RE.fullmatch(value)
        or YAML_LEGACY_OCTAL_RE.fullmatch(value)
        or YAML_SEXAGESIMAL_RE.fullmatch(value)
        or YAML_SPECIAL_FLOAT_RE.fullmatch(value)
        or YAML_TIMESTAMP_RE.fullmatch(value)
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
