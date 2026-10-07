"""What the reading operations return: search, show, lineage and prime.

Each result renders through `trails.render`, the one module that formats reading text.

NOTE: no `from __future__ import annotations` in this package. See `ops/__init__.py`.
"""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel

from trails import render
from trails.models import ArtifactRef, CodeCapture, ProjectPath, Record
from trails.ops.types import RenderedResult
from trails.reachability import PathState, Reachability
from trails.vocab import Detail, EntryType, Relation, WorkStatus


class KeywordMatch(BaseModel):
    """One natural-language query term found in a searchable record text field."""

    term: str
    field: Literal["title", "body"]


class SearchHit(BaseModel):
    """A compact matched-record preview for deciding whether to call show."""

    id: str
    entry_type: EntryType
    title: str
    project: str | None
    created_at: datetime
    lifecycle: Literal["current", "superseded", "retired"]
    superseded_by: str | None
    work_status: WorkStatus
    tags: list[str]
    matched_by: list[Literal["keyword", "exact", "semantic"]]
    keyword_matches: list[KeywordMatch]
    excerpt: str | None


class SearchResult(RenderedResult):
    """One page of search results, or the report of why nothing matched."""

    state: str
    hits: list[SearchHit]
    total: int
    offset: int
    filters: dict[str, Any]  # the filter arguments as received, only those given
    relaxed: list[tuple[str, int, str | None]]  # (filter, matches without it, "<id>, <tag>")
    tag_aliases: dict[str, list[str]]  # each listed tag -> the other tags it also matches
    next: list[str]

    def render_text(self) -> str:
        """State line, results or the no-match report, Next: line."""
        return render.search_text(self)


class RelatedRecord(BaseModel):
    """A record reached over one link, or over several for lineage."""

    relation: Relation
    direction: Literal["in", "out"]  # "out": this record links to it; "in": it links here
    depth: int  # links followed to reach it; 1 for a direct link
    record: Record


class ArtifactStatus(BaseModel):
    """An artifact reference and whether it can be read now."""

    ref: ArtifactRef
    reachability: Reachability


class CaptureStatus(BaseModel):
    """A code capture and whether its commit still resolves."""

    capture: CodeCapture
    reachability: Reachability


class PathStatus(BaseModel):
    """A registered project path and whether it exists on this machine."""

    path: ProjectPath
    state: PathState


class ShownRecord(BaseModel):
    """One record prepared at the requested show detail."""

    record: Record
    outgoing: list[RelatedRecord]  # summary only
    incoming: list[RelatedRecord]  # summary only
    artifacts: list[ArtifactStatus]  # summary and provenance
    captures: list[CaptureStatus]  # provenance only
    capture_store: str
    project_registered: bool
    project_paths: list[PathStatus]  # provenance only
    contradicted_by: int


class ShowResult(RenderedResult):
    """One or several records at one level of detail, and any requested ids not found."""

    requested: list[str]
    records: list[ShownRecord]  # first-occurrence request order
    missing: list[str]
    detail: Detail
    multiple: bool  # the caller supplied an array, even when it had one id

    def render_text(self) -> str:
        """The single-record form stays compact; arrays have one block per record and one Next."""
        return render.show_text(self)


class LineageResult(RenderedResult):
    """What a record was built on and what was built on it."""

    record: Record
    ancestors: list[RelatedRecord]
    descendants: list[RelatedRecord]

    def render_text(self) -> str:
        """State line, one line per related record, Next: line."""
        return render.lineage_text(self)


class PrimeRelation(BaseModel):
    """A current record linked to a prime entry, with the relation it used."""

    relation: Relation
    record: Record


class PrimeEntry(BaseModel):
    """One current record and the connections prime explains beside it."""

    record: Record
    builds_on: list[Record]  # outgoing builds_on targets, including replaced targets
    incoming_results: list[PrimeRelation]  # current results using builds_on/cites/contradicts
    verifications: list[Record]  # current verification records building on this result


class PrimeResult(RenderedResult):
    """The current state of one project, or of the whole store."""

    project: str | None
    registered: bool  # False only when a named project is not registered
    paths: list[PathStatus]  # named project's registered paths
    current_count: int
    replaced_count: int
    retired_count: int
    project_contexts: list[Record]
    problem_statements: list[Record]
    groups: dict[str, list[PrimeEntry]]  # entry type -> newest-first current records
    store_error: str | None

    def render_text(self) -> str:
        """State, project context, problem statements, grouped records, connections, next calls."""
        return render.prime_text(self)
