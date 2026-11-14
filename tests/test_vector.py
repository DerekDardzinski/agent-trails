"""Tests for chunked embedding, the vector statements and the embedding status."""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import pytest

from tests.fakes import FakeEngine, basis
from trails import embeddings, entry_search, vector
from trails.embeddings import MODEL_ID, EmbeddingEngine, engine_status
from trails.entry_store import EntryStore
from trails.errors import ValidationError
from trails.models import EntryDraft


class _Engine(FakeEngine):
    """Model present; every text embeds to the same basis vector."""

    def model_present(self) -> bool:
        return True

    def embed(self, text: str) -> Sequence[float]:
        self.calls.append(text)
        return basis(0)


def _chunk_rows(store: EntryStore, entry_id: str) -> list[tuple[int, int]]:
    rows = store.query(
        "SELECT version, chunk_index FROM entry_chunk_embedding WHERE entry_id = ? "
        "ORDER BY version, chunk_index",
        [entry_id],
    )
    return [(int(row["version"]), int(row["chunk_index"])) for row in rows]


def _note(store: EntryStore, title: str, body: str = "") -> str:
    return store.write_entry(EntryDraft(kind="note", title=title, body=body)).entry.id


def test_long_body_is_written_as_one_row_per_chunk(store: EntryStore) -> None:
    """Catches a long body embedded as one truncated chunk, which leaves its tail unsearchable.

    FakeEngine counts words as tokens: three 150-word paragraphs cannot share a 200-token
    chunk, so each is its own chunk.
    """
    body = "\n\n".join(" ".join([word] * 150) for word in ("alpha", "beta", "gamma"))
    entry_id = _note(store, "Long", body)
    engine = _Engine()

    written = entry_search.embed_entries(store, engine, [entry_id])

    assert written == 3
    assert _chunk_rows(store, entry_id) == [(1, 0), (1, 1), (1, 2)]
    assert [text.split("\n\n")[0] for text in engine.calls] == ["Long", "Long", "Long"]
    assert [text.split("\n\n")[1][:5] for text in engine.calls] == ["alpha", "beta ", "gamma"]


def test_short_paragraphs_are_packed_into_one_chunk(store: EntryStore) -> None:
    """Catches one row per paragraph, which splits a short entry into fragments."""
    entry_id = _note(store, "Short", "one two\n\nthree four\n\n\nfive")
    engine = _Engine()

    entry_search.embed_entries(store, engine, [entry_id])

    assert engine.calls == ["Short\n\none two\n\nthree four\n\nfive"]


def test_second_embed_run_writes_nothing_and_a_new_version_is_embedded(store: EntryStore) -> None:
    """Catches a rerun opening an empty transaction (DOLT_COMMIT raises 'nothing to commit'),
    and an edited entry staying findable only by what its old version said."""
    entry_id = _note(store, "Once", "text")
    engine = _Engine()
    entry_search.embed_entries(store, engine, [entry_id])

    assert entry_search.embed_entries(store, engine, [entry_id]) == 0
    store.write_entry(EntryDraft(id=entry_id, base_version=1, reason="edit", body="new text"))
    assert entry_search.unembedded_ids(store, MODEL_ID) == [entry_id]
    assert entry_search.embed_entries(store, engine, [entry_id]) == 1
    assert _chunk_rows(store, entry_id) == [(1, 0), (2, 0)]


def test_a_wrong_width_vector_is_refused_and_a_vector_round_trips(store: EntryStore) -> None:
    """Catches a mis-serialized vector being stored: the vector read back through the
    ranker's own statement must sit at cosine 1 from itself, with its chunk count."""

    class Narrow(_Engine):
        def embed(self, text: str) -> Sequence[float]:
            return [0.0] * 383

    entry_id = _note(store, "Vec", "first paragraph")
    with pytest.raises(ValidationError, match="383 components"):
        entry_search.embed_entries(store, Narrow(), [entry_id])

    class Fixed(_Engine):
        def embed(self, text: str) -> Sequence[float]:
            return basis(6)

    entry_search.embed_entries(store, Fixed(), [entry_id])
    ranked = entry_search.run_entry_search(
        store,
        entry_search.EntryFilters(),
        query="",
        query_vector=basis(6),
        model_id=MODEL_ID,
        equivalence={},
    )

    assert ranked.vector == {
        entry_id: vector.VectorMatch(record_id=entry_id, cosine=1.0, chunk_index=0, chunk_count=1)
    }


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
