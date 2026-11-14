"""Fixed facts about where a referenced file lives: a temporary directory, or code by suffix.

Read-only, local, and judgment-free: every answer is a fixed rule over a path string and the
local filesystem. Used by write to warn when the code or data behind an entry sits where a
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
    """Directories whose contents the operating system may delete, resolved first.

    /tmp and /var/tmp, and on macOS /var/folders, which holds every user's $TMPDIR, plus
    whatever `tempfile.gettempdir()` reports here, each in its resolved and literal spelling.
    """
    candidates = ["/tmp", "/var/tmp", "/var/folders", tempfile.gettempdir()]
    roots: list[Path] = []
    for candidate in candidates:
        # Both spellings: the resolved one (/private/tmp on macOS) matches a path that
        # resolves, and the literal one (/tmp) matches a path that cannot be resolved, such
        # as one through a symlink loop.
        for spelling in (Path(candidate).resolve(), Path(candidate).absolute()):
            if spelling not in roots:
                roots.append(spelling)
    return tuple(roots)


def safe_resolve(path: Path) -> Path:
    """`path` resolved, or made absolute without resolving when resolution fails.

    Python 3.12 raises RuntimeError on a symlink loop and OSError on some unreadable
    components; a caller after a durable write must not raise for either.
    """
    try:
        return path.expanduser().resolve()
    except (OSError, RuntimeError):
        return path.absolute()


def temporary_root(path: Path, roots: Iterable[Path] | None = None) -> Path | None:
    """The temporary directory `path` lies strictly under, or None. `path` need not exist.

    The root itself is not inside it: a record that names /tmp, as in a rule never to write
    there, names no file that can be lost.
    """
    resolved = safe_resolve(path)
    for root in temporary_roots() if roots is None else roots:
        if resolved != root and resolved.is_relative_to(root):
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
