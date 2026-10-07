"""The `embed` operation.

NOTE: no `from __future__ import annotations` in this package. See `ops/__init__.py`.
"""

from trails import engine as engine_seam, vector
from trails.ops import context
from trails.ops._maint_types import EmbedResult
from trails.ops._marks import Operation

OPERATION = Operation(toolset="full", read_only=False, destructive=False, idempotent=True)


async def embed() -> EmbedResult:
    """Compute similarity embeddings for records that have none for the current model.

    Use this when: search's state line reports unembedded records.

    Use instead: nothing; publish embeds new records automatically when the model is available.

    Returns: the number of embedding vectors written, one per chunk of each record embedded.
    """
    engine = engine_seam.build_engine()
    if not engine.is_available():
        return EmbedResult(available=False, records=0, vectors=0)
    store = context.store()
    rows = store.query(
        "SELECT id FROM record WHERE NOT EXISTS (SELECT 1 FROM record_chunk_embedding AS e "
        "WHERE e.record_id = record.id AND e.model_id = ?) ORDER BY id",
        [engine.model_id()],
    )
    ids = [str(row["id"]) for row in rows]
    vectors = vector.embed_records(store, engine, ids)
    return EmbedResult(available=True, records=len(ids), vectors=vectors)
