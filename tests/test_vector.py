"""Tests for chunked embedding, the vector statements and the embedding status."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import pytest

from tests.fakes import FakeEngine, basis
from trails import dolt, embeddings, vector
from trails.embeddings import MODEL_ID, EmbeddingEngine, engine_status
from trails.errors import ValidationError
from trails.models import RecordDraft
from trails.store import Store


class _Engine(FakeEngine):
    """Model present; every text embeds to the same basis vector."""

    def model_present(self) -> bool:
        return True

    def embed(self, text: str) -> Sequence[float]:
        self.calls.append(text)
        return basis(0)


def _chunk_rows(store: Store, record_id: str) -> list[int]:
    rows = store.query(
        "SELECT chunk_index FROM record_chunk_embedding WHERE record_id = ? ORDER BY chunk_index",
        [record_id],
    )
    return [int(row["chunk_index"]) for row in rows]


def test_long_body_is_written_as_one_row_per_chunk(store: Store) -> None:
    """Catches a long body embedded as one truncated chunk, which leaves its tail unsearchable.

    FakeEngine counts words as tokens: three 150-word paragraphs cannot share a 200-token
    chunk, so each is its own chunk.
    """
    body = "\n\n".join(" ".join([word] * 150) for word in ("alpha", "beta", "gamma"))
    record_id = store.write_record(
        RecordDraft(entry_type="note", work_status="closed", title="Long", body=body)
    ).record.id
    engine = _Engine()

    written = vector.embed_records(store, engine, [record_id])

    assert written == 3
    assert _chunk_rows(store, record_id) == [0, 1, 2]
    assert [text.split("\n\n")[0] for text in engine.calls] == ["Long", "Long", "Long"]
    assert [text.split("\n\n")[1][:5] for text in engine.calls] == ["alpha", "beta ", "gamma"]


def test_short_paragraphs_are_packed_into_one_chunk(store: Store) -> None:
    """Catches one row per paragraph, which splits a short record into fragments."""
    record_id = store.write_record(
        RecordDraft(
            entry_type="note",
            work_status="closed",
            title="Short",
            body="one two\n\nthree four\n\n\nfive",
        )
    ).record.id
    engine = _Engine()

    vector.embed_records(store, engine, [record_id])

    assert engine.calls == ["Short\n\none two\n\nthree four\n\nfive"]


def test_second_embed_run_writes_nothing(store: Store) -> None:
    """Catches a rerun opening an empty transaction (DOLT_COMMIT raises 'nothing to commit')
    or writing duplicate rows."""
    record_id = store.write_record(
        RecordDraft(entry_type="note", work_status="closed", title="Once", body="text")
    ).record.id
    engine = _Engine()
    vector.embed_records(store, engine, [record_id])

    assert vector.embed_records(store, engine, [record_id]) == 0
    assert _chunk_rows(store, record_id) == [0]


def test_upsert_refuses_a_wrong_width_vector_and_round_trips_a_vector(store: Store) -> None:
    """Catches a mis-serialized vector being stored: the vector read back through the
    ranker's own statement must sit at cosine 1 from itself, with its chunk count."""
    record_id = store.write_record(
        RecordDraft(entry_type="note", work_status="closed", title="Vec")
    ).record.id
    with pytest.raises(ValidationError):
        store.upsert_chunk_embeddings([(record_id, 0, MODEL_ID, [0.0] * 383)])

    store.upsert_chunk_embeddings(
        [(record_id, 0, MODEL_ID, basis(5)), (record_id, 1, MODEL_ID, basis(6))]
    )
    rows = dolt.read_result_sets(
        store.db_path, [vector.nearest_statement(basis(6), MODEL_ID, "1 = 1")]
    )[0]

    assert vector.parse_nearest(rows, depth=10) == [
        vector.VectorMatch(record_id=record_id, cosine=1.0, chunk_index=1, chunk_count=2)
    ]


def test_engine_status_reports_missing_model_without_downloading(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches a reading operation's status check fetching the model."""
    monkeypatch.setattr(embeddings, "embedding_available", lambda: True)
    models = tmp_path / "models"
    models.mkdir()

    status = engine_status(EmbeddingEngine(models_dir=models))

    assert status == ("unavailable", "model not downloaded; run embed")
    assert list(models.iterdir()) == []


def test_engine_status_is_ready_with_both_files_and_unavailable_without_dependencies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches model_present looking for the files at the wrong path (every read would then
    skip the vector ranker), and missing dependencies reported as a missing model."""
    models = tmp_path / "models"
    models.mkdir()
    (models / "model_O4.onnx").write_bytes(b"")
    (models / "tokenizer.json").write_text("{}")
    engine = EmbeddingEngine(models_dir=models)

    monkeypatch.setattr(embeddings, "embedding_available", lambda: True)
    assert engine_status(engine) == ("ready", "all-MiniLM-L6-v2/model_O4")

    monkeypatch.setattr(embeddings, "embedding_available", lambda: False)
    assert engine_status(engine) == ("unavailable", "dependencies not installed")


def test_paragraphs_summing_to_exactly_the_chunk_limit_share_one_chunk() -> None:
    """labbook-79y.6 item 12. Catches `<=` turned into `<`, which splits a body that fits."""
    body = " ".join(["a"] * 100) + "\n\n" + " ".join(["b"] * 100)

    assert len(vector.chunk_texts(_Engine(), "T", body)) == 1
    assert len(vector.chunk_texts(_Engine(), "T", body + " b")) == 2


def test_a_long_paragraph_is_split_at_sentence_ends_and_a_long_sentence_between_words(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """labbook-79y.6 item 13. Catches a paragraph over the limit embedded as one chunk, whose
    tail the tokenizer cuts off and similarity search can then never reach."""
    monkeypatch.setattr(vector, "CHUNK_TOKENS", 4)
    body = "One two. Three four. Five six seven eight nine.\n\nTen."

    assert vector.chunk_texts(_Engine(), "T", body) == [
        "T\n\nOne two. Three four.",
        "T\n\nFive six seven eight",
        "T\n\nnine.",
        "T\n\nTen.",
    ]


class _CharEngine(_Engine):
    """Counts one token per character, as a tokenizer does on a whitespace-free blob."""

    def token_count(self, text: str) -> int:
        return len(text)


def test_a_whitespace_free_run_over_the_limit_is_split_by_characters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """labbook-79y.9 item 11d. Catches a base64 blob or a long URL kept as one piece over the
    limit, whose tail the tokenizer cuts off so similarity search never reaches it."""
    monkeypatch.setattr(vector, "CHUNK_TOKENS", 4)

    chunks = vector.chunk_texts(_CharEngine(), "T", "abcdefghij")

    assert chunks == ["T\n\nab", "T\n\ncde", "T\n\nfg", "T\n\nhij"]


def test_parse_nearest_with_no_depth_returns_nothing() -> None:
    """labbook-79y.6 item 12. Catches the depth check after the append, which returns one
    match at depth 0 and every match at a negative depth."""
    rows = [{"record_id": "rec-a", "distance": 0.1, "chunk_index": 0, "chunks": 1}]

    assert vector.parse_nearest(rows, 0) == []
    assert vector.parse_nearest(rows, -1) == []


def test_the_unembedded_count_is_nonzero_and_scoped_to_a_project(store: Store) -> None:
    """labbook-79y.6 items 12 and 18. Catches a count that is always 0, and a project's
    state line reporting another project's unembedded records."""
    store.write_record(
        RecordDraft(entry_type="note", work_status="closed", title="In p", project="p")
    )
    store.write_record(
        RecordDraft(entry_type="note", work_status="closed", title="In q", project="q")
    )
    store.write_record(
        RecordDraft(entry_type="note", work_status="closed", title="Also q", project="q")
    )

    def count(project: str | None) -> int:
        statement = vector.unembedded_statement(MODEL_ID, project)
        return int(dolt.read_rows(store.db_path, statement)[0]["n"])

    assert (count(None), count("p"), count("q")) == (3, 1, 2)
