"""THE one construction seam for the optional vector runtime.

Every caller on every surface builds its engine here. Nothing else constructs an
`EmbeddingEngine`, and no caller omits the argument to `hygiene.lint` and lets it build
one: the MCP surface used to do both, so a tool invocation could attempt a
tens-of-megabytes ONNX download and no test could inject anything, and the CLI and
the MCP tool ran the same rules against two different engines.

The test suite patches this function to keep the default gate offline.
"""

from __future__ import annotations

from trails import embeddings


def build_engine() -> embeddings.EmbeddingEngine:
    """One construction seam for the optional runtime, injectable in tests."""
    return embeddings.EmbeddingEngine()
