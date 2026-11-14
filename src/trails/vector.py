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
from trails.models import Project, ProjectFramingVersion
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


class ProjectVectorMatch(BaseModel):
    """One project's nearest chunk from its current framing version."""

    project_id: str
    version: int
    cosine: float
    chunk_index: int
    chunk_count: int


def embed_project_framings(
    store: Store,
    engine: EmbeddingEngine,
    projects: Sequence[tuple[Project, ProjectFramingVersion]],
    *,
    model_id: str | None = None,
) -> int:
    """Embed supplied Project/ProjectFramingVersion pairs that lack rows for this model."""
    if not projects or not engine.is_available():
        return 0
    selected_model = model_id or engine.model_id()
    if selected_model not in KNOWN_MODEL_IDS:
        known = ", ".join(sorted(KNOWN_MODEL_IDS))
        raise ValidationError(
            f"unknown embedding model {selected_model!r}; this engine can produce: {known}"
        )
    conditions = " OR ".join("(project_id = ? AND version = ?)" for _item in projects)
    params: list[str | int] = [selected_model]
    for project, framing in projects:
        params.extend([project.id, framing.version])
    existing = {
        (str(row["project_id"]), int(row["version"]))
        for row in store.query(
            "SELECT DISTINCT project_id, version FROM project_framing_embedding "
            f"WHERE model_id = ? AND ({conditions})",
            params,
        )
    }
    rows: list[tuple[str, int, int, str, Sequence[float]]] = []
    for project, framing in projects:
        if (project.id, framing.version) in existing:
            continue
        for index, text in enumerate(chunk_texts(engine, framing.title, framing.body)):
            rows.append((project.id, framing.version, index, selected_model, engine.embed(text)))
    return store.insert_project_framing_embeddings(rows)


def project_nearest_statement(query_vector: Sequence[float], model_id: str) -> str:
    """Current framing chunks of `model_id`, nearest first."""
    vector = dolt.sql_literal(dolt.vector_literal(query_vector))
    model = dolt.sql_literal(model_id)
    return (
        "SELECT e.project_id, e.version, e.chunk_index, f.created_at, "
        f"DISTANCE(e.vec, STRING_TO_VECTOR({vector}), 'COSINE') AS distance, "
        "(SELECT COUNT(*) FROM project_framing_embedding c WHERE c.project_id = "
        "e.project_id AND c.version = e.version AND c.model_id = e.model_id) AS chunks "
        "FROM project_framing_embedding e JOIN project_framing_version f ON "
        "f.project_id = e.project_id AND f.version = e.version WHERE e.model_id = "
        f"{model} AND NOT EXISTS (SELECT 1 FROM project_framing_version newer WHERE "
        "newer.project_id = f.project_id AND newer.version > f.version) "
        "ORDER BY distance ASC, e.project_id ASC, e.chunk_index ASC"
    )


def parse_project_nearest(rows: Sequence[dict[str, Any]], depth: int) -> list[ProjectVectorMatch]:
    """The first `depth` projects from current-framing nearest rows."""
    matches: list[ProjectVectorMatch] = []
    seen: set[str] = set()
    for row in rows:
        project_id = str(row["project_id"])
        if project_id in seen:
            continue
        seen.add(project_id)
        matches.append(
            ProjectVectorMatch(
                project_id=project_id,
                version=int(row["version"]),
                cosine=1.0 - float(row["distance"]),
                chunk_index=int(row["chunk_index"]),
                chunk_count=int(row["chunks"]),
            )
        )
        if len(matches) == depth:
            break
    return matches


def unembedded_framing_statement(model_id: str) -> str:
    """Count current framing versions without an embedding row for `model_id`."""
    return (
        "SELECT COUNT(*) AS n FROM project_framing_version f WHERE NOT EXISTS (SELECT 1 "
        "FROM project_framing_version newer WHERE newer.project_id = f.project_id AND "
        "newer.version > f.version) AND NOT EXISTS (SELECT 1 FROM "
        "project_framing_embedding e WHERE e.project_id = f.project_id AND e.version = "
        f"f.version AND e.model_id = {dolt.sql_literal(model_id)})"
    )
