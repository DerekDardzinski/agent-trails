"""The `search` operation.

NOTE: no `from __future__ import annotations` in this package. See `ops/__init__.py`.
"""

from collections.abc import Sequence
from datetime import date
from typing import Annotated, Any, Literal

from pydantic import Field

from trails import embeddings, engine as engine_seam, render, retrieval, vector
from trails.errors import UsageError
from trails.models import Record
from trails.ops import context
from trails.ops._marks import Operation
from trails.ops._read_types import KeywordMatch, SearchHit, SearchResult
from trails.vocab import RELATIONS, EntryType, Relation, WorkStatus

_DEFAULT_LIMIT = 10

OPERATION = Operation(toolset="agent", read_only=True, destructive=False, idempotent=True)


async def search(  # noqa: PLR0917 — every parameter is an operation parameter
    query: Annotated[str, Field(description="Natural-language description of what you want.")] = "",
    terms: Annotated[
        list[str],
        Field(
            description=(
                "Exact strings to look for in titles, bodies, tags, values, artifact "
                "locations, captured repository paths and commit shas, and link targets, "
                "ignoring case. An absolute local path also searches its resolved form, so "
                "`/tmp` can find `/private/tmp` on macOS. Put short identifiers such as v2, "
                "AB or p5 here: strings under 3 characters match only as whole words, and "
                "the keyword search cannot see them."
            )
        ),
    ] = [],  # noqa: B006 — contracted default; never mutated
    tags: Annotated[
        list[str],
        Field(
            description=(
                "Keep only records carrying every one of these tags. A tag also matches its "
                "aliases."
            )
        ),
    ] = [],  # noqa: B006 — contracted default; never mutated
    entry_type: Annotated[
        EntryType | None,
        Field(description="Keep only records of this entry type."),
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
    project: Annotated[str | None, Field(description="Keep only records in this project.")] = None,
    since: Annotated[
        str | None,
        Field(description="Keep only records created on or after this date, YYYY-MM-DD."),
    ] = None,
    until: Annotated[
        str | None,
        Field(description="Keep only records created on or before this date, YYYY-MM-DD."),
    ] = None,
    links: Annotated[
        str | None,
        Field(
            description=(
                '"<relation>:<id>" keeps records linked to or from that id by that relation, '
                'for example "contradicts:hyp-k3m9z".'
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
    limit: Annotated[int, Field(description="Maximum records to return.")] = _DEFAULT_LIMIT,
    offset: Annotated[int, Field(description="Number of records to skip, for paging.")] = 0,
) -> SearchResult:
    """Find records by meaning, by exact strings, or by filters, in one call. Common English function words are left out of keyword matching. Combines that keyword search, an exact-string search over records, their artifacts, captured repository paths and commits, and links (ids, paths, S3 URIs, commit shas, metric names, and short identifiers passed in terms), and a similarity search over record text.

    Use this when: you need to know whether the store holds something: a procedure, a past result, a decision, a fact, or anything that mentions or links to a given id or path.

    Use instead: show, once you have an id; prime, for an overview; lineage, for what a record was built on.

    Returns: one compact preview per record with its id, entry type, title, project, creation time, lifecycle, latest work status, tags, which search methods matched it, any located title or body keyword matches, and a body excerpt when one is known. A body-keyword match shows a window of at most 240 characters centered on the first match. A semantic match with no body-keyword match shows the complete selected embedding chunk. Full records and retrieval scores remain outside search; use show for the complete record. The similarity search contributes at most its 10 nearest records with cosine at least 0.25 internally; exact strings and non-stopword keyword matches have no cosine floor. When nothing matches, it lists the filters applied and what removing each one would find. Its one broader retry drops every restrictive filter and includes retired records, and never repeats the same call. After that retry finds nothing relevant, stop: the store does not hold it.
    """
    if limit < 1:
        raise UsageError(f"limit must be at least 1, not {limit}")
    if offset < 0:
        raise UsageError(f"offset must be 0 or more, not {offset}")
    filters = retrieval.Filters(
        entry_type=entry_type,
        work_status=work_status,
        tags=tags,
        project=project,
        since=_date("since", since),
        until=_date("until", until),
        links=_link_filter(links),
        include_history=include_history,
    )

    store = context.store()
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

    relaxed_firsts: list[tuple[str, int, str]] = []
    if total == 0:
        for name in filters.given():
            found = run(filters.without(name)).order
            if found:
                relaxed_firsts.append((name, len(found), found[0]))
    records = store.get_records([*page, *(first for _, _, first in relaxed_firsts)])

    hits = [_hit(records[record_id], ranked, query, engine) for record_id in page]
    received = {
        "entry_type": entry_type,
        "work_status": work_status,
        "tags": tags,
        "project": project,
        "since": since,
        "until": until,
        "links": links,
    }
    arguments: dict[str, Any] = {"query": query, "terms": terms, **received}
    arguments["include_history"] = include_history
    if limit != _DEFAULT_LIMIT:
        arguments["limit"] = limit

    return SearchResult(
        state=_state(status, detail, ranked.unembedded, project, total),
        hits=hits,
        total=total,
        offset=offset,
        filters={name: value for name, value in received.items() if value},
        relaxed=[
            (name, count, f"{first}, {records[first].entry_type}")
            for name, count, first in relaxed_firsts
        ],
        tag_aliases=_tag_aliases(equivalence, tags) if total == 0 else {},
        next=_next_hints(arguments, hits, offset, total),
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
    matched_by: list[Literal["keyword", "exact", "semantic"]] = []
    if "keyword" in rankers:
        matched_by.append("keyword")
    if "exact" in rankers:
        matched_by.append("exact")
    if "vector" in rankers:
        matched_by.append("semantic")
    return SearchHit(
        id=record.id,
        entry_type=record.entry_type,
        title=record.title,
        project=record.project,
        created_at=record.created_at,
        lifecycle=_lifecycle(record),
        superseded_by=record.superseded_by,
        work_status=record.work_status,
        tags=record.tags,
        matched_by=matched_by,
        keyword_matches=keyword_matches,
        excerpt=_excerpt(record, keyword_matches, vector_match, engine),
    )


def _lifecycle(record: Record) -> Literal["current", "superseded", "retired"]:
    if record.retired:
        return "retired"
    if record.superseded_by is not None:
        return "superseded"
    return "current"


def _excerpt(
    record: Record,
    keyword_matches: list[KeywordMatch],
    vector_match: vector.VectorMatch | None,
    engine: embeddings.EmbeddingEngine,
) -> str | None:
    body = _collapse_whitespace(record.body)
    for match in keyword_matches:
        if match.field != "body":
            continue
        span = retrieval.keyword_match_span(body, match.term)
        if span is not None:
            return _keyword_excerpt(body, *span)
    if vector_match is None or not body:
        return None
    chunks = vector.chunk_texts(engine, record.title, record.body)
    if vector_match.chunk_index >= len(chunks):
        return None
    passage = chunks[vector_match.chunk_index].removeprefix(f"{record.title}\n\n")
    return _collapse_whitespace(passage) or None


def _collapse_whitespace(text: str) -> str:
    """Replace every whitespace run with one ASCII space and strip the ends."""
    return " ".join(text.split())


def _keyword_excerpt(text: str, match_start: int, match_end: int) -> str:
    """A body window with up to 80 characters before the match and markers inside the cap."""
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


def _state(
    status: str, detail: str, unembedded: int | None, project: str | None, total: int
) -> str:
    if status == "ready":
        state = f"state: embeddings ready ({detail}) · {unembedded} unembedded"
    else:
        state = f"state: embeddings unavailable: {detail} · keyword and exact matching only"
    if project is not None:
        state += f" · project {project}"
    return state + f" · {total} {'match' if total == 1 else 'matches'}"


def _next_hints(
    arguments: dict[str, Any], hits: list[SearchHit], offset: int, total: int
) -> list[str]:
    """More results: the offset call. Otherwise the first hit's show() call. No match:
    drop every restrictive filter and include history for one broader search; when history is
    already included and no restrictive filter exists, say there is nothing left to broaden
    instead of repeating the same call."""
    shown_to = offset + len(hits)
    if total == 0:
        had_filters = any(
            arguments.get(name)
            for name in (
                "entry_type",
                "work_status",
                "tags",
                "project",
                "since",
                "until",
                "links",
            )
        )
        can_broaden = had_filters or not arguments["include_history"]
        if not can_broaden:
            return ["nothing left to broaden; stop"]
        broader = render.call(
            "search",
            query=arguments["query"],
            terms=arguments["terms"],
            include_history=True,
        )
        return [broader, "if nothing relevant, stop"]
    if not hits:
        return [render.call("search", **arguments)]
    if shown_to < total:
        return [f"{total - shown_to} more", render.call("search", **arguments, offset=shown_to)]
    return [render.call("show", hits[0].id)]


def _tag_aliases(equivalence: dict[str, tuple[str, ...]], tags: list[str]) -> dict[str, list[str]]:
    aliases = {}
    for tag in tags:
        normal = tag.strip().lower()
        aliases[tag] = [o for o in retrieval.expand_tags(equivalence, [normal]) if o != normal]
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
        f'links must be "<relation>:<id>" with relation one of {", ".join(RELATIONS)}, '
        f"not {value!r}"
    )
