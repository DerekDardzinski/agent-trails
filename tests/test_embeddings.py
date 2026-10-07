"""Tests for the vendored embedding engine.

The tests that download the model are marked `slow` and deselected from the default
gate: a first-run fetch is tens of megabytes, and the gate must stay runnable
offline and without the `search` extra installed.
"""

from __future__ import annotations

import sys

import pytest

from trails.embeddings import (
    MODEL_ID,
    TRUNCATION_LIMIT,
    EmbeddingEngine,
    embedding_available,
)


def test_embedding_available_is_false_without_onnxruntime(monkeypatch: pytest.MonkeyPatch) -> None:
    """The vector arm is an optional extra, and two commands depend on this being a
    RETURN rather than a raise: `trails search` and `trails publish` both have
    to work on a machine without it.

    Injecting None into `sys.modules` makes `import onnxruntime` raise ImportError, so
    this holds whether or not the extra happens to be installed here — a bare
    assertion would pass vacuously in an environment that has it.
    """
    monkeypatch.setitem(sys.modules, "onnxruntime", None)

    assert embedding_available() is False


def test_model_id_is_the_recorded_constant() -> None:
    """Every vector row stores this string, and the query filters on it. Changing it
    makes every previously written row invisible to `similar` and `find` — which reads
    as an empty vector arm rather than as an error, so nothing would report it.
    """
    assert EmbeddingEngine().model_id() == "all-MiniLM-L6-v2/model_O4"
    assert MODEL_ID == "all-MiniLM-L6-v2/model_O4"


@pytest.mark.slow
def test_embed_returns_a_normalised_384_vector() -> None:
    """The schema declares VECTOR(384) and the read path uses cosine distance, so both
    the shape and the unit norm are load-bearing.

    The shape is asserted as the literal tuple rather than by length: a (1, 384)
    result satisfies `len(v) == 384` on the wrong axis and would serialise to a
    one-element vector literal.
    """
    import numpy as np

    vector = EmbeddingEngine().embed("the cross-encoder stage dominates latency")

    assert vector.shape == (384,)
    assert vector.dtype == np.float32
    assert abs(float(np.linalg.norm(vector)) - 1.0) < 1e-5


@pytest.mark.slow
def test_token_count_uses_the_tokenizer_not_a_character_estimate() -> None:
    """The 256-token lint has to count with the tokenizer that truncates.

    The fixture is chosen so the two methods DISAGREE: 300 one-character words is over
    the limit by token count and well under it by the usual four-characters-per-token
    approximation. A rule built on the approximation would call this field in-limit and
    flag long-but-in-limit fields instead, so the truncation cost would look measured
    when it was not.
    """
    text = " ".join(["a"] * 300)
    engine = EmbeddingEngine()

    assert len(text) // 4 < TRUNCATION_LIMIT
    assert engine.token_count(text) > TRUNCATION_LIMIT


@pytest.mark.slow
def test_token_count_is_not_capped_by_the_tokenizer_file_default() -> None:
    """`tokenizer.json` ships its own `max_length: 128`, so a tokenizer loaded and used
    as-is truncates at 128 — BELOW the 256 limit the lint rule checks.

    Without `no_truncation()` every count returns at most 128, the rule can never see a
    field over 256 tokens, and it reports nothing while looking entirely healthy. This
    is asserted against the literal 302 because that is what the tokenizer returns with
    truncation off; re-introducing the cap fails here rather than silently emptying the
    rule. Found by the fixture above failing for the wrong reason.
    """
    count = EmbeddingEngine().token_count(" ".join(["cost"] * 300))

    assert count == 302
