"""Fixed facts about where a referenced file lives: a temporary directory, or code by suffix.

Read-only, local, and judgment-free: every answer is a fixed rule over a path string and the
local filesystem. Used by publish to warn when the code or data behind a record sits where a
later reader cannot find it.
"""

from __future__ import annotations

import re
import tempfile
from collections.abc import Iterable
from pathlib import Path

# Suffixes that name a script or source file, compared case-insensitively. A fixed list, so a
# data file is never mistaken for code by guesswork; a script with an unlisted suffix simply
# gets the data wording.
CODE_SUFFIXES = frozenset(
    {
        ".py",
        ".ipynb",
        ".sql",
        ".sh",
        ".bash",
        ".zsh",
        ".r",
        ".rmd",
        ".jl",
        ".js",
        ".mjs",
        ".ts",
        ".scala",
        ".java",
        ".kt",
        ".go",
        ".rs",
        ".rb",
        ".pl",
        ".m",
        ".do",
    }
)

# An absolute path inside free text. The look-behind keeps the path part of a URI
# ("s3://bucket/tmp/x", "https://host/tmp/x") and a relative path from matching.
_TEXT_PATH = re.compile(r"(?<![\w.~:/-])/[^\s`'\"<>()\[\]{},;|*]+")


def temporary_roots() -> tuple[Path, ...]:
    """Directories whose contents the operating system may delete, resolved.

    /tmp and /var/tmp, and on macOS /var/folders, which holds every user's $TMPDIR, plus
    whatever `tempfile.gettempdir()` reports here.
    """
    candidates = ["/tmp", "/var/tmp", "/var/folders", tempfile.gettempdir()]
    roots: list[Path] = []
    for candidate in candidates:
        resolved = Path(candidate).resolve()
        if resolved not in roots:
            roots.append(resolved)
    return tuple(roots)


def temporary_root(path: Path, roots: Iterable[Path] | None = None) -> Path | None:
    """The temporary directory `path` lies under, or None. `path` need not exist."""
    try:
        resolved = path.expanduser().resolve()
    except (OSError, RuntimeError):
        return None
    for root in temporary_roots() if roots is None else roots:
        if resolved == root or resolved.is_relative_to(root):
            return root
    return None


def is_code(path: Path) -> bool:
    """Whether the file name ends in one of CODE_SUFFIXES."""
    return path.suffix.lower() in CODE_SUFFIXES


def temporary_paths_in_text(text: str) -> list[str]:
    """Absolute paths in `text` that lie under a temporary directory, in first-seen order.

    Trailing sentence punctuation is not part of a path.
    """
    roots = temporary_roots()
    found: list[str] = []
    for match in _TEXT_PATH.finditer(text):
        candidate = match.group(0).rstrip(".:!?")
        if candidate not in found and temporary_root(Path(candidate), roots) is not None:
            found.append(candidate)
    return found
