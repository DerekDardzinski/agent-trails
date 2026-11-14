"""The `embed` operation.

NOTE: no `from __future__ import annotations` in this package. See `ops/__init__.py`.
"""

from trails import engine as engine_seam, entry_search, vector
from trails.ops import context
from trails.ops._maint_types import EmbedResult
from trails.ops._marks import Operation

OPERATION = Operation(toolset="full", read_only=False, destructive=False, idempotent=True)


async def embed() -> EmbedResult:
    """Compute missing similarity embeddings for current entry versions and current project framing.

    Use this when: lint reports unembedded entries or framing, for example after installing the search extra.

    Use instead: nothing; write embeds each version it writes, and register_project or update_project embeds new framing, when the runtime is available.

    Returns: availability plus separate entry, framing-version, entry-vector and framing-vector counts.
    """
    engine = engine_seam.build_engine()
    if not engine.is_available():
        return EmbedResult(
            available=False, entries=0, framing_versions=0, entry_vectors=0, framing_vectors=0
        )
    store = context.store()
    entry_ids = entry_search.unembedded_ids(store, engine.model_id())
    framing_rows = store.query(
        "SELECT f.project_id, f.version FROM project_framing_version f WHERE NOT EXISTS ("
        "SELECT 1 FROM project_framing_version newer WHERE newer.project_id = f.project_id "
        "AND newer.version > f.version) AND NOT EXISTS (SELECT 1 FROM "
        "project_framing_embedding e WHERE e.project_id = f.project_id AND e.version = "
        "f.version AND e.model_id = ?) ORDER BY f.project_id",
        [engine.model_id()],
    )
    missing = {(str(row["project_id"]), int(row["version"])) for row in framing_rows}
    projects = [
        (project, project.current_framing)
        for project in store.list_projects()
        if project.current_framing is not None
        and (project.id, project.current_framing.version) in missing
    ]
    return EmbedResult(
        available=True,
        entries=len(entry_ids),
        framing_versions=len(projects),
        entry_vectors=entry_search.embed_entries(store, engine, entry_ids),
        framing_vectors=vector.embed_project_framings(store, engine, projects),
    )
