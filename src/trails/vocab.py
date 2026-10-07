"""Closed vocabularies and stable constants used by the store and operations."""

from __future__ import annotations

import os
import re
from typing import Literal

EntryType = Literal[
    "project-context",
    "problem-statement",
    "hypothesis",
    "procedure",
    "result",
    "verification",
    "fact",
    "insight",
    "decision",
    "report",
    "note",
]
ENTRY_TYPES: tuple[EntryType, ...] = (
    "project-context",
    "problem-statement",
    "hypothesis",
    "procedure",
    "result",
    "verification",
    "fact",
    "insight",
    "decision",
    "report",
    "note",
)
ENTRY_TYPE_PREFIXES: dict[EntryType, str] = {
    "project-context": "ctx",
    "problem-statement": "prb",
    "hypothesis": "hyp",
    "procedure": "pro",
    "result": "res",
    "verification": "ver",
    "fact": "fac",
    "insight": "ins",
    "decision": "dec",
    "report": "rpt",
    "note": "note",
}

WorkStatus = Literal["open", "closed"]
WORK_STATUSES: tuple[WorkStatus, ...] = ("open", "closed")

Relation = Literal["builds_on", "supersedes", "contradicts", "cites"]
RELATIONS: tuple[Relation, ...] = ("builds_on", "supersedes", "contradicts", "cites")

# The body registered for a new project's context. Prime compares exactly against this text so
# it can say the template has not been filled in instead of printing six empty headings.
PROJECT_CONTEXT_HEADINGS: tuple[str, ...] = (
    "## Goal",
    "## Data",
    "## Environments",
    "## Code",
    "## Conventions",
    "## Open questions",
)
PROJECT_CONTEXT_TEMPLATE = "\n\n".join(PROJECT_CONTEXT_HEADINGS) + "\n"

Detail = Literal["summary", "full", "provenance"]

# New type-prefixed IDs use five lowercase base36 characters, with six as the fallback after
# collisions. Compatibility forms remain recognized: legacy `rec-` plus 16 lowercase hex and
# the immediately previous type-prefix plus 16 lowercase hex format.
LEGACY_ID_PREFIX = "rec"
ID_ALPHABET = "0123456789abcdefghijklmnopqrstuvwxyz"
ID_LENGTHS: tuple[int, ...] = (5, 6)
ID_ATTEMPTS_PER_LENGTH = 10
LEGACY_HEX_LENGTH = 16
_TYPED_PREFIX_ALTERNATION = "|".join(re.escape(prefix) for prefix in ENTRY_TYPE_PREFIXES.values())
_NEW_TYPED_ID = rf"(?:{_TYPED_PREFIX_ALTERNATION})-[0-9a-z]{{5,6}}"
_PREVIOUS_TYPED_ID = rf"(?:{_TYPED_PREFIX_ALTERNATION})-[0-9a-f]{{{LEGACY_HEX_LENGTH}}}"
_LEGACY_ID = rf"{LEGACY_ID_PREFIX}-[0-9a-f]{{{LEGACY_HEX_LENGTH}}}"
ID_PATTERN = re.compile(rf"(?:{_NEW_TYPED_ID}|{_PREVIOUS_TYPED_ID}|{_LEGACY_ID})")

STALE_DAYS_ENV = "TRAILS_STALE_DAYS"
DEFAULT_STALE_DAYS = 90


def stale_days() -> int:
    """Days after which a procedure's last verification counts as stale.

    `$TRAILS_STALE_DAYS` when it parses as an integer, else 90. The one definition prime and
    lint read, so the two cannot disagree about which procedures are stale.
    """
    raw = os.environ.get(STALE_DAYS_ENV, "").strip()
    try:
        return int(raw)
    except ValueError:
        return DEFAULT_STALE_DAYS


def enum_column(values: tuple[str, ...]) -> str:
    """Render a vocabulary tuple as a SQL ENUM column type.

    The schema calls this instead of spelling the members out, so the database and the Python
    vocabulary cannot disagree about what a column accepts.
    """
    members = ",".join(f"'{value}'" for value in values)
    return f"ENUM({members})"
