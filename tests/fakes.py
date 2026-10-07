"""Shared deterministic embedding test double.

It subclasses the real engine so its signatures are type-checked, but overrides every
method that could import optional dependencies. Default-gate tests therefore never
reach onnxruntime, tokenizers or numpy even when the host happens to have them.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from trails.embeddings import MODEL_ID, EmbeddingEngine


def basis(index: int, *, sign: float = 1.0) -> list[float]:
    """A 384-dimensional unit basis vector."""
    values = [0.0] * 384
    values[index] = sign
    return values


class FakeEngine(EmbeddingEngine):
    """Map exact input strings to vectors and record every embedding call."""

    def __init__(
        self,
        vectors: Mapping[str, Sequence[float]] | None = None,
        *,
        model: str = MODEL_ID,
        available: bool = True,
        failure: Exception | None = None,
        availability_failure: Exception | None = None,
    ) -> None:
        # Do not call the real initializer: none of its lazy runtime state is used.
        self.vectors = dict(vectors or {})
        self.model = model
        self.available = available
        self.failure = failure
        self.availability_failure = availability_failure
        self.calls: list[str] = []

    def is_available(self) -> bool:
        if self.availability_failure is not None:
            raise self.availability_failure
        return self.available

    def model_id(self) -> str:
        return self.model

    def model_present(self) -> bool:
        # The real method reads a models directory this fake never sets up, and raised
        # AttributeError for any test that routed the fake through engine_status.
        return self.available

    def embed(self, text: str) -> Sequence[float]:
        self.calls.append(text)
        if self.failure is not None:
            raise self.failure
        return self.vectors[text]

    def token_count(self, text: str) -> int:
        # No optional import. This is a TOKEN-count test double, not a character
        # approximation: the real-tokenizer assertion is slow-tested separately.
        return len(text.split())
