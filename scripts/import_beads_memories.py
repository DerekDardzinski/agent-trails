"""Import Beads memories (`bd remember`) into trails as note entries.

A one-off script, NOT an operation: it is outside `trails.ops`, so it is not discovered by
`ops.operations()`, appears in no toolset, and has no tool description.

    uv run python scripts/import_beads_memories.py <source_dir> [--db <path>]

Runs `bd memories --json` in <source_dir> (no shell), whose output is one JSON object mapping
each memory key to its text, plus a top-level `schema_version`, which is skipped. Each entry
becomes a note: title = key, body = text, and tags contain `imported-memory` plus the
key's prefix before its first `-` (arch, pattern, tooling, util, ...). A key whose title already
exists on an imported-memory note is skipped. A possible secret never stops a write: the entry
is stored and its key is printed with the detector names.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from trails.entry_store import EntryStore, open_entry_store
from trails.errors import TrailsError
from trails.models import EntryDraft


def import_memories(
    store: EntryStore, memories: dict[str, str]
) -> tuple[int, int, list[tuple[str, list[str]]]]:
    """Write each memory not already imported.

    Returns (created, skipped, flagged), where flagged lists each created key whose entry
    carries possible secrets, with the detector names.
    """
    existing = {
        str(row["title"])
        for row in store.query(
            "SELECT x.title FROM entry AS e JOIN entry_version AS x ON x.entry_id = e.id "
            "JOIN version_tag AS t ON t.entry_id = x.entry_id AND t.version = x.version "
            "WHERE e.kind = 'note' AND t.tag = 'imported-memory'"
        )
    }
    created, skipped = 0, 0
    flagged: list[tuple[str, list[str]]] = []
    for key, text in memories.items():
        if key in existing:
            skipped += 1
            continue
        tags = ["imported-memory"]
        prefix = key.split("-", 1)[0] if "-" in key else ""
        if prefix:  # a key starting with "-" has no prefix; an empty tag would be refused
            tags.append(prefix)
        outcome = store.write_entry(EntryDraft(kind="note", title=key, body=text, tags=tags))
        created += 1
        existing.add(key)
        if outcome.secret_findings:
            detectors = sorted({finding.detector for finding in outcome.secret_findings})
            flagged.append((key, detectors))
    return created, skipped, flagged


class MalformedOutputError(TrailsError):
    """bd ran but printed something other than an object of key to text."""


def _read_memories(source_dir: Path) -> dict[str, str]:
    """`bd memories --json` in source_dir, checked to be an object of strings.

    Raises:
        TrailsError: bd failed to run or exited non-zero.
        MalformedOutputError: bd's output was not JSON, or not an object of key to text.
    """
    completed = subprocess.run(
        ["bd", "memories", "--json"],
        cwd=source_dir,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise TrailsError(f"bd memories --json failed: {completed.stderr.strip()}")
    try:
        memories = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise MalformedOutputError(f"bd memories --json did not print JSON: {exc}") from exc
    if isinstance(memories, dict):
        # bd 1.0.3 prints `"schema_version": 1` beside the memories; it is metadata, not a memory.
        memories.pop("schema_version", None)
    if not isinstance(memories, dict) or not all(
        isinstance(key, str) and isinstance(value, str) for key, value in memories.items()
    ):
        raise MalformedOutputError("bd memories --json did not print an object of key to text")
    return memories


def main(argv: list[str] | None = None) -> int:
    """Run the import. Exit 0 on success, 2 when bd's output was malformed, 1 when bd or the
    store failed."""
    parser = argparse.ArgumentParser(description="Import Beads memories into trails.")
    parser.add_argument("source_dir", type=Path, help="Directory to run `bd memories` in.")
    parser.add_argument("--db", type=Path, default=None, help="Database path (default $TRAILS_DB).")
    arguments = parser.parse_args(argv)
    try:
        memories = _read_memories(arguments.source_dir)
        created, skipped, flagged = import_memories(open_entry_store(arguments.db), memories)
    except MalformedOutputError as exc:
        sys.stderr.write(f"error: {exc}\n")
        return 2
    except (TrailsError, OSError) as exc:
        sys.stderr.write(f"error: {exc}\n")
        return 1
    print(f"created {created}, skipped {skipped}")
    for key, detectors in flagged:
        print(f"possible secret in {key}: {', '.join(detectors)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
