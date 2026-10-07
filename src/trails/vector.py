"""The vector arm: chunked embedding of records, and the statements the vector ranker reads.

A record is embedded as one row per chunk in `record_chunk_embedding`. The body is split on
blank lines and consecutive paragraphs are packed into chunks of at most `CHUNK_TOKENS`
tokens, so a long body's tail is not lost to the tokenizer's truncation. Each chunk is
embedded with the record's title in front of it.

Embedded text is passed to the model as authored: no tags, prefixes or expansion, because
storage-time vocabulary expansion helps lexical retrieval and hurts embedding retrieval.

The read side returns SQL statements rather than running them, so the search can send every
ranker in one dolt invocation.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any

from pydantic import BaseModel

from trails import dolt
from trails.embeddings import MODEL_ID, EmbeddingEngine
from trails.errors import ValidationError
from trails.store import Store

# One engine exists. An arbitrary model LABEL is refused, because it would write MiniLM
# vectors under a false name that a query filtering on the engine's own id never reaches.
KNOWN_MODEL_IDS: frozenset[str] = frozenset({MODEL_ID})

# Paragraphs are packed into a chunk while their token counts sum to at most this. Below the
# tokenizer's 256-token truncation, leaving room for the title each chunk is embedded with.
CHUNK_TOKENS = 200

_PARAGRAPH_BREAK = re.compile(r"\n\s*\n")
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")


class VectorMatch(BaseModel):
    """One record's nearest chunk to a query."""

    record_id: str
    cosine: float  # 1 - Dolt's COSINE distance
    chunk_index: int  # 0-based
    chunk_count: int  # chunk rows the record has for this model


def chunk_texts(engine: EmbeddingEngine, title: str, body: str) -> list[str]:
    """The texts to embed for one record, one per chunk, each starting with the title.

    Consecutive paragraphs are packed while their token counts sum to at most `CHUNK_TOKENS`.
    A PARAGRAPH LONGER THAN `CHUNK_TOKENS` IS SPLIT AT SENTENCE ENDS (".", "!" or "?" followed
    by whitespace) into pieces of at most `CHUNK_TOKENS`, each its own chunk, because the
    tokenizer truncates at 256 tokens and the tail of one long chunk would be invisible to
    similarity search. A single sentence still over the limit is split between words. Token
    counts of pieces are summed, which can overstate a joined count slightly and never
    understates it by more than the tokenizer's merges across a space. An empty body gives the
    title alone.
    """
    paragraphs = [part.strip() for part in _PARAGRAPH_BREAK.split(body) if part.strip()]
    if not paragraphs:
        return [title]
    chunks: list[list[str]] = []
    used = 0
    for paragraph in paragraphs:
        tokens = engine.token_count(paragraph)
        if tokens > CHUNK_TOKENS:
            chunks.extend([piece] for piece in _split_long(engine, paragraph))
            used = CHUNK_TOKENS  # nothing is packed onto a split paragraph's last piece
        elif chunks and used + tokens <= CHUNK_TOKENS:
            chunks[-1].append(paragraph)
            used += tokens
        else:
            chunks.append([paragraph])
            used = tokens
    return [f"{title}\n\n" + "\n\n".join(chunk) for chunk in chunks]


def _split_long(engine: EmbeddingEngine, paragraph: str) -> list[str]:
    """`paragraph` as pieces of at most CHUNK_TOKENS, cut at sentence ends, else at spaces,
    else, for one whitespace-free run longer than CHUNK_TOKENS such as a base64 blob, by
    halving it by characters until each half fits."""
    units: list[tuple[str, int]] = []
    for sentence in _SENTENCE_END.split(paragraph):
        count = engine.token_count(sentence)
        if count <= CHUNK_TOKENS:
            units.append((sentence, count))
        else:
            for word in sentence.split():
                units.extend(_halved(engine, word))
    pieces: list[list[str]] = []
    used = 0
    for text, count in units:
        if pieces and used + count <= CHUNK_TOKENS:
            pieces[-1].append(text)
            used += count
        else:
            pieces.append([text])
            used = count
    return [" ".join(piece) for piece in pieces]


def _halved(engine: EmbeddingEngine, word: str) -> list[tuple[str, int]]:
    """`word` with its token count, or its two character halves split the same way."""
    count = engine.token_count(word)
    if count <= CHUNK_TOKENS or len(word) < 2:
        return [(word, count)]
    middle = len(word) // 2
    return [*_halved(engine, word[:middle]), *_halved(engine, word[middle:])]


def embed_record(store: Store, engine: EmbeddingEngine, record_id: str) -> int:
    """Embed one record. Returns rows written; 0 when the engine is unavailable."""
    return embed_records(store, engine, [record_id])


def embed_records(
    store: Store,
    engine: EmbeddingEngine,
    record_ids: Sequence[str],
    *,
    model_id: str | None = None,
) -> int:
    """Embed many records, one row per chunk, with a bounded number of Dolt subprocesses.

    Reads the records in one `get_records` call, the existing chunk rows in one query, and
    writes every new vector in one batched upsert. A record already embedded under this model
    is skipped, which makes a second run return 0 without reaching `CALL DOLT_COMMIT` (which
    errors on a transaction that changed nothing).

    Raises:
        ValidationError: `record_ids` is a string rather than a sequence of ids, or
            `model_id` names a model this engine cannot produce.
    """
    if isinstance(record_ids, str):
        # A `str` IS a `Sequence[str]`: iterating it would embed nothing and return 0, which
        # reads the same as a store that was already fully embedded.
        raise ValidationError(
            f"record_ids must be a sequence of ids, not the string {record_ids!r}"
        )
    if not record_ids or not engine.is_available():
        return 0
    selected_model = model_id or engine.model_id()
    if selected_model not in KNOWN_MODEL_IDS:
        known = ", ".join(sorted(KNOWN_MODEL_IDS))
        raise ValidationError(
            f"unknown embedding model {selected_model!r}; this engine can produce: {known}"
        )

    unique_ids = tuple(dict.fromkeys(record_ids))
    records = store.get_records(unique_ids)
    placeholders = ", ".join("?" * len(unique_ids))
    embedded = {
        str(row["record_id"])
        for row in store.query(
            "SELECT DISTINCT record_id FROM record_chunk_embedding WHERE model_id = ? "
            f"AND record_id IN ({placeholders})",
            [selected_model, *unique_ids],
        )
    }
    rows: list[tuple[str, int, str, Sequence[float]]] = []
    for record_id in unique_ids:
        record = records.get(record_id)
        if record is None or record_id in embedded:
            continue
        for index, text in enumerate(chunk_texts(engine, record.title, record.body)):
            rows.append((record_id, index, selected_model, engine.embed(text)))
    return store.upsert_chunk_embeddings(rows)


def nearest_statement(query_vector: Sequence[float], model_id: str, where: str) -> str:
    """Every chunk row of `model_id` whose record passes `where`, nearest first.

    `where` is a SQL condition over the record aliased `r`. Each row carries its record's
    chunk count and created_at, so a vector-only hit can break a fused-score tie by date. Brute force: distance over a few thousand rows is cheap, and Dolt's vector
    index is not relied on.
    """
    vector = dolt.sql_literal(dolt.vector_literal(query_vector))
    model = dolt.sql_literal(model_id)
    return (
        "SELECT e.record_id AS record_id, e.chunk_index AS chunk_index, "
        "r.created_at AS created_at, "
        f"DISTANCE(e.vec, STRING_TO_VECTOR({vector}), 'COSINE') AS distance, "
        "(SELECT COUNT(*) FROM record_chunk_embedding c "
        "WHERE c.record_id = e.record_id AND c.model_id = e.model_id) AS chunks "
        "FROM record_chunk_embedding e JOIN record r ON r.id = e.record_id "
        f"WHERE e.model_id = {model} AND {where} "
        "ORDER BY distance ASC, e.record_id ASC, e.chunk_index ASC"
    )


def parse_nearest(rows: Sequence[dict[str, Any]], depth: int) -> list[VectorMatch]:
    """The first `depth` records of `nearest_statement` rows, each with its best chunk.

    Rows arrive nearest first, so a record's first row is its best chunk. Dolt's COSINE
    distance is 1 - cosine, unclipped, so the conversion keeps the full [-1, 1] range.
    """
    matches: list[VectorMatch] = []
    if depth <= 0:
        return matches
    seen: set[str] = set()
    for row in rows:
        record_id = str(row["record_id"])
        if record_id in seen:
            continue
        seen.add(record_id)
        matches.append(
            VectorMatch(
                record_id=record_id,
                cosine=1.0 - float(row["distance"]),
                chunk_index=int(row["chunk_index"]),
                chunk_count=int(row["chunks"]),
            )
        )
        if len(matches) == depth:
            break
    return matches


def unembedded_statement(model_id: str, project: str | None = None) -> str:
    """The count, as column `n`, of records with no chunk row for `model_id`.

    Counted within `project` when given, so the number agrees with lint's unembedded
    category for that project; over the whole store otherwise.
    """
    scope = f" AND r.project = {dolt.sql_literal(project)}" if project is not None else ""
    return (
        "SELECT COUNT(*) AS n FROM record r WHERE NOT EXISTS (SELECT 1 FROM "
        "record_chunk_embedding e WHERE e.record_id = r.id AND e.model_id = "
        f"{dolt.sql_literal(model_id)}){scope}"
    )
