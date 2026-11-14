"""Shared fixtures."""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Callable, Iterator
from pathlib import Path

import pytest

from trails.entry_store import EntryStore
from trails.store import Store, open_store


def _git(path: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(path), *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return completed.stdout.strip()


@pytest.fixture(scope="session")
def initialised_db(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """One initialised database, created once for the whole session.

    `dolt init` plus the CREATE TABLEs is several subprocess round trips; paying it per test
    would dominate the suite. Tests get a COPY of this (see `store`) rather than sharing it,
    because several assertions count rows and would otherwise see each other's writes.
    """
    template = tmp_path_factory.mktemp("template") / "db"
    open_store(template)
    return template


@pytest.fixture
def store(initialised_db: Path, tmp_path: Path) -> Iterator[Store]:
    destination = tmp_path / "db"
    shutil.copytree(initialised_db, destination)
    yield open_store(destination)


@pytest.fixture(scope="session")
def initialised_entry_db(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """One database with the entry schema, created once; tests get a copy (see `entries`)."""
    from trails.entry_store import open_entry_store

    template = tmp_path_factory.mktemp("entry-template") / "db"
    open_entry_store(template)
    return template


@pytest.fixture
def entries(initialised_entry_db: Path, tmp_path: Path) -> Iterator[EntryStore]:
    from trails.entry_store import open_entry_store

    destination = tmp_path / "entry-db"
    shutil.copytree(initialised_entry_db, destination)
    yield open_entry_store(destination)


@pytest.fixture(autouse=True)
def no_harness_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every write resolves its harness and session from the process tree, and a test run
    inside Kiro or Claude Code would otherwise record the developer's real session. The process
    table is the external boundary, so it is empty by default and every write records the
    harness `unknown`. Session tests set their own table and home directory."""
    from trails import sessions

    monkeypatch.setattr(sessions, "process_table", dict)


@pytest.fixture
def make_git_repo(tmp_path: Path) -> Callable[[str], Path]:
    """Create a git repository with one commit and a local identity.

    The identity is set repo-locally so the commit succeeds on a host with no
    global git config, which would otherwise fail the whole suite for a reason
    unrelated to anything under test.
    """

    def factory(name: str = "repo") -> Path:
        repo = tmp_path / name
        repo.mkdir(parents=True)
        _git(repo, "init", "-q", ".")
        _git(repo, "config", "user.email", "test@example.invalid")
        _git(repo, "config", "user.name", "trails tests")
        (repo / "tracked.txt").write_text("original\n")
        _git(repo, "add", "-A")
        _git(repo, "commit", "-qm", "initial")
        return repo

    return factory


@pytest.fixture
def git_repo(make_git_repo: Callable[[str], Path]) -> Path:
    return make_git_repo("repo")


@pytest.fixture
def git_cmd() -> Callable[..., str]:
    """Run a git command in a given repository, raising on failure."""
    return _git


@pytest.fixture(autouse=True)
def prevent_implicit_cli_model_download(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the default test gate offline even though dev installs the search extra.

    A write embeds when dependencies are importable and downloads the model on first use.
    Most tests exercise records, not embeddings; letting them hit the real engine
    would put a tens-of-megabytes network fetch on the DEFAULT gate and make ordinary tests
    depend on HuggingFace availability. The model is a true external boundary, so default it
    unavailable at the one construction seam. Vector tests opt in with their own FakeEngine,
    and slow tests opt in with the real engine. Library-level embedding tests are unaffected.
    """
    from tests.fakes import FakeEngine
    from trails import engine as engine_seam

    # `trails.engine.build_engine` is THE seam: every operation reaches the runtime
    # through it. Patching anywhere else would silently stop working — the operations would
    # reach the real engine and the default gate would start downloading a model.
    monkeypatch.setattr(engine_seam, "build_engine", lambda: FakeEngine(available=False))


@pytest.fixture(autouse=True)
def no_temporary_roots_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every pytest `tmp_path` lies under the OS temp directory, so with the real roots every
    artifact and repository a test creates would draw a temporary-directory warning, and
    tests that assert an exact warning list would fail for a reason unrelated to them. Tests
    of that warning patch `trails.locations.temporary_roots` to a root of their own, and one
    test checks the real roots directly."""
    from trails import locations

    monkeypatch.setattr(locations, "temporary_roots", lambda: ())
