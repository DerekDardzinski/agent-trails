"""What the reading operations return: search, show, lineage and prime.

NOTE: no `from __future__ import annotations` in this package. See `ops/__init__.py`.
"""

from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel

from trails import render
from trails.models import (
    ArtifactRef,
    CodeCapture,
    ProjectFramingVersion,
    ProjectMembershipEvent,
    ProjectPath,
    ProjectRef,
    Record,
)
from trails.ops.types import RenderedResult
from trails.reachability import PathState, Reachability
from trails.vocab import Detail, EntryType, Relation, WorkStatus


class KeywordMatch(BaseModel):
    """One natural-language query term found in a searchable record field."""

    term: str
    field: Literal["title", "body"]


class ProjectKeywordMatch(BaseModel):
    """One natural-language query term found in a searchable current-project field."""

    term: str
    field: Literal["name", "framing_title", "framing_body"]


class SearchHit(BaseModel):
    """A compact matched-record preview for deciding whether to call show."""

    id: str
    entry_type: EntryType
    title: str
    projects: list[ProjectRef]
    created_at: datetime
    lifecycle: Literal["current", "superseded", "retired"]
    superseded_by: str | None
    work_status: WorkStatus
    tags: list[str]
    matched_by: list[Literal["keyword", "exact", "semantic"]]
    keyword_matches: list[KeywordMatch]
    excerpt: str | None


class ProjectSearchHit(BaseModel):
    """A separately ranked project match from identity or current framing."""

    id: str
    name: str
    framing_version: int | None
    framing_title: str | None
    matched_by: list[Literal["keyword", "exact", "semantic"]]
    keyword_matches: list[ProjectKeywordMatch]
    excerpt: str | None
    updated_at: datetime


class SearchResult(RenderedResult):
    """One record page plus at most three separately ranked project matches."""

    state: str
    hits: list[SearchHit]
    total: int
    project_hits: list[ProjectSearchHit]
    project_total: int
    offset: int
    filters: dict[str, Any]
    relaxed: list[tuple[str, int, str | None]]
    tag_aliases: dict[str, list[str]]
    next: list[str]

    def render_text(self) -> str:
        return render.search_text(self)


class RelatedRecord(BaseModel):
    """A record reached over one link, or over several for lineage."""

    relation: Relation
    direction: Literal["in", "out"]
    depth: int
    record: Record


class ArtifactStatus(BaseModel):
    ref: ArtifactRef
    reachability: Reachability


class CaptureStatus(BaseModel):
    capture: CodeCapture
    reachability: Reachability


class PathStatus(BaseModel):
    path: ProjectPath
    state: PathState


class ProjectPathStatus(BaseModel):
    project: ProjectRef
    path: ProjectPath
    state: PathState


class ShownRecord(BaseModel):
    """One record prepared at the requested show detail."""

    record: Record
    outgoing: list[RelatedRecord]
    incoming: list[RelatedRecord]
    artifacts: list[ArtifactStatus]
    captures: list[CaptureStatus]
    capture_store: str
    project_paths: list[ProjectPathStatus]
    membership_history: list[ProjectMembershipEvent]
    contradicted_by: int


class ShownProject(BaseModel):
    """One exact project prepared at the requested detail."""

    project: ProjectRef
    created_at: datetime
    created_by: str
    created_on_host: str
    paths: list[PathStatus]
    current_framing: ProjectFramingVersion | None
    framing_history: list[ProjectFramingVersion]
    current_record_count: int


class ShowResult(RenderedResult):
    """Exact records and projects at one detail level, plus missing identifiers."""

    requested: list[str]
    records: list[ShownRecord]
    projects: list[ShownProject]
    missing: list[str]
    detail: Detail
    multiple: bool

    def render_text(self) -> str:
        return render.show_text(self)


class LineageResult(RenderedResult):
    record: Record
    ancestors: list[RelatedRecord]
    descendants: list[RelatedRecord]

    def render_text(self) -> str:
        return render.lineage_text(self)


class PrimeRecordRef(BaseModel):
    """A record as prime lists it: identity and lifecycle, never its body.

    Prime is an orientation page over every current record of a project, so it carries
    references and leaves full records to show. Measured on a 317-record project, full
    records here made one 10.6 MB MCP reply that the host dropped.
    """

    id: str
    entry_type: EntryType
    title: str
    work_status: WorkStatus
    superseded_by: str | None
    retired: bool
    replaces: str | None  # the record this one supersedes
    last_verified: tuple[date, str] | None


class PrimeRelation(BaseModel):
    relation: Relation
    record: PrimeRecordRef


class PrimeEntry(BaseModel):
    record: PrimeRecordRef
    builds_on: list[PrimeRecordRef]
    incoming_results: list[PrimeRelation]
    verifications: list[PrimeRecordRef]
    contradicted_by: list[PrimeRecordRef]  # current records of any type that contradict this one


class ProjectIndexEntry(BaseModel):
    """One bounded whole-store orientation row."""

    project: ProjectRef
    framing_title: str | None
    record_count: int
    open_count: int
    last_activity: datetime


class PrimeResult(RenderedResult):
    """The whole-store project index or one project's complete current orientation."""

    project: ProjectRef | None
    projects: list[ProjectIndexEntry]
    project_total: int
    offset: int
    limit: int
    unscoped_count: int
    paths: list[PathStatus]
    framing: ProjectFramingVersion | None
    current_count: int
    replaced_count: int
    retired_count: int
    problem_statements: list[PrimeRecordRef]
    # problem statement id -> current records of any type that contradict it; absent when none
    problem_statement_contradicted_by: dict[str, list[PrimeRecordRef]]
    # entry type -> its listed current records: every open one, and every closed one that a
    # current record contradicts. A type with current records always has a key, even if empty.
    groups: dict[str, list[PrimeEntry]]
    # entry type -> current closed records of that type, problem statements excluded
    closed_counts: dict[str, int]
    store_error: str | None

    def render_text(self) -> str:
        return render.prime_text(self)
