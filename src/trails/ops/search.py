"""The `search` operation.

NOTE: no `from __future__ import annotations` in this package. See `ops/__init__.py`.
"""

from collections.abc import Sequence
from datetime import date
from typing import Annotated, Any, Literal

from pydantic import Field

from trails import embeddings, engine as engine_seam, render, retrieval, vector
from trails.errors import UsageError
from trails.models import Project, Record
from trails.ops import context
from trails.ops._marks import Operation
from trails.ops._read_types import (
    KeywordMatch,
    ProjectKeywordMatch,
    ProjectSearchHit,
    SearchHit,
    SearchResult,
)
from trails.vocab import RELATIONS, EntryType, Relation, WorkStatus

_DEFAULT_LIMIT = 10
PROJECT_HIT_LIMIT = 3

OPERATION = Operation(toolset="agent", read_only=True, destructive=False, idempotent=True)


async def search(  # noqa: PLR0917 — public query contract
    query: Annotated[str, Field(description="Natural-language description of what you want.")] = "",
    terms: Annotated[
        list[str],
        Field(
            description=(
                "Exact record-only strings to look for in titles, bodies, tags, values, "
                "artifacts, captured paths and commits, and link targets. Project id and name "
                "exact matching comes from query."
            )
        ),
    ] = [],  # noqa: B006
    tags: Annotated[
        list[str],
        Field(description="Keep only records carrying every requested tag or an alias."),
    ] = [],  # noqa: B006
    entry_type: Annotated[
        EntryType | None, Field(description="Keep only records of this entry type.")
    ] = None,
    work_status: Annotated[
        WorkStatus | None,
        Field(
            description=(
                "Keep only records whose latest work status is open or closed. Unless "
                "include_history is true, this filter searches current record versions only. "
                "Work status is separate from whether a record is current, superseded or retired."
            )
        ),
    ] = None,
    projects: Annotated[
        list[str],
        Field(
            description=(
                "Keep records currently belonging to any of these stable project ids or exact "
                "names. Several values use OR semantics."
            )
        ),
    ] = [],  # noqa: B006
    unscoped: Annotated[
        bool,
        Field(
            description=(
                "Also keep current records with no project membership. Combine with projects "
                "to return either listed-project or unscoped records."
            )
        ),
    ] = False,
    since: Annotated[
        str | None, Field(description="Keep records created on or after YYYY-MM-DD.")
    ] = None,
    until: Annotated[
        str | None, Field(description="Keep records created on or before YYYY-MM-DD.")
    ] = None,
    links: Annotated[
        str | None,
        Field(
            description=(
                '"<relation>:<id>" keeps records linked to or from that id by that relation.'
            )
        ),
    ] = None,
    include_history: Annotated[
        bool,
        Field(
            description=(
                "Also return retired records. When work_status is set, also restore superseded "
                "versions that the default current-version work-status search excludes. Searches "
                "without work_status continue to return superseded records and label them."
            )
        ),
    ] = False,
    limit: Annotated[int, Field(description="Maximum record hits to return.")] = _DEFAULT_LIMIT,
    offset: Annotated[
        int, Field(description="Record hits to skip; project hits are not paged.")
    ] = 0,
) -> SearchResult:
    """Find records and, for an unfiltered query, discover projects by current framing.

    Use this when: you need to know whether the store holds a record or which existing project
    matches a described body of work.

    Use instead: show, once you have an exact id; prime, to orient to all projects or one project;
    lineage, for what a record was built on.

    Returns: a separately ranked list of at most three project matches before one paged record
    list. Project discovery searches exact id/name, current framing keywords, and current framing
    similarity. Record-only filters suppress project matches. A body-keyword match shows a window
    of at most 240 characters centered on the first match. A semantic match with no body-keyword
    match shows the complete selected embedding chunk.
    """
    if limit < 1:
        raise UsageError(f"limit must be at least 1, not {limit}")
    if offset < 0:
        raise UsageError(f"offset must be 0 or more, not {offset}")
    store = context.store()
    resolved_projects = store.resolve_projects(projects)
    filters = retrieval.Filters(
        entry_type=entry_type,
        work_status=work_status,
        tags=tags,
        projects=[item.id for item in resolved_projects],
        unscoped=unscoped,
        since=_date("since", since),
        until=_date("until", until),
        links=_link_filter(links),
        include_history=include_history,
    )

    engine = engine_seam.build_engine()
    status, detail = embeddings.engine_status(engine)
    query_vector: Sequence[float] | None = None
    if status == "ready" and query.strip():
        try:
            query_vector = engine.embed(query)
        except (embeddings.ModelNotAvailableError, embeddings.ModelDownloadError) as exc:
            status, detail = "unavailable", str(exc)
    model_id = engine.model_id() if status == "ready" else None
    equivalence = retrieval.tag_equivalence(store) if tags else {}

    def run(active: retrieval.Filters, *, count_unembedded: bool = False) -> retrieval.Ranked:
        return retrieval.run_search(
            store,
            active,
            query=query,
            terms=terms,
            query_vector=query_vector,
            model_id=model_id,
            equivalence=equivalence,
            count_unembedded=count_unembedded,
        )

    ranked = run(filters, count_unembedded=True)
    total = len(ranked.order)
    page = ranked.order[offset : offset + limit]
    allow_project_hits = bool(query.strip()) and not any(
        (entry_type, work_status, tags, links, since, until, projects, unscoped)
    )
    project_ranked = (
        retrieval.run_project_search(
            store, query=query, query_vector=query_vector, model_id=model_id
        )
        if allow_project_hits
        else retrieval.ProjectRanked(order=[], rankers={}, vector={})
    )
    project_total = len(project_ranked.order)
    project_page = project_ranked.order[:PROJECT_HIT_LIMIT]

    relaxed_firsts: list[tuple[str, int, str]] = []
    if total == 0:
        for name in filters.given():
            found = run(filters.without(name)).order
            if found:
                relaxed_firsts.append((name, len(found), found[0]))
    records = store.get_records([*page, *(first for _, _, first in relaxed_firsts)])
    hits = [_hit(records[record_id], ranked, query, engine) for record_id in page]
    all_projects = {item.id: item for item in store.list_projects()}
    project_hits = [
        _project_hit(all_projects[project_id], project_ranked, query, engine)
        for project_id in project_page
        if project_id in all_projects
    ]
    received: dict[str, Any] = {
        "entry_type": entry_type,
        "work_status": work_status,
        "tags": tags,
        "projects": projects,
        "unscoped": unscoped,
        "since": since,
        "until": until,
        "links": links,
    }
    arguments: dict[str, Any] = {"query": query, "terms": terms, **received}
    arguments["include_history"] = include_history
    if limit != _DEFAULT_LIMIT:
        arguments["limit"] = limit
    return SearchResult(
        state=_state(
            status,
            detail,
            ranked.unembedded,
            [item.name for item in resolved_projects],
            unscoped,
            total,
            project_total,
        ),
        hits=hits,
        total=total,
        project_hits=project_hits,
        project_total=project_total,
        offset=offset,
        filters={name: value for name, value in received.items() if value},
        relaxed=[
            (name, count, f"{first}, {records[first].entry_type}")
            for name, count, first in relaxed_firsts
        ],
        tag_aliases=_tag_aliases(equivalence, tags) if total == 0 else {},
        next=_next_hints(arguments, hits, project_hits, offset, total),
    )


SEARCH_EXCERPT_CHARS = 240
_KEYWORD_LEAD_CHARS = 80
_ELLIPSIS = "..."


def _hit(
    record: Record,
    ranked: retrieval.Ranked,
    query: str,
    engine: embeddings.EmbeddingEngine,
) -> SearchHit:
    vector_match = ranked.vector.get(record.id)
    rankers = ranked.rankers.get(record.id, {})
    keyword_matches = [
        KeywordMatch(term=term, field=field)
        for term, field in (
            retrieval.keyword_matches(query, record.title, record.body)
            if "keyword" in rankers
            else []
        )
    ]
    return SearchHit(
        id=record.id,
        entry_type=record.entry_type,
        title=record.title,
        projects=record.projects,
        created_at=record.created_at,
        lifecycle=_lifecycle(record),
        superseded_by=record.superseded_by,
        work_status=record.work_status,
        tags=record.tags,
        matched_by=_matched_by(rankers),
        keyword_matches=keyword_matches,
        excerpt=_excerpt(record.title, record.body, keyword_matches, vector_match, engine),
    )


def _project_hit(
    project: Project,
    ranked: retrieval.ProjectRanked,
    query: str,
    engine: embeddings.EmbeddingEngine,
) -> ProjectSearchHit:
    framing = project.current_framing
    title = framing.title if framing is not None else ""
    body = framing.body if framing is not None else ""
    rankers = ranked.rankers.get(project.id, {})
    keyword_matches = [
        ProjectKeywordMatch(term=term, field=field)
        for term, field in (
            retrieval.project_keyword_matches(query, project.name, title, body)
            if "keyword" in rankers
            else []
        )
    ]
    return ProjectSearchHit(
        id=project.id,
        name=project.name,
        framing_version=framing.version if framing is not None else None,
        framing_title=framing.title if framing is not None else None,
        matched_by=_matched_by(rankers),
        keyword_matches=keyword_matches,
        excerpt=_project_excerpt(project, keyword_matches, ranked.vector.get(project.id), engine),
        updated_at=framing.created_at if framing is not None else project.created_at,
    )


def _matched_by(rankers: dict[str, int]) -> list[Literal["keyword", "exact", "semantic"]]:
    matched: list[Literal["keyword", "exact", "semantic"]] = []
    if "keyword" in rankers:
        matched.append("keyword")
    if "exact" in rankers:
        matched.append("exact")
    if "vector" in rankers:
        matched.append("semantic")
    return matched


def _lifecycle(record: Record) -> Literal["current", "superseded", "retired"]:
    if record.retired:
        return "retired"
    return "superseded" if record.superseded_by is not None else "current"


def _excerpt(
    title: str,
    body_text: str,
    keyword_matches: list[KeywordMatch],
    vector_match: vector.VectorMatch | None,
    engine: embeddings.EmbeddingEngine,
) -> str | None:
    body = _collapse_whitespace(body_text)
    for match in keyword_matches:
        if match.field == "body" and (span := retrieval.keyword_match_span(body, match.term)):
            return _keyword_excerpt(body, *span)
    if vector_match is None or not body:
        return None
    chunks = vector.chunk_texts(engine, title, body_text)
    if vector_match.chunk_index >= len(chunks):
        return None
    passage = chunks[vector_match.chunk_index].removeprefix(f"{title}\n\n")
    return _collapse_whitespace(passage) or None


def _project_excerpt(
    project: Project,
    keyword_matches: list[ProjectKeywordMatch],
    vector_match: vector.ProjectVectorMatch | None,
    engine: embeddings.EmbeddingEngine,
) -> str | None:
    framing = project.current_framing
    if framing is None:
        return None
    body = _collapse_whitespace(framing.body)
    for match in keyword_matches:
        if match.field == "framing_body" and (
            span := retrieval.keyword_match_span(body, match.term)
        ):
            return _keyword_excerpt(body, *span)
    if vector_match is None or not body:
        return None
    chunks = vector.chunk_texts(engine, framing.title, framing.body)
    if vector_match.chunk_index >= len(chunks):
        return None
    passage = chunks[vector_match.chunk_index].removeprefix(f"{framing.title}\n\n")
    return _collapse_whitespace(passage) or None


def _collapse_whitespace(text: str) -> str:
    return " ".join(text.split())


def _keyword_excerpt(text: str, match_start: int, match_end: int) -> str:
    start = max(0, match_start - _KEYWORD_LEAD_CHARS)
    start = _word_start_inward(text, start, match_start)
    left_clipped = start > 0
    marker_chars = len(_ELLIPSIS) if left_clipped else 0
    available_without_right = SEARCH_EXCERPT_CHARS - marker_chars
    right_clipped = len(text) - start > available_without_right
    content_chars = available_without_right - (len(_ELLIPSIS) if right_clipped else 0)
    if match_end - start > content_chars:
        start = max(0, match_end - content_chars)
        left_clipped = start > 0
        marker_chars = len(_ELLIPSIS) if left_clipped else 0
        content_chars = SEARCH_EXCERPT_CHARS - marker_chars - len(_ELLIPSIS)
        right_clipped = len(text) - start > content_chars
        if not right_clipped:
            content_chars += len(_ELLIPSIS)
    end = min(len(text), start + content_chars)
    if right_clipped:
        end = _word_end_inward(text, match_end, end)
    passage = text[start:end].strip()
    return (_ELLIPSIS if left_clipped else "") + passage + (_ELLIPSIS if right_clipped else "")


def _word_start_inward(text: str, start: int, match_start: int) -> int:
    if start == 0 or text[start - 1] == " ":
        return start
    boundary = text.find(" ", start, match_start)
    return boundary + 1 if boundary >= 0 else start


def _word_end_inward(text: str, match_end: int, end: int) -> int:
    if end >= len(text) or text[end] == " " or text[end - 1] == " ":
        return end
    boundary = text.rfind(" ", match_end, end)
    return boundary if boundary >= match_end else end


def _state(  # noqa: PLR0917 — exact state dimensions
    status: str,
    detail: str,
    unembedded: int | None,
    projects: list[str],
    unscoped: bool,
    total: int,
    project_total: int,
) -> str:
    if status == "ready":
        state = f"state: embeddings ready ({detail}) · {unembedded} unembedded records"
    else:
        state = f"state: embeddings unavailable: {detail} · keyword and exact matching only"
    if projects:
        state += f" · projects {', '.join(projects)}"
    if unscoped:
        state += " · includes unscoped"
    return state + f" · {project_total} project matches · {total} record matches"


def _next_hints(
    arguments: dict[str, Any],
    hits: list[SearchHit],
    project_hits: list[ProjectSearchHit],
    offset: int,
    total: int,
) -> list[str]:
    shown_to = offset + len(hits)
    if shown_to < total:
        return [
            f"{total - shown_to} more records",
            render.call("search", **arguments, offset=shown_to),
        ]
    if hits:
        return [render.call("show", hits[0].id)]
    if project_hits:
        return [render.call("prime", project=project_hits[0].id)]
    had_filters = any(
        arguments.get(name)
        for name in (
            "entry_type",
            "work_status",
            "tags",
            "projects",
            "unscoped",
            "since",
            "until",
            "links",
        )
    )
    if not (had_filters or not arguments["include_history"]):
        return ["nothing left to broaden; stop"]
    return [
        render.call(
            "search",
            query=arguments["query"],
            terms=arguments["terms"],
            include_history=True,
        ),
        "if nothing relevant, stop",
    ]


def _tag_aliases(equivalence: dict[str, tuple[str, ...]], tags: list[str]) -> dict[str, list[str]]:
    aliases = {}
    for tag in tags:
        normal = tag.strip().lower()
        aliases[tag] = [
            item for item in retrieval.expand_tags(equivalence, [normal]) if item != normal
        ]
    return aliases


def _date(name: str, value: str | None) -> date | None:
    if value is None:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise UsageError(f"{name} must be a date written YYYY-MM-DD, not {value!r}") from exc


def _link_filter(value: str | None) -> tuple[Relation, str] | None:
    if value is None:
        return None
    relation, _, target = value.partition(":")
    for known in RELATIONS:
        if relation == known and target.strip():
            return known, target.strip()
    raise UsageError(
        f'links must be "<relation>:<id>" with relation one of {", ".join(RELATIONS)}, not {value!r}'
    )
