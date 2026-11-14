"""Tests for write(repos=...): code capture into trails' own git store.

Real git and real Dolt, each in a temporary directory.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import pytest

from trails import capture, gitprobe
from trails.entry_store import EntryStore
from trails.errors import TrailsError, ValidationError
from trails.models import EntryDraft
from trails.ops import context
from trails.ops.write import write

AWS_KEY = "AKIAIOSFODNN7EXAMPLE"


def _run(store: EntryStore, **arguments: Any) -> Any:
    if "id" not in arguments:
        arguments.setdefault("kind", "note")
        arguments.setdefault("title", "t")

    async def go() -> Any:
        return await context.bind(write, lambda: store.db_path)(**arguments)

    return asyncio.run(go())


def _store(store: EntryStore) -> Path:
    return capture.store_path(store.db_path)


def _show(store: EntryStore, sha: str, path: str) -> str:
    """A file of a capture, read from the store with no alternate."""
    return subprocess.run(
        ["git", f"--git-dir={_store(store)}", "show", f"{sha}:{path}"],
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
        env={k: v for k, v in os.environ.items() if not k.startswith("GIT_")},
    ).stdout.strip()


def _files(store: EntryStore, sha: str) -> list[str]:
    listing = subprocess.run(
        ["git", f"--git-dir={_store(store)}", "ls-tree", "-r", "--name-only", sha],
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    ).stdout
    return listing.splitlines()


def _git_bytes(repo: Path, *args: str) -> bytes:
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, check=True, timeout=60
    ).stdout


def _user_state(repo: Path) -> tuple[bytes, bytes, bytes, bytes, list[str], int]:
    """Everything a capture must leave alone in the user's repository."""
    git_dir = repo / ".git"
    return (
        _git_bytes(repo, "--no-optional-locks", "status", "--porcelain"),
        _git_bytes(repo, "rev-parse", "HEAD"),
        _git_bytes(repo, "ls-files", "-s"),
        _git_bytes(repo, "for-each-ref"),
        sorted(str(p.relative_to(git_dir)) for p in git_dir.rglob("*")),
        (git_dir / "index").stat().st_mtime_ns,
    )


def test_capture_writes_nothing_to_the_repository_and_holds_the_new_contents(
    store: EntryStore, git_repo: Path, git_cmd: Callable[..., str]
) -> None:
    """labbook-79y.10 criterion 1. Catches a capture that writes an object, a ref, a lock
    file or the index in the user's repository, misses the untracked file or the uncommitted
    edit, or pins a ref other than the captured sha."""
    (git_repo / "tracked.txt").write_text("edited\n")
    (git_repo / "untracked.txt").write_text("new\n")
    before = _user_state(git_repo)
    head = git_cmd(git_repo, "rev-parse", "HEAD")

    result = _run(store, title="t", kind="claim", repos=[str(git_repo)])

    assert _user_state(git_repo) == before
    [code] = store.get_entry(result.id).current.code
    assert (code.head_sha, code.dirty) == (head, True)
    assert _show(store, code.captured_sha, "tracked.txt") == "edited"
    assert _show(store, code.captured_sha, "untracked.txt") == "new"
    pinned = git_cmd(_store(store), "rev-parse", f"refs/research/{result.id}/v1/0")
    assert pinned == code.captured_sha


def _git_tree(repo: Path) -> dict[str, tuple[int, int]]:
    """Every file under .git with its size and mtime."""
    git_dir = repo / ".git"
    return {
        str(path.relative_to(git_dir)): (path.stat().st_size, path.stat().st_mtime_ns)
        for path in sorted(git_dir.rglob("*"))
        if path.is_file() or path.is_socket()
    }


@pytest.mark.parametrize(
    "setting", ["filter", "core.splitIndex", "core.fsmonitor", "core.untrackedCache"]
)
def test_repository_config_does_not_make_a_capture_write_into_git(
    store: EntryStore, git_repo: Path, git_cmd: Callable[..., str], setting: str
) -> None:
    """labbook-79y.10 review round 1. Catches the capture's git commands running under the
    repository's own config, where a clean filter such as git-lfs's writes .git/lfs/objects
    and stores a pointer in place of the file, core.splitIndex writes sharedindex files, and
    core.fsmonitor starts a daemon with a socket in .git. The filter here writes a marker
    into .git and rewrites the content, so both effects are visible."""
    if setting == "filter":
        git_cmd(
            git_repo,
            "config",
            "filter.marker.clean",
            "sh -c 'touch \"$(git rev-parse --git-dir)/filtered\"; tr a b'",
        )
        git_cmd(git_repo, "config", "filter.marker.required", "true")
        (git_repo / ".gitattributes").write_text("*.bin filter=marker\n")
    else:
        git_cmd(git_repo, "config", setting, "true")
    (git_repo / "data.bin").write_text("aaaa\n")
    (git_repo / "tracked.txt").write_text("edited\n")
    before = _git_tree(git_repo)
    try:
        result = _run(store, title="t", repos=[str(git_repo)])
    finally:
        subprocess.run(
            ["git", "-C", str(git_repo), "fsmonitor--daemon", "stop"],
            capture_output=True,
            check=False,
            timeout=60,
        )

    assert _git_tree(git_repo) == before
    [code] = store.get_entry(result.id).current.code
    assert _show(store, code.captured_sha, "data.bin") == "aaaa"


def test_a_repository_whose_path_holds_a_colon_is_captured(
    store: EntryStore, make_git_repo: Callable[[str], Path]
) -> None:
    """labbook-79y.10 review round 1, advisory. Catches the alternate path split at the colon,
    which git reports as an unnormalizable path and the write fails."""
    repo = make_git_repo("a:b/repo")

    result = _run(store, title="t", repos=[str(repo)])

    [code] = store.get_entry(result.id).current.code
    assert _show(store, code.captured_sha, "tracked.txt") == "original"


def test_a_capture_is_readable_after_the_repository_is_deleted(
    store: EntryStore, git_repo: Path
) -> None:
    """labbook-79y.10 criterion 1. Catches a capture whose unchanged files were only ever read
    through the repository's own objects, so it vanishes with the repository."""
    (git_repo / "new.txt").write_text("n\n")
    result = _run(store, title="t", kind="claim", repos=[str(git_repo)])
    [code] = store.get_entry(result.id).current.code

    shutil.rmtree(git_repo)

    assert _show(store, code.captured_sha, "tracked.txt") == "original"
    assert _show(store, code.captured_sha, "new.txt") == "n"
    fsck = subprocess.run(
        ["git", f"--git-dir={_store(store)}", "fsck", "--no-dangling"],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )
    assert (fsck.returncode, fsck.stderr) == (0, "")


def test_a_clean_tree_is_not_dirty_and_an_edit_is(
    store: EntryStore, git_repo: Path, git_cmd: Callable[..., str]
) -> None:
    """labbook-79y.9 item 11a. Catches dirty compared against the commit sha rather than its
    tree, which reports every capture dirty because a capture commit is never HEAD."""
    clean = _run(store, title="t", repos=[str(git_repo)])
    (git_repo / "tracked.txt").write_text("edited\n")
    edited = _run(store, title="t", repos=[str(git_repo)])

    assert [c.dirty for c in store.get_entry(clean.id).current.code] == [False]
    assert [c.dirty for c in store.get_entry(edited.id).current.code] == [True]


def test_two_concurrent_captures_of_one_repo_both_succeed(
    store: EntryStore, git_repo: Path, git_cmd: Callable[..., str]
) -> None:
    """Catches two captures colliding on the store, so one write fails or one record names
    a commit that is not pinned."""
    (git_repo / "untracked.txt").write_text("new\n")

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda n: _run(store, title=f"t{n}", repos=[str(git_repo)]),
                range(2),
            )
        )

    ids = {result.id for result in results}
    assert len(ids) == 2
    refs = git_cmd(_store(store), "for-each-ref", "--format=%(refname)", "refs/research/")
    assert sorted(refs.splitlines()) == sorted(f"refs/research/{i}/v1/0" for i in ids)


def test_two_repositories_in_one_write_get_one_ref_each(
    store: EntryStore, make_git_repo: Callable[[str], Path], git_cmd: Callable[..., str]
) -> None:
    """labbook-79y.10 design gap. Catches both captures of one write pinned under the same
    ref, which refuses the second after the first is written."""
    first, second = make_git_repo("one"), make_git_repo("two")

    result = _run(store, title="t", repos=[str(first), str(second)])

    refs = git_cmd(_store(store), "for-each-ref", "--format=%(refname)", "refs/research/")
    assert refs.splitlines() == [
        f"refs/research/{result.id}/v1/0",
        f"refs/research/{result.id}/v1/1",
    ]


def test_a_non_git_directory_is_refused_by_path(store: EntryStore, tmp_path: Path) -> None:
    """Catches a non-repository recorded silently with no code captured."""
    plain = tmp_path / "plain"
    plain.mkdir()

    with pytest.raises(ValidationError, match=str(plain)):
        _run(store, title="t", repos=[str(plain)])


def test_a_repo_with_no_commit_gives_a_parentless_capture(
    store: EntryStore, tmp_path: Path, git_cmd: Callable[..., str]
) -> None:
    """Catches `read-tree HEAD` failing the write in a fresh repository."""
    repo = tmp_path / "fresh"
    repo.mkdir()
    git_cmd(repo, "init", "-q", ".")
    (repo / "a.txt").write_text("a\n")

    result = _run(store, title="t", repos=[str(repo)])

    [code] = store.get_entry(result.id).current.code
    assert (code.head_sha, code.dirty) == (None, True)
    assert _files(store, code.captured_sha) == ["a.txt"]


def test_a_capture_has_no_parent_so_history_is_not_copied(
    store: EntryStore, git_repo: Path, git_cmd: Callable[..., str]
) -> None:
    """labbook-79y.10 deviation from the design's `-p head`. Catches the capture parented on
    HEAD, which copies the whole history into the store, including a large file deleted
    long ago that the size check never saw."""
    (git_repo / "old.bin").write_bytes(b"x" * 64)
    git_cmd(git_repo, "add", "old.bin")
    git_cmd(git_repo, "commit", "-qm", "add")
    old = git_cmd(git_repo, "rev-parse", "HEAD:old.bin")
    git_cmd(git_repo, "rm", "-q", "old.bin")
    git_cmd(git_repo, "commit", "-qm", "remove")

    result = _run(store, title="t", repos=[str(git_repo)])

    [code] = store.get_entry(result.id).current.code
    parents = git_cmd(_store(store), "rev-list", "--parents", "-n", "1", code.captured_sha)
    assert parents == code.captured_sha
    present = subprocess.run(
        ["git", f"--git-dir={_store(store)}", "cat-file", "-e", old],
        capture_output=True,
        check=False,
        timeout=60,
    )
    assert present.returncode != 0


def test_a_linked_worktree_is_captured_without_writing_to_the_main_repository(
    store: EntryStore, git_repo: Path, git_cmd: Callable[..., str], tmp_path: Path
) -> None:
    """Catches the alternate taken from the worktree's own git dir, which holds no objects,
    so every unchanged file of a worktree fails to read."""
    worktree = tmp_path / "wt"
    git_cmd(git_repo, "worktree", "add", "-q", str(worktree), "-b", "side")
    (worktree / "w.txt").write_text("w\n")
    before = sorted(str(p) for p in (git_repo / ".git").rglob("*"))

    result = _run(store, title="t", repos=[str(worktree)])

    [code] = store.get_entry(result.id).current.code
    assert _show(store, code.captured_sha, "w.txt") == "w"
    assert _show(store, code.captured_sha, "tracked.txt") == "original"
    assert sorted(str(p) for p in (git_repo / ".git").rglob("*")) == before


@pytest.mark.parametrize("second", ["same", "worktree"])
def test_two_entries_of_one_repository_are_refused_before_capture(
    store: EntryStore, git_repo: Path, git_cmd: Callable[..., str], tmp_path: Path, second: str
) -> None:
    """Catches one repository captured twice under one record, as two paths. Covers the same
    repo listed twice and a repo plus its own linked worktree."""
    other = git_repo
    if second == "worktree":
        other = tmp_path / "wt"
        git_cmd(git_repo, "worktree", "add", "-q", str(other), "-b", "side")

    with pytest.raises(ValidationError, match="same repository"):
        _run(store, title="t", repos=[str(git_repo), str(other)])

    assert not _store(store).exists()


def test_a_secret_in_a_new_untracked_directory_is_a_warning(
    store: EntryStore, git_repo: Path
) -> None:
    """Catches a scan that sees a new untracked directory as one entry and never reads its
    files, a secret that blocks the write, or the key text in a warning."""
    (git_repo / "newexp").mkdir()
    (git_repo / "newexp" / ".env").write_text(f"key = {AWS_KEY}\n")

    result = _run(store, title="t", repos=[str(git_repo)])

    assert store.get_entry(result.id).current.code
    assert "possible secret: AWSKeyDetector in file:newexp/.env line 1" in result.warnings
    assert AWS_KEY not in result.render_text()


def test_an_untracked_nested_repository_is_named_as_not_captured(
    store: EntryStore, git_repo: Path, make_git_repo: Callable[[str], Path]
) -> None:
    """labbook-79y.8 criteria 1 and 2. Catches a capture that silently drops a nested
    repository's files, and a secret scan that reads the gitlink as an empty file or fails
    on it."""
    nested = make_git_repo("repo/vendored")
    (nested / ".env").write_text(f"key = {AWS_KEY}\n")

    result = _run(store, title="t", repos=[str(git_repo)])

    top = str(git_repo.resolve())
    assert (
        f"not captured: {top}/vendored is a nested git repository, so its files are not in "
        "this capture; pass it in repos to capture it"
    ) in result.warnings
    assert not any(warning.startswith("possible secret") for warning in result.warnings)


def _add_submodule(
    repo: Path, make_git_repo: Callable[[str], Path], git_cmd: Callable[..., str], path: str
) -> Path:
    source = make_git_repo("submodule-source")
    git_cmd(repo, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(source), path)
    git_cmd(repo, "commit", "-qm", "add submodule")
    return repo / path


def test_a_registered_submodule_is_not_reported_as_uncaptured(
    store: EntryStore,
    git_repo: Path,
    make_git_repo: Callable[[str], Path],
    git_cmd: Callable[..., str],
) -> None:
    """labbook-79y.8 criterion 3. Catches a registered submodule, whose commit the
    superproject records on purpose, being reported as missing files. The path has a space
    because git names a submodule after its path, and a name with a space once broke the
    .gitmodules parse, which now reads the store's copy of .gitmodules."""
    _add_submodule(git_repo, make_git_repo, git_cmd, "third party/sub")

    result = _run(store, title="t", repos=[str(git_repo)])

    assert not any(warning.startswith("not captured") for warning in result.warnings)


def test_a_nested_repository_with_no_commit_is_left_out_and_passing_it_captures_it(
    store: EntryStore, git_repo: Path, git_cmd: Callable[..., str]
) -> None:
    """labbook-79y.6 item 16 and labbook-79y.9 item 11e. Catches `git add -A` refusing the
    nested repository and so refusing the whole write, the omission going unreported, and
    the advice not working: passing the nested repository in repos must capture its files."""
    for name in ("empty", "deep/x/inner"):
        (git_repo / name).mkdir(parents=True)
        git_cmd(git_repo / name, "init", "-q", ".")
    (git_repo / "empty" / "e.txt").write_text("e\n")
    (git_repo / "kept.txt").write_text("kept\n")

    alone = _run(store, title="t", repos=[str(git_repo)])
    together = _run(store, title="t", repos=[str(git_repo), str(git_repo / "empty")])

    top = str(git_repo.resolve())
    left_out = "is a nested git repository with no commit, so it was left out of this capture"
    assert sorted(w for w in alone.warnings if left_out in w) == [
        f"not captured: {top}/deep/x/inner {left_out}; pass it in repos to capture it",
        f"not captured: {top}/empty {left_out}; pass it in repos to capture it",
    ]
    [code] = store.get_entry(alone.id).current.code
    assert _show(store, code.captured_sha, "kept.txt") == "kept"
    assert not any(f"{top}/empty " in w for w in together.warnings)
    shas = {c.repo_path: c.captured_sha for c in store.get_entry(together.id).current.code}
    assert _files(store, shas[f"{top}/empty"]) == ["e.txt"]


def test_a_submodule_with_uncommitted_changes_is_named_and_passing_it_captures_them(
    store: EntryStore,
    git_repo: Path,
    make_git_repo: Callable[[str], Path],
    git_cmd: Callable[..., str],
) -> None:
    """labbook-79y.6 item 16. Catches a submodule's uncommitted edits missing from the
    capture with no warning, and a warning whose advice does not work."""
    submodule = _add_submodule(git_repo, make_git_repo, git_cmd, "vendor/lib")
    (submodule / "tracked.txt").write_text("patched\n")

    alone = _run(store, title="t", repos=[str(git_repo)])
    together = _run(store, title="t", repos=[str(git_repo), str(submodule)])

    top = str(git_repo.resolve())
    assert (
        f"not captured: {top}/vendor/lib is a submodule with uncommitted changes, and this "
        "capture records only its commit; pass it in repos to capture the changes"
    ) in alone.warnings
    assert not any(w.startswith("not captured") for w in together.warnings)
    shas = {Path(c.repo_path): c.captured_sha for c in store.get_entry(together.id).current.code}
    assert _show(store, shas[submodule.resolve()], "tracked.txt") == "patched"


@pytest.mark.parametrize("committed", [True, False])
def test_ls_tree_runs_once_in_the_repository_and_once_in_the_store(
    store: EntryStore,
    tmp_path: Path,
    git_cmd: Callable[..., str],
    monkeypatch: pytest.MonkeyPatch,
    committed: bool,
) -> None:
    """labbook-79y.6 item 16 and labbook-79y.9 item 11c. Catches the listing rerun for each
    consumer of it: gitlinks, sizes, changed files. A repository with no commit has no HEAD to
    list, so only the store's listing runs."""
    repo = tmp_path / "r"
    repo.mkdir()
    git_cmd(repo, "init", "-q", ".")
    (repo / "a.txt").write_text("a\n")
    if committed:
        git_cmd(repo, "add", "a.txt")
        git_cmd(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "c")
        (repo / "b.txt").write_text("b\n")
    real_run = subprocess.run
    listings: list[list[str]] = []

    def counting(command: list[str], *args: Any, **kwargs: Any) -> Any:
        if command[:1] == ["git"] and "ls-tree" in command:
            listings.append(command)
        return real_run(command, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", counting)
    _run(store, title="t", repos=[str(repo)])

    in_store = [c for c in listings if any(a.startswith("--git-dir=") for a in c)]
    assert (len(in_store), len(listings) - len(in_store)) == (1, 1 if committed else 0)


def test_git_add_runs_with_lc_all_c(
    store: EntryStore, git_repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """labbook-79y.9 item 11f. Catches the add run in the caller's locale, where git prints a
    translated refusal for a nested repository with no commit, the regex does not match, and
    the whole write fails."""
    monkeypatch.setenv("LC_ALL", "de_DE.UTF-8")
    real_run = subprocess.run
    locales: list[str | None] = []

    def recording(command: list[str], *args: Any, **kwargs: Any) -> Any:
        if command[:1] == ["git"] and "add" in command:
            locales.append((kwargs.get("env") or {}).get("LC_ALL"))
        return real_run(command, *args, **kwargs)

    monkeypatch.setattr(subprocess, "run", recording)
    _run(store, title="t", repos=[str(git_repo)])

    assert locales == ["C"]


def test_a_blob_missing_from_the_store_raises_trails_error(store: EntryStore) -> None:
    """labbook-79y.6 item 16. Catches the secret scan skipping a file it could not read,
    which would report a capture as scanned when part of it was not."""
    captures = capture.open_store(store.db_path)

    with pytest.raises(TrailsError, match=r"blob 0{40} of new.txt is not in"):
        capture.scan_capture(captures, {"new.txt": "0" * 40})


def test_a_capture_waits_for_the_store_lock(store: EntryStore, git_repo: Path) -> None:
    """labbook-79y.6 item 14. Catches the lock removed from capture: the concurrent capture
    test usually passes without it."""
    (git_repo / "new.txt").write_text("n\n")
    order: list[str] = []
    with gitprobe.git_lock(capture.open_store(store.db_path)):
        writeer = threading.Thread(
            target=lambda: order.append(_run(store, title="t", repos=[str(git_repo)]).id)
        )
        writeer.start()
        writeer.join(3)
        order.append("released")
    writeer.join(60)

    assert order[0] == "released"
    assert len(order) == 2


# ----------------------------------------------------- validation before capture


def test_malformed_fields_are_refused_before_repository_capture(
    store: EntryStore, git_repo: Path
) -> None:
    """Catches a refused write leaving an unreported capture ref or capture store."""
    with pytest.raises(ValidationError, match="priority is a goal and task field"):
        _run(store, kind="claim", title="bad priority", priority=1, repos=[str(git_repo)])
    with pytest.raises(ValidationError, match="no entry has id"):
        _run(
            store,
            kind="task",
            title="bad dependency",
            waits_on=["tsk-zzzzz"],
            repos=[str(git_repo)],
        )

    assert store.query("SELECT COUNT(*) AS n FROM entry") == [{"n": 0}]
    assert not _store(store).exists()


def test_a_losing_version_race_discards_its_unwritten_capture_refs(
    store: EntryStore,
    git_repo: Path,
    git_cmd: Callable[..., str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches the final serialized base_version check leaving a capture ref for a version it
    refused."""
    old = _run(store, title="old").id
    original_capture = capture.capture_repo
    winner: list[int] = []

    def capture_then_compete(*args: Any, **kwargs: Any) -> Any:
        outcome = original_capture(*args, **kwargs)
        winner.append(
            store.write_entry(
                EntryDraft(id=old, base_version=1, reason="won the race", body="winner")
            ).entry.current.version
        )
        return outcome

    monkeypatch.setattr(capture, "capture_repo", capture_then_compete)

    with pytest.raises(ValidationError) as refused:
        _run(store, id=old, base_version=1, reason="lost the race", repos=[str(git_repo)])

    refs = git_cmd(_store(store), "for-each-ref", "--format=%(refname)", "refs/research/")
    assert winner == [2]
    assert "base_version=1 is stale" in str(refused.value)
    assert refs == ""
    assert [v.body for v in store.get_versions(old)] == ["", "winner"]


def test_a_post_land_exception_keeps_the_durable_capture_ref(
    store: EntryStore,
    git_repo: Path,
    git_cmd: Callable[..., str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Catches rollback deleting a capture pin after the corresponding entry became durable."""
    original_write = EntryStore.write_entry
    landed: list[str] = []

    def write_then_raise(bound_store: EntryStore, draft: EntryDraft, new_id: Any = None) -> Any:
        outcome = original_write(bound_store, draft, new_id)
        landed.append(outcome.entry.id)
        raise ValidationError("injected after durable write")

    monkeypatch.setattr(EntryStore, "write_entry", write_then_raise)

    with pytest.raises(ValidationError, match="injected after durable write"):
        _run(store, title="durable", repos=[str(git_repo)])

    assert len(landed) == 1
    assert store.get_entry(landed[0]).current.title == "durable"
    assert (
        git_cmd(_store(store), "rev-parse", f"refs/research/{landed[0]}/v1/0")
        == store.get_entry(landed[0]).current.code[0].captured_sha
    )


# ------------------------------------------------------------------ size check


def _big(repo: Path, name: str, size: int = 64) -> Path:
    path = repo / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * size)
    return path


def test_a_file_over_the_cap_refuses_the_write_and_writes_nothing(
    store: EntryStore, git_repo: Path, git_cmd: Callable[..., str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """labbook-79y.10 criterion 2. Catches a large file captured silently, a refusal that
    writes a record, a ref or the store first, and a refusal missing either call."""
    monkeypatch.setenv("TRAILS_MAX_CAPTURE_BYTES", "32")
    _big(git_repo, "data/raw.bin", 2048)
    _big(git_repo, "small.bin", 16)
    records = store.query("SELECT COUNT(*) AS n FROM entry")

    with pytest.raises(ValidationError) as refused:
        _run(store, title="t", repos=[str(git_repo)], exclude=["other"])

    top = git_repo.resolve()
    assert str(refused.value) == (
        f"not written: 1 file(s) over 32 B would be captured from {top}:\n"
        "  data/raw.bin  2.0 KiB\n"
        "If it is data, leave it out and reference it:\n"
        '  write(<the same arguments>, exclude=["other", "data/raw.bin"], artifacts=['
        f'{{"location": "{top}/data/raw.bin", "notes": "<what identifies this data>"}}])\n'
        "If it belongs in the capture:\n"
        '  write(<the same arguments>, allow_large=["data/raw.bin"])'
    )
    assert store.query("SELECT COUNT(*) AS n FROM entry") == records
    assert not _store(store).exists()
    assert git_cmd(git_repo, "for-each-ref", "refs/research/") == ""


def test_exclude_and_allow_large_each_let_the_write_through(
    store: EntryStore, git_repo: Path, git_cmd: Callable[..., str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """labbook-79y.10 criterion 2. Catches either suggested call refused again, an excluded
    file captured anyway, an allowed file left out, and a tracked excluded file kept in the
    capture because the temporary index read it from HEAD."""
    monkeypatch.setenv("TRAILS_MAX_CAPTURE_BYTES", "32")
    _big(git_repo, "committed.bin")
    git_cmd(git_repo, "add", "committed.bin")
    git_cmd(git_repo, "commit", "-qm", "big")
    _big(git_repo, "data/raw.bin")

    excluded = _run(
        store,
        title="t",
        kind="note",
        repos=[str(git_repo)],
        exclude=["data", "committed.bin"],
    )
    allowed = _run(
        store,
        title="t",
        kind="note",
        repos=[str(git_repo)],
        allow_large=["data/raw.bin", "committed.bin"],
    )

    [left_out] = store.get_entry(excluded.id).current.code
    [kept] = store.get_entry(allowed.id).current.code
    assert _files(store, left_out.captured_sha) == ["tracked.txt"]
    assert _files(store, kept.captured_sha) == ["committed.bin", "data/raw.bin", "tracked.txt"]


@pytest.mark.parametrize("how", ["deleted", "git rm", "renamed"])
def test_a_large_committed_file_that_will_not_be_captured_is_not_refused(
    store: EntryStore,
    git_repo: Path,
    git_cmd: Callable[..., str],
    monkeypatch: pytest.MonkeyPatch,
    how: str,
) -> None:
    """labbook-79y.10 review round 1. Catches a committed file deleted from the working tree,
    staged for removal, or renamed away measured at its committed size, which refuses a
    write for a file the capture does not contain."""
    monkeypatch.setenv("TRAILS_MAX_CAPTURE_BYTES", "32")
    _big(git_repo, "big.dat")
    git_cmd(git_repo, "add", "big.dat")
    git_cmd(git_repo, "commit", "-qm", "big")
    if how == "deleted":
        (git_repo / "big.dat").unlink()
    elif how == "git rm":
        git_cmd(git_repo, "rm", "-q", "big.dat")
    else:
        git_cmd(git_repo, "mv", "big.dat", "moved.dat")

    if how == "renamed":
        with pytest.raises(ValidationError) as refused:
            _run(store, title="t", repos=[str(git_repo)])
        assert "  moved.dat  64 B" in str(refused.value)
        assert "big.dat" not in str(refused.value)
    else:
        result = _run(store, title="t", repos=[str(git_repo)])
        [code] = store.get_entry(result.id).current.code
        assert _files(store, code.captured_sha) == ["tracked.txt"]


def test_a_committed_file_a_filter_shrinks_is_measured_on_disk(
    store: EntryStore, git_repo: Path, git_cmd: Callable[..., str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """labbook-79y.10 review round 2. Catches the size check measuring an LFS-style file by
    the pointer HEAD holds while the capture, run with filters off, packs its full bytes:
    a multi-GB model would enter the store with no refusal. Also catches the allowed file
    refused again on the next write once the store holds its content."""
    git_cmd(git_repo, "config", "filter.pointer.clean", "sh -c 'cat >/dev/null; echo pointer'")
    (git_repo / ".gitattributes").write_text("*.bin filter=pointer\n")
    _big(git_repo, "model.bin")
    git_cmd(git_repo, "add", ".gitattributes", "model.bin")
    git_cmd(git_repo, "commit", "-qm", "model")
    assert git_cmd(git_repo, "cat-file", "-s", "HEAD:model.bin") == "8"
    monkeypatch.setenv("TRAILS_MAX_CAPTURE_BYTES", "32")

    with pytest.raises(ValidationError, match=r"  model\.bin  64 B"):
        _run(store, title="t", repos=[str(git_repo)])
    allowed = _run(store, title="t", repos=[str(git_repo)], allow_large=["model.bin"])
    again = _run(store, title="t", repos=[str(git_repo)])

    [code] = store.get_entry(allowed.id).current.code
    assert _show(store, code.captured_sha, "model.bin") == "x" * 64
    assert store.get_entry(again.id).current.code


def test_a_large_committed_blob_already_in_the_store_is_not_refused_twice(
    store: EntryStore, git_repo: Path, git_cmd: Callable[..., str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """labbook-79y.10 section 2. Catches every later capture of the repository refused for a
    file the user already allowed once, which the store already holds."""
    monkeypatch.setenv("TRAILS_MAX_CAPTURE_BYTES", "32")
    _big(git_repo, "model.bin")
    git_cmd(git_repo, "add", "model.bin")
    git_cmd(git_repo, "commit", "-qm", "model")
    _run(store, title="t", repos=[str(git_repo)], allow_large=["model.bin"])

    again = _run(store, title="t", repos=[str(git_repo)])

    assert store.get_entry(again.id).current.code


def test_the_write_result_reports_the_capture_and_the_commands_to_keep_paths_out(
    store: EntryStore, git_repo: Path
) -> None:
    """labbook-79y.10 section 3. Catches the capture report missing or miscounted, and the
    printf lines unquoted or pointed at the wrong file. trails runs neither command."""
    (git_repo / "tracked.txt").write_text("edited!\n")
    (git_repo / "b.txt").write_text("bb\n")
    (git_repo / "my data").mkdir()
    (git_repo / "my data" / "x").write_text("x\n")
    gitignore = git_repo / ".gitignore"

    result = _run(store, title="t", repos=[str(git_repo)], exclude=["my data"])

    [item] = result.captures
    top = str(git_repo.resolve())
    assert (item.changed_files, item.changed_bytes) == (2, 11)
    assert item.largest == [("tracked.txt", 8), ("b.txt", 3)]
    lines = result.render_text().splitlines()
    assert lines[2:6] == [
        f"captured {top} @ {item.captured_sha[:12]}: 2 changed files, 11 B; largest: "
        "tracked.txt 8 B, b.txt 3 B",
        "excluded from this capture: my data",
        "To keep them out of future captures and of git: printf '%s\\n' '/my data' >> "
        f"{top}/.gitignore",
        "To keep them out locally without editing .gitignore: printf '%s\\n' '/my data' >> "
        f"{top}/.git/info/exclude",
    ]
    assert not gitignore.exists()


def test_exclude_without_repos_is_refused(store: EntryStore) -> None:
    """Catches exclude accepted and silently doing nothing."""
    with pytest.raises(ValidationError, match="apply to repos"):
        _run(store, title="t", exclude=["data"])


def test_human_size_units() -> None:
    """Catches a unit boundary off by one, which mislabels a file at the cap."""
    assert [capture.human_size(n) for n in (1023, 1024, 50 * 1024 * 1024, 3 * 1024**3)] == [
        "1023 B",
        "1.0 KiB",
        "50.0 MiB",
        "3.0 GiB",
    ]
