"""Closed vocabularies and stable constants used by the store and operations."""

from __future__ import annotations

import os
import re
from typing import Literal

EntryType = Literal[
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

ProjectAction = Literal["add", "remove"]
PROJECT_ACTIONS: tuple[ProjectAction, ...] = ("add", "remove")

Relation = Literal["builds_on", "supersedes", "contradicts", "cites"]
RELATIONS: tuple[Relation, ...] = ("builds_on", "supersedes", "contradicts", "cites")

Detail = Literal["summary", "full", "provenance"]

# New record ids use five lowercase base36 characters, with six as collision fallback.
# The unrelated historic record-id forms remain readable. Project ids have their own pattern.
LEGACY_ID_PREFIX = "rec"
PROJECT_ID_PREFIX = "prj"
ID_ALPHABET = "0123456789abcdefghijklmnopqrstuvwxyz"
ID_LENGTHS: tuple[int, ...] = (5, 6)
ID_ATTEMPTS_PER_LENGTH = 10
LEGACY_HEX_LENGTH = 16
_TYPED_PREFIX_ALTERNATION = "|".join(re.escape(prefix) for prefix in ENTRY_TYPE_PREFIXES.values())
_NEW_TYPED_ID = rf"(?:{_TYPED_PREFIX_ALTERNATION})-[0-9a-z]{{5,6}}"
_PREVIOUS_TYPED_ID = rf"(?:{_TYPED_PREFIX_ALTERNATION})-[0-9a-f]{{{LEGACY_HEX_LENGTH}}}"
_LEGACY_ID = rf"{LEGACY_ID_PREFIX}-[0-9a-f]{{{LEGACY_HEX_LENGTH}}}"
ID_PATTERN = re.compile(rf"(?:{_NEW_TYPED_ID}|{_PREVIOUS_TYPED_ID}|{_LEGACY_ID})")
PROJECT_ID_PATTERN = re.compile(rf"{PROJECT_ID_PREFIX}-[0-9a-z]{{5,6}}")

# ---------------------------------------------------------------- the entry model (redesign)
#
# Six kinds (dec-ix8zd). A kind exists only where the store treats it differently; domain
# labels are tags. The kind is fixed at creation, so an id keeps its kind's prefix.
Kind = Literal["goal", "task", "note", "claim", "preference", "skill"]
KINDS: tuple[Kind, ...] = ("goal", "task", "note", "claim", "preference", "skill")
KIND_PREFIXES: dict[Kind, str] = {
    "goal": "gol",
    "task": "tsk",
    "note": "note",
    "claim": "clm",
    "preference": "prf",
    "skill": "skl",
}
# The kinds with open/closed state, a priority, children through part_of and close outcomes.
WORK_KINDS: tuple[Kind, ...] = ("goal", "task")
# The kinds that record whether a person or an agent stated them.
STATED_KINDS: tuple[Kind, ...] = ("claim", "preference")

# Named from the source's side: `a waits_on b` means a is not ready until b is completed (D2).
LinkRelation = Literal[
    "waits_on", "part_of", "discovered_from", "supersedes", "contradicts", "supports", "references"
]
LINK_RELATIONS: tuple[LinkRelation, ...] = (
    "waits_on",
    "part_of",
    "discovered_from",
    "supersedes",
    "contradicts",
    "supports",
    "references",
)
LinkAction = Literal["add", "remove"]
LINK_ACTIONS: tuple[LinkAction, ...] = ("add", "remove")

Outcome = Literal["completed", "dropped", "failed", "superseded"]
OUTCOMES: tuple[Outcome, ...] = ("completed", "dropped", "failed", "superseded")

Verdict = Literal["approve", "changes_requested"]
VERDICTS: tuple[Verdict, ...] = ("approve", "changes_requested")

StatedBy = Literal["agent", "person"]
STATED_BY_VALUES: tuple[StatedBy, ...] = ("agent", "person")

TaskState = Literal["open", "claimed", "closed"]
TASK_STATES: tuple[TaskState, ...] = ("open", "claimed", "closed")
TaskEventKind = Literal["claim", "release", "close", "reopen"]
TASK_EVENTS: tuple[TaskEventKind, ...] = ("claim", "release", "close", "reopen")

# What search's status filter accepts (D19): a task state, or the outcome a close recorded.
SearchStatus = Literal["open", "claimed", "closed", "completed", "dropped", "failed", "superseded"]
SEARCH_STATUSES: tuple[SearchStatus, ...] = (
    "open",
    "claimed",
    "closed",
    "completed",
    "dropped",
    "failed",
    "superseded",
)

# Lowest number first in the ready queue (dec-o0gww: priority 0-4).
PRIORITY_RANGE: tuple[int, int] = (0, 4)

COMMENT_ID_PREFIX = "cmt"
_KIND_PREFIX_ALTERNATION = "|".join(re.escape(prefix) for prefix in KIND_PREFIXES.values())
ENTRY_ID_PATTERN = re.compile(rf"(?:{_KIND_PREFIX_ALTERNATION})-[0-9a-z]{{5,6}}")
COMMENT_ID_PATTERN = re.compile(rf"{COMMENT_ID_PREFIX}-[0-9a-z]{{5,6}}")

STALE_DAYS_ENV = "TRAILS_STALE_DAYS"
DEFAULT_STALE_DAYS = 90


def stale_days() -> int:
    """Days after which a procedure's last verification counts as stale."""
    raw = os.environ.get(STALE_DAYS_ENV, "").strip()
    try:
        return int(raw)
    except ValueError:
        return DEFAULT_STALE_DAYS


def enum_column(values: tuple[str, ...]) -> str:
    """Render a vocabulary tuple as a SQL ENUM column type."""
    members = ",".join(f"'{value}'" for value in values)
    return f"ENUM({members})"
