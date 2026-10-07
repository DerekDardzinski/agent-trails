"""Tests that the embedding engine is built at the one library seam.

`trails.engine.build_engine` is the only place an `EmbeddingEngine` is constructed, so a test
can inject one and no operation can trigger a model download through a path the suite cannot
patch. The publish operation's use of the seam is asserted in tests/test_ops_write.py.
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC_ROOT = Path(__file__).resolve().parent.parent / "src" / "trails"


def _modules_constructing_an_engine(root: Path) -> set[str]:
    """Every module under `root` that calls `EmbeddingEngine(...)`."""
    building = set()
    for path in sorted(root.rglob("*.py")):
        for node in ast.walk(ast.parse(path.read_text())):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if name == "EmbeddingEngine":
                building.add(path.stem)
    return building


def test_only_the_seam_constructs_an_embedding_engine() -> None:
    """Catches a new caller reintroducing a direct construction.

    Both surfaces keep working when one does, so nothing else notices that the seam has
    quietly stopped being one. The expected set is a hardcoded literal so a new bypass site
    fails rather than passes.
    """
    assert _modules_constructing_an_engine(SRC_ROOT) == {"engine"}
