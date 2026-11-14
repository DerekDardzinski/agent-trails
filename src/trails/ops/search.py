"""The `search` operation.

NOTE: no `from __future__ import annotations` in this package. See `ops/__init__.py`.
"""

from collections.abc import Sequence
from typing import Annotated

from pydantic import Field

from trails import embeddings, engine as engine_seam, entry_search, retrieval
from trails.errors import UsageError
from trails.ops import context
from trails.ops._entry_types import ProjectHit, SearchHit, SearchResult
from trails.ops._marks import Operation
from trails.present import evidence_users, node_state, standing
from trails.vocab import Kind, SearchStatus

PROJECT_HIT_LIMIT = 3

OPERATION = Operation(toolset="agent", read_only=True, destructive=False, idempotent=True)


async def search(  # noqa: PLR0917 — public query contract
    query: Annotated[str, Field(description="What you are looking for, in plain words.")] = "",
    kinds: Annotated[list[Kind], Field(description="Keep only these kinds.")] = [],  # noqa: B006
    projects: Annotated[
        list[str], Field(description="Keep entries in any of these project ids or names.")
    ] = [],  # noqa: B006
    tags: Annotated[
        list[str], Field(description="Keep entries carrying every tag, or an alias of it.")
    ] = [],  # noqa: B006
    status: Annotated[
        SearchStatus | None,
        Field(
            description=(
                "Goals and tasks only: open, claimed or closed, or the outcome of a close "
                "(completed, dropped, failed, superseded)."
            )
        ),
    ] = None,
    open_comments: Annotated[
        bool, Field(description="Keep only entries with a comment still waiting for a response.")
    ] = False,
    include_retired: Annotated[bool, Field(description="Also return retired entries.")] = False,
    limit: Annotated[int, Field(description="Most entry hits to return.")] = 10,
    offset: Annotated[int, Field(description="Entry hits to skip.")] = 0,
) -> SearchResult:
    """Find entries by meaning, exact id or filters, over current versions; for an unfiltered query, also find projects.

    Use this when: you need to know whether something is already recorded, before writing it again, or to list entries by kind, project, tag or task state.

    Use instead: show, once you have an id; ready, for the next task; prime, to orient in a project.

    Returns: up to three project matches, then one page of entries, each with its state, a claim's standing (observation or assertion, supported, contradicted), the open comment count, for a note the entries that use it as evidence, and an excerpt. Notes rank below other kinds.
    """
    if limit < 1:
        raise UsageError(f"limit must be at least 1, not {limit}")
    if offset < 0:
        raise UsageError(f"offset must be 0 or more, not {offset}")
    store = context.store()
    refs = store.resolve_projects(projects)
    filters = entry_search.EntryFilters(
        kinds=kinds,
        projects=[ref.id for ref in refs],
        tags=tags,
        status=status,
        open_comments=open_comments,
        include_retired=include_retired,
    )
    engine = engine_seam.build_engine()
    engine_state, _detail = embeddings.engine_status(engine)
    query_vector: Sequence[float] | None = None
    if engine_state == "ready" and query.strip():
        try:
            query_vector = engine.embed(query)
        except (embeddings.ModelNotAvailableError, embeddings.ModelDownloadError):
            engine_state = "unavailable"
    model_id = engine.model_id() if engine_state == "ready" else None
    equivalence = retrieval.tag_equivalence(store) if tags else {}
    ranked = entry_search.run_entry_search(
        store,
        filters,
        query=query,
        query_vector=query_vector,
        model_id=model_id,
        equivalence=equivalence,
    )
    page = ranked.order[offset : offset + limit]
    found = store.get_entries(page)
    incoming = store.incoming_links(page, ["supports", "contradicts", "references"])
    counts = store.open_comment_counts(page)
    graph = store.task_graph()
    hits = []
    for entry_id in page:
        entry = found.get(entry_id)
        if entry is None:
            continue
        links = incoming.get(entry_id, [])
        hits.append(
            SearchHit(
                id=entry.id,
                kind=entry.kind,
                version=entry.current.version,
                title=entry.current.title,
                state=node_state(graph, entry.id)
                if entry.id in graph.nodes
                else ("retired" if entry.retired else entry.kind),
                tags=entry.current.tags,
                projects=[ref.id for ref in entry.projects],
                standing=standing(entry, links),
                evidence_for=evidence_users(entry, links),
                open_comments=counts.get(entry.id, 0),
                matched=sorted(ranked.rankers.get(entry_id, {})),
                excerpt=entry_search.excerpt(entry.current.body, query),
            )
        )
    project_hits: list[ProjectHit] = []
    project_total = 0
    if query.strip() and not (kinds or projects or tags or status or open_comments):
        project_ranked = retrieval.run_project_search(
            store, query=query, query_vector=query_vector, model_id=model_id
        )
        project_total = len(project_ranked.order)
        listed = {project.id: project for project in store.list_projects()}
        for project_id in project_ranked.order[:PROJECT_HIT_LIMIT]:
            project = listed.get(project_id)
            if project is not None:
                project_hits.append(
                    ProjectHit(
                        id=project.id,
                        name=project.name,
                        framing_title=(
                            project.current_framing.title if project.current_framing else None
                        ),
                    )
                )
    return SearchResult(
        query=query,
        projects=project_hits,
        project_total=project_total,
        hits=hits,
        total=len(ranked.order),
        offset=offset,
    )
