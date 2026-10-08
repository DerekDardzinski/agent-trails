"""The `embed` operation.

NOTE: no `from __future__ import annotations` in this package. See `ops/__init__.py`.
"""

from trails import engine as engine_seam, vector
from trails.ops import context
from trails.ops._maint_types import EmbedResult
from trails.ops._marks import Operation

OPERATION = Operation(toolset="full", read_only=False, destructive=False, idempotent=True)


async def embed() -> EmbedResult:
    """Compute missing similarity embeddings for records and current project framing.

    Use this when: search reports unembedded records or lint reports unembedded framing.

    Use instead: nothing; publish embeds new records, and register_project or update_project
    embeds new framing when the runtime is available.

    Returns: availability plus separate record, framing-version, record-vector, and
    framing-vector counts.
    """
    engine = engine_seam.build_engine()
    if not engine.is_available():
        return EmbedResult(
            available=False,
            records=0,
            framing_versions=0,
            record_vectors=0,
            framing_vectors=0,
        )
    store = context.store()
    rows = store.query(
        "SELECT id FROM record WHERE NOT EXISTS (SELECT 1 FROM record_chunk_embedding AS e "
        "WHERE e.record_id = record.id AND e.model_id = ?) ORDER BY id",
        [engine.model_id()],
    )
    record_ids = [str(row["id"]) for row in rows]
    framing_rows = store.query(
        "SELECT f.project_id, f.version FROM project_framing_version f WHERE NOT EXISTS ("
        "SELECT 1 FROM project_framing_version newer WHERE newer.project_id = f.project_id "
        "AND newer.version > f.version) AND NOT EXISTS (SELECT 1 FROM "
        "project_framing_embedding e WHERE e.project_id = f.project_id AND e.version = "
        "f.version AND e.model_id = ?) ORDER BY f.project_id",
        [engine.model_id()],
    )
    missing_framings = {(str(row["project_id"]), int(row["version"])) for row in framing_rows}
    projects = [
        (project, project.current_framing)
        for project in store.list_projects()
        if project.current_framing is not None
        and (project.id, project.current_framing.version) in missing_framings
    ]
    return EmbedResult(
        available=True,
        records=len(record_ids),
        framing_versions=len(projects),
        record_vectors=vector.embed_records(store, engine, record_ids),
        framing_vectors=vector.embed_project_framings(store, engine, projects),
    )
