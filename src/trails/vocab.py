"""Closed vocabularies and stable constants used by the store and operations."""

from __future__ import annotations

import re
from typing import Literal

ProjectAction = Literal["add", "remove"]
PROJECT_ACTIONS: tuple[ProjectAction, ...] = ("add", "remove")


PROJECT_ID_PREFIX = "prj"
ID_ALPHABET = "0123456789abcdefghijklmnopqrstuvwxyz"
ID_LENGTHS: tuple[int, ...] = (5, 6)
ID_ATTEMPTS_PER_LENGTH = 10
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


def enum_column(values: tuple[str, ...]) -> str:
    """Render a vocabulary tuple as a SQL ENUM column type."""
    members = ",".join(f"'{value}'" for value in values)
    return f"ENUM({members})"
