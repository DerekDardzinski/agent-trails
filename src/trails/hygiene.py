"""Hygiene primitives: the near-duplicate tag check and the research-ref listing.

`lint` reads these. Everything here is local: tags in memory, and git run against trails' own
capture store.
"""

from __future__ import annotations

import subprocess
from collections.abc import Sequence
from difflib import SequenceMatcher
from pathlib import Path

NEAR_DUPLICATE_RATIO = 0.85
RESEARCH_REF_PREFIX = "refs/research/"


def _near_duplicates(tags: Sequence[str]) -> tuple[tuple[str, str], ...]:
    """Tag pairs close enough to be the same idea spelled two ways.

    `difflib` rather than anything learned: no LLM appears anywhere in the store, and a
    deterministic ratio is reproducible between runs, which a model is not.
    """
    found = [
        (first, second)
        for index, first in enumerate(tags)
        for second in tags[index + 1 :]
        if SequenceMatcher(None, first, second).ratio() >= NEAR_DUPLICATE_RATIO
    ]
    return tuple(sorted(found))


def research_refs(store: Path) -> list[str] | None:
    """The record ids named by `refs/research/<id>/<n>` refs in the capture store, sorted.

    None when the store cannot be read as a git repository, which is a different statement
    from "no refs". A store that does not exist yet holds no refs and gives [].
    """
    if not store.exists():
        return []
    listed = _git(store, "for-each-ref", "--format=%(refname)", RESEARCH_REF_PREFIX)
    if listed is None:
        return None
    return sorted(
        {
            line.removeprefix(RESEARCH_REF_PREFIX).split("/", 1)[0]
            for line in listed.splitlines()
            if line.strip()
        }
    )


def _git(repo: Path, *args: str) -> str | None:
    """Stripped stdout of one git command in the bare repository `repo`, or None on failure."""
    try:
        completed = subprocess.run(
            ["git", f"--git-dir={repo}", *args], capture_output=True, text=True, check=False
        )
    except OSError:
        return None
    return completed.stdout.strip() if completed.returncode == 0 else None
