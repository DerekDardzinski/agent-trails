"""Warnings and notes that publish, link and prime give about where a record's code and data
live and about records that later records contradict or supersede.

Each test names the silent failure it catches. None of these paths refuses a write: every
assertion is about what the agent is told after the record is stored.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from trails import locations
from trails.ops import context
from trails.ops.link import link
from trails.ops.prime import prime
from trails.ops.publish import publish
from trails.store import Store


def _run(store: Store, operation: Any, **arguments: Any) -> Any:
    if operation is publish:
        arguments.setdefault("entry_type", "note")
        arguments.setdefault("work_status", "closed")

    async def go() -> Any:
        return await context.bind(operation, lambda: store.db_path)(**arguments)

    return asyncio.run(go())


def _temp_root(monkeypatch: pytest.MonkeyPatch, root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    resolved = root.resolve()
    monkeypatch.setattr(locations, "temporary_roots", lambda: (resolved,))
    return resolved


def _artifact(path: Path) -> list[dict[str, Any]]:
    return [{"location": str(path), "digest": None, "notes": ""}]


# --- where the code and data behind a record live -------------------------------------------


def test_a_script_in_a_temporary_directory_warns_that_it_will_be_lost(
    store: Store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches the audit's main loss: a /tmp analysis script recorded with no warning, so the
    code behind the number disappears when the OS clears the directory."""
    root = _temp_root(monkeypatch, tmp_path / "scratch")
    script = root / "a2_alias.py"
    script.write_text("print(1)\n")

    result = _run(store, publish, title="Alias count", artifacts=_artifact(script))

    [warning] = result.warnings
    assert warning.startswith(
        f"artifact[0] {script} is a script in the temporary directory {root}, which the "
        "operating system may delete"
    )
    assert warning.endswith(
        f'export("{result.record_id}", "<file>"), add reason = "..." to its header, then '
        'publish(from_file="<file>", repos=["<repository>"])'
    )


def test_data_in_a_temporary_directory_gets_the_data_wording_without_a_repos_call(
    store: Store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches data being told to go into a git repository, which would push agents to commit
    datasets into captures."""
    root = _temp_root(monkeypatch, tmp_path / "scratch")
    data = root / "p3snap_0" / "part-0.parquet"

    result = _run(store, publish, title="Snapshot", artifacts=_artifact(data))

    [warning] = result.warnings
    assert warning.startswith(
        f"artifact[0] {data} is in the temporary directory {root}, which the operating system "
        "may delete; trails keeps a reference to it, never a copy. Move it somewhere permanent"
    )
    assert "repos=" not in warning


def test_a_script_held_by_this_publishs_capture_gets_no_warning_even_under_a_temp_root(
    store: Store, git_repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches a false alarm on the right workflow: the capture pins the untracked script, so
    its later deletion loses nothing."""
    _temp_root(monkeypatch, tmp_path)
    script = git_repo / "analysis.py"
    script.write_text("print(2)\n")

    result = _run(
        store, publish, title="Captured", artifacts=_artifact(script), repos=[str(git_repo)]
    )

    assert result.warnings == []


def test_a_script_git_ignores_is_reported_even_when_its_repository_was_captured(
    store: Store, git_repo: Path
) -> None:
    """Catches the gap where capture skips ignored files, so passing repos looked like it
    saved a script it did not."""
    (git_repo / ".gitignore").write_text("scratch/\n")
    script = git_repo / "scratch" / "run.py"
    script.parent.mkdir()
    script.write_text("print(3)\n")
    top = git_repo.resolve()

    result = _run(
        store, publish, title="Ignored", artifacts=_artifact(script), repos=[str(git_repo)]
    )

    [warning] = result.warnings
    assert warning.startswith(
        f"artifact[0] {script} is code in the git repository {top}, but git ignores it, so "
        "the capture left it out."
    )
    assert warning.endswith(f'repos=["{top}"])')


def test_a_script_in_a_repository_not_passed_in_repos_gets_the_call_with_its_top(
    store: Store, git_repo: Path
) -> None:
    """Catches the keyword-eval case: scripts in a repository that no publish captured."""
    script = git_repo / "src" / "score.py"
    script.parent.mkdir()
    script.write_text("print(4)\n")
    top = git_repo.resolve()

    result = _run(store, publish, title="Not captured", artifacts=_artifact(script))

    [warning] = result.warnings
    assert warning.startswith(
        f"artifact[0] {script} is code in the git repository {top}, but this publish did not "
        "capture that repository."
    )
    assert warning.endswith(f'repos=["{top}"])')


def test_code_outside_any_repository_is_reported_as_uncapturable(
    store: Store, tmp_path: Path
) -> None:
    """Catches scripts in plain output directories, nine of which the audit found."""
    script = tmp_path / "output" / "analyze.py"
    script.parent.mkdir()
    script.write_text("print(5)\n")

    result = _run(store, publish, title="Loose", artifacts=_artifact(script))

    [warning] = result.warnings
    assert warning.startswith(
        f"artifact[0] {script} is code outside any git repository, so trails could not capture it"
    )


def test_temporary_paths_named_only_in_the_body_get_one_warning_listing_them(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches the common case where the script path appears only in prose, never as an
    artifact; a URI whose path contains /tmp is not a local file and is not listed."""
    monkeypatch.setattr(locations, "temporary_roots", lambda: (Path("/tmp").resolve(),))
    body = (
        "Ran /tmp/da/run_alias.py on s3://bucket/tmp/x.parquet, wrote /tmp/da/out.csv. "
        "Again: /tmp/da/run_alias.py."
    )

    result = _run(store, publish, title="Prose paths", body=body)

    assert result.warnings == [
        "the record names 2 paths in temporary directories, which the operating system may "
        "delete, and trails keeps no copy: /tmp/da/run_alias.py, /tmp/da/out.csv. Commit the "
        "scripts to a git repository and pass it in repos; move data somewhere permanent and "
        f'name it in artifacts: export("{result.record_id}", "<file>"), add reason = "..." '
        'to its header, then publish(from_file="<file>", repos=["<repository>"])'
    ]


def test_the_real_temporary_roots_cover_tmp_and_not_the_home_directory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches the default list missing /tmp (macOS resolves it to /private/tmp) while every
    other test runs with patched roots."""
    monkeypatch.undo()

    assert locations.temporary_root(Path("/tmp/x/a.py")) == Path("/tmp").resolve()
    assert locations.temporary_root(Path("/private/tmp/a.py")) == Path("/tmp").resolve()
    assert locations.temporary_root(Path.home() / "workplace" / "a.py") is None


# --- records that later records contradict or supersede -------------------------------------


def test_prime_shows_a_note_that_contradicts_a_report(store: Store) -> None:
    """Catches the audit's 8 wrong-but-current records: a correction written as a note was
    invisible in prime, which listed contradictions only on hypotheses and only from results."""
    project = store.write_project("Oracle", "host", [], []).project.id
    report = _run(
        store, publish, entry_type="report", title="19,389 of 20,948 rows", projects=[project]
    ).record_id
    correction = _run(
        store,
        publish,
        title="Correction: 19,189 of 20,748",
        projects=[project],
        links=[{"relation": "contradicts", "target_id": report}],
    ).record_id

    text = _run(store, prime, project=project).render_text()

    assert (
        f"  {report}  19,389 of 20,948 rows · work closed\n"
        f"    contradicted by: {correction} (note) Correction: 19,189 of 20,748"
    ) in text


def test_contradicting_a_current_record_names_the_calls_that_correct_it_there(
    store: Store,
) -> None:
    """Catches corrections that stop at a side note: the target is still read as current."""
    report = _run(store, publish, entry_type="report", title="Wrong").record_id
    old = _run(store, publish, entry_type="report", title="Old").record_id
    _run(store, publish, entry_type="report", title="Newer", supersedes=old, reason="update")

    result = _run(
        store,
        publish,
        title="Both wrong",
        links=[
            {"relation": "contradicts", "target_id": report},
            {"relation": "contradicts", "target_id": old},
        ],
    )

    assert result.notes[-1:] == [
        f"{result.record_id} contradicts {report} (report), which is still current, so a "
        f"reader of {report} sees its content as current belief. If {report} is wrong, "
        f'correct it there: export("{report}", "<file>"), add reason = "..." to its header, '
        'then publish(from_file="<file>") with the corrected content, or '
        f'retire("{report}", reason="...") when nothing replaces it'
    ]
    assert not any(old in note for note in result.notes)


def test_a_new_link_to_a_superseded_record_names_its_newest_version(store: Store) -> None:
    """Catches links landing on old problem statements, which the audit found 32 of, and a
    warning that names the next version instead of the current one after two revisions."""
    first = _run(store, publish, entry_type="problem-statement", title="v1").record_id
    second = _run(
        store, publish, entry_type="problem-statement", title="v2", supersedes=first, reason="r"
    ).record_id
    third = _run(
        store, publish, entry_type="problem-statement", title="v3", supersedes=second, reason="r"
    ).record_id

    published = _run(
        store, publish, title="Report", links=[{"relation": "builds_on", "target_id": first}]
    )
    linked = _run(store, link, source_id=published.record_id, relation="cites", target_id=second)

    assert published.warnings == [
        f"{published.record_id} builds_on {first}, which is superseded; its current version "
        f"is {third}, and a reader of {third} does not see this link. If this record is about "
        f'the current version, add: link("{published.record_id}", "builds_on", "{third}")'
    ]
    assert linked.warnings == [
        f"{published.record_id} cites {second}, which is superseded; its current version is "
        f"{third}, and a reader of {third} does not see this link. If this record is about the "
        f'current version, add: link("{published.record_id}", "cites", "{third}")'
    ]


def test_superseding_a_problem_statement_lists_current_records_on_every_earlier_version(
    store: Store,
) -> None:
    """Catches the re-pointing list missing records on older versions, or listing records
    that were themselves replaced."""
    first = _run(store, publish, entry_type="problem-statement", title="v1").record_id
    on_first = _run(
        store, publish, title="on v1", links=[{"relation": "builds_on", "target_id": first}]
    ).record_id
    second = _run(
        store, publish, entry_type="problem-statement", title="v2", supersedes=first, reason="r"
    ).record_id
    on_second = _run(
        store, publish, title="on v2", links=[{"relation": "builds_on", "target_id": second}]
    ).record_id
    replaced = _run(
        store, publish, title="old", links=[{"relation": "builds_on", "target_id": second}]
    ).record_id
    _run(store, publish, title="new", supersedes=replaced, reason="r")

    third = _run(
        store, publish, entry_type="problem-statement", title="v3", supersedes=second, reason="r"
    )

    assert third.notes[-1] == (
        f"2 current records build on the replaced problem statement ({second}, {first}), and "
        f'show and search(links="builds_on:{third.record_id}") do not list them under '
        f"{third.record_id}. To relate each to {third.record_id}: "
        f'link("{on_second}", "builds_on", "{third.record_id}") · '
        f'link("{on_first}", "builds_on", "{third.record_id}")'
    )
