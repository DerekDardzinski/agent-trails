"""Tests for stable project registration and append-only framing against real Dolt."""

from __future__ import annotations

import asyncio
import fcntl
import socket
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from tests.fakes import FakeEngine, basis
from trails import engine as engine_seam
from trails.entry_store import EntryStore as Store
from trails.errors import ValidationError
from trails.models import ProjectFramingDraft, ProjectPath
from trails.ops import context
from trails.ops.register_project import register_project
from trails.ops.revise_framing import revise_framing
from trails.ops.set_paths import set_paths

AWS_KEY = "AKIAIOSFODNN7EXAMPLE"


def _run(store: Store, **arguments: Any) -> Any:
    async def go() -> Any:
        return await context.bind(register_project, lambda: store.db_path)(**arguments)

    return asyncio.run(go())


def _call(store: Store, operation: Any, **arguments: Any) -> Any:
    async def go() -> Any:
        return await context.bind(operation, lambda: store.db_path)(**arguments)

    return asyncio.run(go())


def _update(store: Store, **arguments: Any) -> Any:
    """set_paths for paths and remove_paths, then revise_framing for framing, as the old
    update_project did in one call; returns the last result."""
    project = arguments.pop("project")
    result = None
    path_args = {k: arguments.pop(k) for k in ("paths", "remove_paths") if k in arguments}
    if path_args or not arguments:
        result = _call(store, set_paths, project=project, **path_args)
    if "framing" in arguments:
        result = _call(store, revise_framing, project=project, **arguments)
    return result


def _dolt_commits(store: Store) -> int:
    return int(store.query("SELECT COUNT(*) AS n FROM dolt_log")[0]["n"])


def _tree(root: Path) -> list[str]:
    return sorted(str(path.relative_to(root)) for path in root.rglob("*"))


def test_register_creates_stable_identity_without_a_context_entry(
    store: Store, tmp_path: Path
) -> None:
    """Catches registration creating a template entry, changing id on retry, touching a path,
    or hiding path and secret warnings."""
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
    commits = _dolt_commits(store)
    second = _run(store, name="mint", paths=paths)
    lookup = _run(store, name="mint")

    host = socket.gethostname()
    assert _tree(tmp_path) == before
    assert first.project.id.startswith("prj-")
    assert first.project == second.project == lookup.project
    assert (first.created, second.created, lookup.created) == (True, False, False)
    assert (first.changed, second.changed, lookup.changed) == (True, False, False)
    assert first.framing is second.framing is lookup.framing is None
    assert _dolt_commits(store) == commits
    assert store.query("SELECT COUNT(*) AS n FROM entry") == [{"n": 0}]
    assert first.warnings == [
        f"{missing} does not exist on {host}",
        "s3://bucket/mint/: not checked (s3 URI)",
        "possible secret: AWSKeyDetector in s3://bucket/mint/ access_instructions line 1",
    ]
    assert first.render_text().splitlines()[-1] == (
        f'Next: prime(project="{first.project.id}") · '
        f'create_task(title="...", projects=["{first.project.id}"])'
    )


def test_first_framing_registration_retry_returns_version_one_without_writes(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches an identical first-framing retry adding a version, embedding, path, or commit."""
    first = _run(
        store,
        name="First retry",
        paths=[ProjectPath(path="s3://bucket/original")],
        framing=ProjectFramingDraft(title="Initial purpose", body="Serve devices.\r\n"),
    )
    commits_before_retry = _dolt_commits(store)
    engine = FakeEngine()
    monkeypatch.setattr(engine_seam, "build_engine", lambda: engine)

    retry = _run(
        store,
        name="First retry",
        paths=[ProjectPath(path="s3://bucket/original")],
        framing=ProjectFramingDraft(title="Initial purpose", body="Serve devices.\n"),
    )

    project = store.get_project(first.project.id)
    assert project is not None
    assert retry.project == first.project
    assert retry.changed is False
    assert retry.framing is not None and retry.framing.version == 1
    assert retry.framing_created is False
    assert "no change detected" in retry.render_text()
    assert [item.path for item in project.paths] == ["s3://bucket/original"]
    assert [item.version for item in store.project_framing_history(project.id)] == [1]
    assert store.query(
        "SELECT COUNT(*) AS n FROM project_framing_embedding WHERE project_id = ?",
        [project.id],
    ) == [{"n": 0}]
    assert engine.calls == []
    assert _dolt_commits(store) == commits_before_retry


def test_stale_registration_paths_refuse_without_mutation(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches stale creation paths being silently dropped or changing current project state."""
    first = _run(
        store,
        name="Stale registration paths",
        paths=[ProjectPath(path="s3://bucket/original")],
        framing=ProjectFramingDraft(title="Initial purpose", body="Serve devices."),
    )
    _update(
        store,
        project=first.project.id,
        paths=[ProjectPath(path="s3://bucket/current")],
        remove_paths=["s3://bucket/original"],
    )
    commits_before_refusal = _dolt_commits(store)
    engine = FakeEngine()
    monkeypatch.setattr(engine_seam, "build_engine", lambda: engine)

    with pytest.raises(ValidationError) as caught:
        _run(
            store,
            name="Stale registration paths",
            paths=[ProjectPath(path="s3://bucket/original")],
            framing=ProjectFramingDraft(title="Initial purpose", body="Serve devices."),
        )

    assert str(caught.value) == (
        f"the project name 'Stale registration paths' is taken by {first.project.id}, which "
        "has other paths or framing; a name names one project"
    )
    stored = store.get_project(first.project.id)
    assert stored is not None
    assert [item.path for item in stored.paths] == ["s3://bucket/current"]
    assert [item.version for item in store.project_framing_history(stored.id)] == [1]
    assert engine.calls == []
    assert _dolt_commits(store) == commits_before_refusal


def test_registration_path_equality_is_order_independent_after_local_normalization(
    store: Store,
) -> None:
    """Catches equivalent normalized paths refusing because caller order or spelling changed."""
    home_path = Path("~/trails-registration-normalization-test").expanduser()
    first = _run(
        store,
        name="Equivalent registration paths",
        paths=[
            ProjectPath(path="  ~/trails-registration-normalization-test  ", label="code"),
            ProjectPath(path="s3://bucket/data", access_instructions="read-only role"),
        ],
    )
    commits_before_retry = _dolt_commits(store)

    retry = _run(
        store,
        name="Equivalent registration paths",
        paths=[
            ProjectPath(path="s3://bucket/data", access_instructions="read-only role"),
            ProjectPath(path=str(home_path), label="code"),
        ],
    )

    assert retry.changed is False
    assert retry.project == first.project
    assert {item.path for item, _state in retry.paths} == {str(home_path), "s3://bucket/data"}
    assert _dolt_commits(store) == commits_before_retry


@pytest.mark.parametrize(
    "metadata_change",
    [{"label": "new label"}, {"access_instructions": "new access"}],
    ids=["label", "access-instructions"],
)
def test_changed_registration_path_metadata_refuses(
    store: Store, metadata_change: dict[str, str]
) -> None:
    """Catches path comparison ignoring changed metadata and silently reporting no change."""
    original = ProjectPath(
        path="s3://bucket/current", label="original label", access_instructions="original access"
    )
    first = _run(store, name="Changed path metadata", paths=[original])
    requested = original.model_copy(update=metadata_change)
    commits_before_refusal = _dolt_commits(store)

    with pytest.raises(ValidationError) as caught:
        _run(store, name="Changed path metadata", paths=[requested])

    assert str(caught.value) == (
        f"the project name 'Changed path metadata' is taken by {first.project.id}, which has "
        "other paths or framing; a name names one project"
    )
    stored = store.get_project(first.project.id)
    assert stored is not None
    assert stored.paths == [original]
    assert _dolt_commits(store) == commits_before_refusal


def test_registration_path_refusal_compares_under_exclusive_lock(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches the path comparison moving outside the serialized register decision."""
    path = "s3://bucket/locked"
    store.write_project(
        "Locked registration paths",
        "host",
        [ProjectPath(path=path, label="original")],
        [],
        intent="register",
    )
    real_eq = ProjectPath.__eq__
    lock_path = store.db_path / ".trails-write.lock"
    held: list[bool] = []

    def checking(self: ProjectPath, other: object) -> bool:
        if (
            isinstance(other, ProjectPath)
            and self.path == other.path == path
            and self.label != other.label
        ):
            with lock_path.open("a") as contender:
                try:
                    fcntl.flock(contender, fcntl.LOCK_SH | fcntl.LOCK_NB)
                except BlockingIOError:
                    held.append(True)
                else:
                    held.append(False)
                    fcntl.flock(contender, fcntl.LOCK_UN)
        return real_eq(self, other)

    monkeypatch.setattr(ProjectPath, "__eq__", checking)
    with pytest.raises(ValidationError, match="is taken by"):
        store.write_project(
            "Locked registration paths",
            "host",
            [ProjectPath(path=path, label="changed")],
            [],
            intent="register",
        )

    assert held and all(held)


def test_a_registration_refusal_names_no_other_operation(store: Store) -> None:
    """Silent failure caught: a refusal that tells the agent to use another operation for the
    same intent (principle 5), where the request is simply malformed: the name is taken."""
    _run(store, name="Taken name", framing=ProjectFramingDraft(title="Current purpose"))
    with pytest.raises(ValidationError) as caught:
        _run(store, name="Taken name", framing=ProjectFramingDraft(title="Other purpose"))
    assert "update_project" not in str(caught.value)
    assert "revise_framing" not in str(caught.value)
    assert "set_paths" not in str(caught.value)


def test_project_writes_record_the_harness_and_session(store: Store) -> None:
    """Silent failure caught: a project, path, framing or alias write with no trace of the
    harness and session that made it, although every entry write records them (the
    philosophy's opening: every write records its agent tool and session)."""
    from trails import sessions

    token = sessions.CLIENT_NAME.set("claude-code")
    try:
        first = _run(
            store,
            name="Attributed",
            paths=[ProjectPath(path="s3://bucket/a")],
            framing=ProjectFramingDraft(title="Purpose"),
        )
        store.write_tag_alias("ml", "machine-learning")
    finally:
        sessions.CLIENT_NAME.reset(token)
    rows = [
        store.query(f"SELECT harness FROM {table}")[0]["harness"]
        for table in ("project", "project_path", "project_framing_version", "tag_alias")
    ]
    assert rows == ["claude"] * 4
    assert first.project.name == "Attributed"


def test_set_paths_and_an_unchanged_framing_write_only_what_changed(store: Store) -> None:
    """Silent failure caught: a framing equal to the current one (up to line endings) written
    as a new version, or a path change that writes nothing."""
    first = _run(
        store,
        name="No-op update",
        paths=[ProjectPath(path="s3://bucket/old")],
        framing=ProjectFramingDraft(title="Purpose", body="Serve devices.\r\n"),
    )
    before = _dolt_commits(store)
    changed_paths = _call(
        store,
        set_paths,
        project=first.project.id,
        paths=[ProjectPath(path="s3://bucket/new")],
        remove_paths=["s3://bucket/old"],
    )
    after_paths = _dolt_commits(store)
    no_change = _call(
        store,
        revise_framing,
        project=first.project.id,
        framing=ProjectFramingDraft(title="Purpose", body="Serve devices.\n"),
        base_version=1,
        reason="No content change.",
    )

    assert changed_paths.changed is True
    assert [item.path for item, _state in changed_paths.paths] == ["s3://bucket/new"]
    assert after_paths == before + 1
    assert (no_change.changed, no_change.framing_changed) == (False, False)
    assert "no change detected" in no_change.render_text()
    assert [item.version for item in store.project_framing_history(first.project.id)] == [1]
    assert _dolt_commits(store) == after_paths


def test_revise_framing_adds_the_first_framing_with_base_zero_and_a_reason(
    store: Store,
) -> None:
    """Silent failure caught: a first framing written after registration with an empty
    reason, so its history says nothing about why."""
    project = _run(store, name="Later framing")
    with pytest.raises(ValidationError, match="base_version=0 and a nonempty reason"):
        _update(
            store,
            project=project.project.id,
            framing=ProjectFramingDraft(title="Purpose", body="Serve users."),
            base_version=0,
            reason="   ",
        )
    result = _update(
        store,
        project=project.project.id,
        framing=ProjectFramingDraft(title="Purpose", body="Serve users."),
        base_version=0,
        reason="Added project purpose.",
    )
    assert (result.changed, result.framing_changed) == (True, True)
    assert result.framing is not None and result.framing.version == 1


def test_update_refuses_an_unregistered_project(store: Store) -> None:
    with pytest.raises(ValidationError, match="no registered project"):
        _update(store, project="Missing", paths=[ProjectPath(path="s3://bucket/x")])


def test_store_update_intent_check_runs_under_the_exclusive_lock(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches the update-only existence guard moving outside the serialized decision."""
    real_get = Store.get_project
    lock_path = store.db_path / ".trails-write.lock"
    held: list[bool] = []

    def checking(self: Store, value: str) -> Any:
        if value == "Missing":
            with lock_path.open("a") as other:
                try:
                    fcntl.flock(other, fcntl.LOCK_SH | fcntl.LOCK_NB)
                except BlockingIOError:
                    held.append(True)
                else:
                    held.append(False)
                    fcntl.flock(other, fcntl.LOCK_UN)
        return real_get(self, value)

    monkeypatch.setattr(Store, "get_project", checking)
    with pytest.raises(ValidationError, match="not registered"):
        store.write_project("Missing", "host", [], [], intent="update")

    assert held == [True]
    assert real_get(store, "Missing") is None


def test_framing_versions_retry_and_stale_conflict_are_append_only(store: Store) -> None:
    """Catches retry duplication, empty reasons, and stale content overwriting framing."""
    first = _run(
        store,
        name="Latency",
        framing=ProjectFramingDraft(title="Reduce latency", body="Serve ingest users.\r\n"),
    )
    with pytest.raises(ValidationError, match="updating framing needs a nonempty reason"):
        _update(
            store,
            project="Latency",
            framing=ProjectFramingDraft(title="Reduce tail latency", body="Serve ingest users.\n"),
            base_version=1,
            reason="",
        )
    second = _update(
        store,
        project="Latency",
        framing=ProjectFramingDraft(title="Reduce tail latency", body="Serve ingest users.\n"),
        base_version=1,
        reason="Narrowed success.",
    )
    commits_after_second = _dolt_commits(store)
    retry_current = _update(
        store,
        project="Latency",
        framing=ProjectFramingDraft(title="Reduce tail latency", body="Serve ingest users.\r\n"),
        base_version=1,
        reason="Narrowed success.",
    )
    third = _update(
        store,
        project="Latency",
        framing=ProjectFramingDraft(title="Hold tail latency", body="Serve ingest users.\n"),
        base_version=2,
        reason="Changed the target.",
    )
    commits_after_third = _dolt_commits(store)
    retry_older = _update(
        store,
        project="Latency",
        framing=ProjectFramingDraft(title="Reduce tail latency", body="Serve ingest users.\n"),
        base_version=1,
        reason="Narrowed success.",
    )

    assert (first.framing.version, second.framing.version, third.framing.version) == (1, 2, 3)
    assert retry_current.framing.version == retry_older.framing.version == 2
    assert retry_current.current_framing_version == 2
    assert retry_older.current_framing_version == 3
    assert retry_current.framing_changed is retry_older.framing_changed is False
    assert _dolt_commits(store) == commits_after_third
    assert commits_after_second < commits_after_third
    assert [item.version for item in store.project_framing_history(first.project.id)] == [3, 2, 1]

    with pytest.raises(ValidationError, match="stale; current version is 3"):
        _update(
            store,
            project="Latency",
            framing=ProjectFramingDraft(title="Different", body="Different"),
            base_version=1,
            reason="Conflicting edit.",
        )


class _AnyText(FakeEngine):
    def embed(self, text: str) -> Sequence[float]:
        self.calls.append(text)
        return basis(0)


def test_register_embeds_framing_and_keeps_old_vectors(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches a framing version stored without its vector or a later version deleting old rows."""
    monkeypatch.setattr(engine_seam, "build_engine", _AnyText)
    first = _run(
        store,
        name="Vectors",
        framing=ProjectFramingDraft(title="First", body="First purpose"),
    )
    _update(
        store,
        project="Vectors",
        framing=ProjectFramingDraft(title="Second", body="Second purpose"),
        base_version=1,
        reason="Changed.",
    )

    assert store.query(
        "SELECT version FROM project_framing_embedding WHERE project_id = ? ORDER BY version",
        [first.project.id],
    ) == [{"version": 1}, {"version": 2}]


def test_project_names_use_database_equality_and_near_names_only_note(store: Store) -> None:
    """Catches accent/case duplicates creating identities, near names blocking creation, and an
    id-shaped human name making exact calls ambiguous."""
    original = _run(store, name="Café ingest")
    equal = _run(store, name="CAFE INGEST")
    near = _run(store, name="Cafe-ingest")

    assert equal.project.id == original.project.id
    assert near.project.id != original.project.id
    assert near.notes == [
        f"similar registered project: {original.project.id} · {original.project.name}"
    ]
    with pytest.raises(ValidationError, match="reserved project id shape"):
        _run(store, name="prj-a7k2m")


@pytest.mark.parametrize(
    "bad", ["Alpha\u0085Beta", "Alpha\u2028Beta", "Alpha\u200bBeta", "Alpha\ud800Beta"]
)
def test_project_names_refuse_unicode_controls_and_line_separators(store: Store, bad: str) -> None:
    with pytest.raises(ValidationError, match="control characters"):
        _run(store, name=bad)


@pytest.mark.parametrize(
    "bad", ["Frame\u0085Title", "Frame\u2029Title", "Frame\u200bTitle", "Frame\ud800Title"]
)
def test_framing_titles_refuse_unicode_controls_and_line_separators(store: Store, bad: str) -> None:
    with pytest.raises(ValidationError, match="control characters"):
        _run(
            store,
            name="Unicode framing",
            framing=ProjectFramingDraft(title=bad, body="Purpose"),
        )


def test_nul_in_framing_body_is_a_validation_refusal(store: Store) -> None:
    with pytest.raises(ValidationError, match="framing body contains a NUL byte"):
        _run(
            store,
            name="Nul framing",
            framing=ProjectFramingDraft(title="Frame", body="before\x00after"),
        )


def test_unpaired_surrogate_in_framing_body_is_a_validation_refusal(store: Store) -> None:
    with pytest.raises(
        ValidationError, match="framing body contains an unpaired Unicode surrogate"
    ):
        _run(
            store,
            name="Surrogate framing",
            framing=ProjectFramingDraft(title="Frame", body="before\ud800after"),
        )
