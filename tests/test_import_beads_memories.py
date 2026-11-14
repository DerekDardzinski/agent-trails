"""Tests for scripts/import_beads_memories.py, against a stub `bd` on PATH.

The stub prints a fixture object, so no test runs `bd` against a real Beads database.
"""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
from types import ModuleType

import pytest

from trails import ops
from trails.entry_store import EntryStore

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "import_beads_memories.py"
AWS_KEY = "AKIAIOSFODNN7EXAMPLE"
MEMORIES = {
    "pattern-examples-must-be-code-derivable": "Every example output comes from code.",
    "tooling-dolt-version": "Dolt 2.4.0 since 2026-10-01.",
    "arch-red-account": f"Red account key {AWS_KEY}.",
}


def _script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("import_beads_memories", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def stub_bd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A `bd` that prints MEMORIES for `bd memories --json`, first on PATH."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fixture = tmp_path / "memories.json"
    # The shape bd 1.0.3 prints: the memories beside an integer schema_version.
    fixture.write_text(json.dumps({"schema_version": 1, **MEMORIES}))
    stub = bin_dir / "bd"
    stub.write_text(f'#!/bin/sh\n[ "$1 $2" = "memories --json" ] || exit 9\ncat "{fixture}"\n')
    stub.chmod(0o755)
    # Prepended, so the stub shadows any real `bd` while `dolt` stays reachable.
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    source = tmp_path / "source"
    source.mkdir()
    return source


def test_import_creates_one_tagged_note_per_memory_and_a_rerun_creates_none(
    store: EntryStore, stub_bd: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Silent failures: real bd output refused for its schema_version field, so nothing is
    imported; a rerun duplicating every memory; a missing prefix tag; a flagged secret dropped
    or hidden instead of stored and reported."""
    script = _script()
    argv = [str(stub_bd), "--db", str(store.db_path)]

    first = script.main(argv)
    first_out = capsys.readouterr().out
    second = script.main(argv)
    second_out = capsys.readouterr().out

    assert (first, second) == (0, 0)
    assert first_out == (
        "created 3, skipped 0\npossible secret in arch-red-account: AWSKeyDetector\n"
    )
    assert second_out == "created 0, skipped 3\n"
    rows = store.query("SELECT id FROM entry")
    records = store.get_entries([str(row["id"]) for row in rows])
    assert sorted((r.current.title, r.current.body, r.current.tags) for r in records.values()) == [
        ("arch-red-account", f"Red account key {AWS_KEY}.", ["arch", "imported-memory"]),
        (
            "pattern-examples-must-be-code-derivable",
            "Every example output comes from code.",
            ["imported-memory", "pattern"],
        ),
        ("tooling-dolt-version", "Dolt 2.4.0 since 2026-10-01.", ["imported-memory", "tooling"]),
    ]


def test_the_import_is_not_an_operation() -> None:
    """Silent failure: the one-off script reachable as a tool on either surface."""
    assert "import_beads_memories" not in ops.operation_module_names()


def test_malformed_bd_output_exits_2_and_a_dash_key_gets_no_empty_tag(
    store: EntryStore, stub_bd: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """labbook-79y.6 item 10. Silent failures: malformed bd output reported with the same
    exit as a store failure, and a key starting with "-" yielding an empty tag that the
    store refuses, which stops this run and every rerun at that key."""
    script = _script()
    fixture = tmp_path / "memories.json"
    argv = [str(stub_bd), "--db", str(store.db_path)]

    fixture.write_text("[1, 2]")
    malformed = script.main(argv)
    fixture.write_text(json.dumps({"-leading": "Text."}))
    dashed = script.main(argv)

    assert (malformed, dashed) == (2, 0)
    assert capsys.readouterr().err == (
        "error: bd memories --json did not print an object of key to text\n"
    )
    [record] = store.get_entries(
        [str(r["id"]) for r in store.query("SELECT id FROM entry")]
    ).values()
    assert (record.kind, record.current.title, record.current.tags) == (
        "note",
        "-leading",
        ["imported-memory"],
    )
