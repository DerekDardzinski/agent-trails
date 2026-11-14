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
# Four kinds (trails dec-d1krx). A kind exists only where the store treats it differently;
# domain labels, "goal" among them, are tags. The kind is fixed at creation, so an id keeps
# its kind's prefix. A parent is a task with children.
Kind = Literal["task", "note", "preference", "skill"]
KINDS: tuple[Kind, ...] = ("task", "note", "preference", "skill")
KIND_PREFIXES: dict[Kind, str] = {
    "task": "tsk",
    "note": "note",
    "preference": "prf",
    "skill": "skl",
}
# The kinds with open/closed state, a priority, children through part_of and close outcomes.
WORK_KINDS: tuple[Kind, ...] = ("task",)
# The kinds evidence links join: everything known, as against something to do.
KNOWLEDGE_KINDS: tuple[Kind, ...] = ("note", "preference", "skill")

# Named from the source's side: `a waits_on b` means a is not ready until b is completed (D2).
# Work relations join tasks; evidence relations join knowledge entries; `supersedes` is written
# only when a task is replaced by other tasks.
WorkRelation = Literal["waits_on", "part_of"]
WORK_RELATIONS: tuple[WorkRelation, ...] = ("waits_on", "part_of")
EvidenceRelation = Literal["supports", "contradicts"]
EVIDENCE_RELATIONS: tuple[EvidenceRelation, ...] = ("supports", "contradicts")
AgentLinkRelation = Literal["waits_on", "part_of", "supports", "contradicts"]
LinkRelation = Literal["waits_on", "part_of", "supports", "contradicts", "supersedes"]
LINK_RELATIONS: tuple[LinkRelation, ...] = (
    "waits_on",
    "part_of",
    "supports",
    "contradicts",
    "supersedes",
)
LinkAction = Literal["add", "remove"]
LINK_ACTIONS: tuple[LinkAction, ...] = ("add", "remove")

Outcome = Literal["completed", "dropped", "failed", "superseded"]
OUTCOMES: tuple[Outcome, ...] = ("completed", "dropped", "failed", "superseded")

Verdict = Literal["approve", "changes_requested"]
VERDICTS: tuple[Verdict, ...] = ("approve", "changes_requested")

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


def id_pattern(*kinds: Kind) -> str:
    """A full-match regular expression for the ids of `kinds`, for an operation's schema, so
    an id of another kind cannot be written in the call at all (principle 5)."""
    prefixes = "|".join(re.escape(KIND_PREFIXES[kind]) for kind in kinds)
    return rf"^(?:{prefixes})-[0-9a-z]{{5,6}}$"


TASK_ID = id_pattern("task")
NOTE_ID = id_pattern("note")
PREFERENCE_ID = id_pattern("preference")
SKILL_ID = id_pattern("skill")
KNOWLEDGE_ID = id_pattern(*KNOWLEDGE_KINDS)
