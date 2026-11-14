"""Possible-secret detection, warning only.

detect-secrets through its Python API, with every default plugin EXCEPT the two high-entropy
ones. With those on, every sha256 digest is flagged (measured), and records are full of
digests. Keyword, AWS key, token and private-key detectors stay on. A secret written in plain
prose is not caught.

A finding names the field, the line and the detector, and NEVER the matched text: the
finding itself is returned to an agent and may be logged, so carrying the text would copy the
secret to a second place.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from functools import cache
from itertools import chain

from detect_secrets.core.plugins.util import get_mapping_from_secret_type_to_class
from detect_secrets.core.scan import scan_line
from detect_secrets.settings import default_settings, transient_settings

from trails.models import SecretFinding
from trails.vocab import ENTRY_ID_PATTERN

DISABLED_PLUGINS = frozenset({"HexHighEntropyString", "Base64HighEntropyString"})

# detect-secrets 1.5.0 requires quotes around a value before KeywordDetector, and around an
# AWS secret access key before AWSKeyDetector, will report it. Record bodies commonly contain
# pasted shell and config assignments without quotes. This fallback covers those assignments
# without enabling Base64HighEntropyString, which also flags digests, git shas, record ids,
# paths, S3 URIs and model names.
_ASSIGNMENT = re.compile(
    r"""(?x)
    (?<![A-Za-z0-9_-])
    (?:--?)?
    (?P<name>[A-Za-z_][A-Za-z0-9_-]*)
    \s*[:=]\s*
    (?P<quote>[\"']?)
    (?P<value>[A-Za-z0-9_./+=:@~-]+)
    (?P=quote)
    """
)

_FLAG_VALUE = re.compile(
    r"""(?x)
    (?<![A-Za-z0-9_-])
    --?
    (?P<name>[A-Za-z_][A-Za-z0-9_-]*)
    \s+
    (?P<quote>[\"']?)
    (?P<value>[A-Za-z0-9_./+=:@~-]+)
    (?P=quote)
    """
)
_SENSITIVE_SUFFIXES = frozenset(
    {
        "password",
        "passwd",
        "secret",
        "secret_key",
        "api_key",
        "apikey",
        "access_token",
        "auth_token",
        "token",
    }
)
_NON_SECRET_NAMES = frozenset(
    {"next_page_token", "page_token", "continuation_token", "pagination_token"}
)
_URI_OR_ARN = re.compile(r"(?i)(?:[a-z][a-z0-9+.-]*://|arn:)")
_HEX_IDENTITY = re.compile(r"(?i)[0-9a-f]{40}|[0-9a-f]{64}")
_MODEL_REFERENCE = re.compile(r"(?i)(?:all-[a-z0-9-]+-v\d+|[a-z0-9_.-]+/model_[a-z0-9_.-]+)")

_PLACEHOLDERS = frozenset(
    {"none", "null", "required", "optional", "redacted", "omitted", "example", "value"}
)


@cache
def _plugins() -> tuple[str, ...]:
    with default_settings() as settings:
        names = [plugin["name"] for plugin in settings.json()["plugins_used"]]
    return tuple(name for name in names if name not in DISABLED_PLUGINS)


def scan_for_secrets(fields: Mapping[str, str]) -> list[SecretFinding]:
    """Scan each named text with the configured detectors. Never returns matched text."""
    detectors = get_mapping_from_secret_type_to_class()
    findings: list[SecretFinding] = []
    with transient_settings({"plugins_used": [{"name": name} for name in _plugins()]}):
        for field, text in fields.items():
            for number, line in enumerate(text.splitlines(), start=1):
                for secret in scan_line(line):
                    plugin = detectors.get(secret.type)
                    finding = SecretFinding(
                        field=field,
                        line=number,
                        detector=plugin.__name__ if plugin is not None else secret.type,
                    )
                    if finding not in findings:
                        findings.append(finding)
                fallback = _unquoted_assignment_finding(field, number, line)
                if fallback is not None and fallback not in findings:
                    findings.append(fallback)
    return findings


def _unquoted_assignment_finding(field: str, line_number: int, line: str) -> SecretFinding | None:
    """A secret-looking value assigned to a sensitive name, including an unquoted value.

    Ordinary prose such as ``password: field is required`` stays quiet: a general value must
    be at least eight characters, contain a digit or punctuation, and not be a path, URI, ARN
    or recorded identity. Sensitive suffixes after environment-style prefixes count, so
    `DB_PASSWORD` and `client_secret` are covered. AWS secret access keys have their documented
    40-character alphabet and length. Quoted assignments normally reach the native detector
    too; returning its detector name makes the result deduplicate.
    """
    for match in chain(_ASSIGNMENT.finditer(line), _FLAG_VALUE.finditer(line)):
        detector = _sensitive_detector(match.group("name"))
        if detector is None:
            continue
        value = match.group("value")
        if value.lower() in _PLACEHOLDERS:
            continue
        is_aws = detector == "AWSKeyDetector"
        if not is_aws and _is_reference(value):
            continue
        secret_looking = (
            len(value) == 40 and re.fullmatch(r"[A-Za-z0-9/+]{40}", value) is not None
            if is_aws
            else len(value) >= 8 and any(not char.isalpha() for char in value)
        )
        if secret_looking:
            return SecretFinding(
                field=field,
                line=line_number,
                detector=detector,
            )
    return None


def _sensitive_detector(name: str) -> str | None:
    """The detector name for a sensitive whole name or underscore-delimited suffix."""
    normalized = name.lower().replace("-", "_")
    if any(normalized == item or normalized.endswith(f"_{item}") for item in _NON_SECRET_NAMES):
        return None
    aws = "aws_secret_access_key"
    if normalized == aws or normalized.endswith(f"_{aws}"):
        return "AWSKeyDetector"
    if any(
        normalized == suffix or normalized.endswith(f"_{suffix}") for suffix in _SENSITIVE_SUFFIXES
    ):
        return "KeywordDetector"
    return None


def _is_reference(value: str) -> bool:
    """True for an identity or location commonly assigned to a secret-related field name."""
    return (
        _URI_OR_ARN.match(value) is not None
        or value.startswith(("/", "~/"))
        or ENTRY_ID_PATTERN.fullmatch(value) is not None
        or _HEX_IDENTITY.fullmatch(value) is not None
        or _MODEL_REFERENCE.fullmatch(value) is not None
    )
