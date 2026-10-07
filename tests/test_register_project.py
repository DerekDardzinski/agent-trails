"""Tests for the register_project operation, against real Dolt in a temporary directory."""

from __future__ import annotations

import asyncio
import socket
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from tests.fakes import FakeEngine, basis
from trails import engine as engine_seam
from trails.models import ProjectPath
from trails.ops import context
from trails.ops.prime import prime
from trails.ops.register_project import register_project
from trails.store import Store

AWS_KEY = "AKIAIOSFODNN7EXAMPLE"


def _run(store: Store, **arguments: Any) -> Any:
    async def go() -> Any:
        return await context.bind(register_project, lambda: store.db_path)(**arguments)

    return asyncio.run(go())


def _dolt_commits(store: Store) -> int:
    return int(store.query("SELECT COUNT(*) AS n FROM dolt_log")[0]["n"])


def _tree(root: Path) -> list[str]:
    return sorted(str(path.relative_to(root)) for path in root.rglob("*"))


def test_register_creates_nothing_on_disk_and_warns_on_a_missing_path(
    store: Store, tmp_path: Path
) -> None:
    """labbook-79y.10 criterion 4. Catches a directory, repository or file created at a
    registered path, a missing local path or an s3 URI registered silently, a secret in the
    access instructions unreported, and a rerun that writes a second project context or a Dolt commit."""
    code = tmp_path / "code"
    code.mkdir()
    (code / "a.py").write_text("x = 1\n")
    missing = tmp_path / "not-yet"
    before = _tree(tmp_path)
    paths = [
        ProjectPath(path=str(code), label="analysis code"),
        ProjectPath(path=str(missing)),
        ProjectPath(path="s3://bucket/mint/", access_instructions=f"key {AWS_KEY}"),
    ]

    first = _run(store, name="mint", paths=paths)

    host = socket.gethostname()
    assert _tree(tmp_path) == before
    assert first.warnings == [
        f"{missing} does not exist on {host}",
        "s3://bucket/mint/: not checked (s3 URI)",
        "possible secret: AWSKeyDetector in s3://bucket/mint/ access_instructions line 1",
    ]
    project = store.get_project("mint")
    assert project is not None
    assert [(p.path, p.label, p.host) for p in project.paths] == [
        (str(code), "analysis code", host),
        (str(missing), "", host),
        ("s3://bucket/mint/", "", ""),
    ]
    project_context = store.get_record(first.project_context_id)
    assert (
        project_context.entry_type,
        project_context.work_status,
        project_context.tags,
        project_context.project,
        project_context.title,
    ) == (
        "project-context",
        "open",
        [],
        "mint",
        "mint",
    )
    commits = _dolt_commits(store)

    second = _run(store, name="mint", paths=paths)

    assert (second.created, second.project_context_created, second.project_context_id) == (
        False,
        False,
        first.project_context_id,
    )
    assert _dolt_commits(store) == commits
    assert second.render_text().splitlines()[-1] == f'Next: show("{first.project_context_id}")'


def test_editing_paths_adds_updates_and_removes(store: Store, tmp_path: Path) -> None:
    """labbook-79y.10 section 5. Catches an update stored as a second row, a removal ignored,
    and ~ left unexpanded so the existence check looks for a literal "~" directory."""
    _run(store, name="mint", paths=[ProjectPath(path=str(tmp_path), label="old")])

    result = _run(
        store,
        name="mint",
        paths=[ProjectPath(path=str(tmp_path), label="new"), ProjectPath(path="~")],
        remove_paths=["/never/registered"],
    )
    removed = _run(store, name="mint", remove_paths=[str(tmp_path)])

    assert {(p.path, p.label, state) for p, state in result.paths} == {
        (str(tmp_path), "new", "exists"),
        (str(Path.home()), "", "exists"),
    }
    assert [p.path for p, _ in removed.paths] == [str(Path.home())]


class _AnyText(FakeEngine):
    """Embeds every text to one basis vector."""

    def embed(self, text: str) -> Sequence[float]:
        self.calls.append(text)
        return basis(0)


def test_register_embeds_the_project_context_it_writes(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """labbook-79y.6 item 18. Catches the project context left as the one unembedded record in a new
    project, which every search state line then reports."""
    monkeypatch.setattr(engine_seam, "build_engine", _AnyText)

    result = _run(store, name="proj")

    rows = store.query(
        "SELECT COUNT(*) AS n FROM record_chunk_embedding WHERE record_id = ?",
        [result.project_context_id],
    )
    assert rows == [{"n": 1}]


def test_register_replaces_a_retired_project_context(store: Store) -> None:
    """Catches register_project returning a retired context that prime correctly hides."""
    first = _run(store, name="demo")
    store.retire(first.project_context_id, "context withdrawn")

    second = _run(store, name="demo")

    async def read_prime() -> Any:
        return await context.bind(prime, lambda: store.db_path)(project="demo")

    state = asyncio.run(read_prime())

    assert second.project_context_created is True
    assert second.project_context_id != first.project_context_id
    assert store.get_record(first.project_context_id).retired is True
    assert [record.id for record in state.project_contexts] == [second.project_context_id]
