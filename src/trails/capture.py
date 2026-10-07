"""Capture a repository's full working tree into trails' own git store, without touching it.

NOTHING IS WRITTEN TO THE USER'S REPOSITORY: no file is created, and no file's content
changes, whether object, ref, index or lock file. The one thing git itself still does is
"freshen" an existing file: it updates the mtime of a loose object it finds in the
repository's object directory instead of writing a duplicate, and of an existing
sharedindex file it reads. git does both during ordinary read commands, and no git setting
turns it off. Probed 2026-10-02 on git 2.54.0, with file contents compared by sha256 before
and after a capture of an LFS repository and of a split-index repository: no content changed.
Every git command that builds a capture runs with three environment variables set:

* `GIT_INDEX_FILE` names a temporary index, so the user's index is never read for writing;
* `GIT_OBJECT_DIRECTORY` names `<store>/objects`, so every new object lands in the store;
* `GIT_ALTERNATE_OBJECT_DIRECTORIES` names the repository's own object directory, so objects
  the repository already holds are read from it rather than copied.

AND EVERY GIT COMMAND RUN IN THE USER'S REPOSITORY CARRIES `-c` OVERRIDES (`_overrides`) for
the settings that would otherwise make git write into its `.git` from a read or from an add
into a temporary index: every configured filter driver (git-lfs's clean filter writes
`.git/lfs/objects`), `core.splitIndex` (writes `sharedindex.*`), `core.fsmonitor` (starts a
daemon with a socket in `.git`), `core.untrackedCache`, and automatic gc and maintenance. With
the filters off, the capture holds each file's bytes as they are on disk, so an LFS-tracked
file is captured as its content. The size check measures a committed file that a configured
filter transforms by its size on disk, not by the pointer HEAD holds, and the capture of such
a file counts as changed relative to HEAD, which makes a clean LFS repository's capture dirty.

The capture commit has NO PARENT. A parent would make the store's copy step (`rev-list
--objects`) copy the repository's whole history, including large files deleted long ago that
the size check never saw. The commit the repository had checked out is recorded as
`head_sha` in the record and named in the commit message.

After the commit is built, every object it reaches that the store does not hold on its own is
packed into the store, so the capture reads with the source repository gone. The pin is
`refs/research/<record_id>/<n>` in the store, n being the repository's position in the
publish call's repos list.

`plan_capture` runs first, before the record id is minted: it lists every file the capture
would take and finds the ones over the size cap. `capture_repo` then captures, scans and
warns, and lists the captured commit once in the store for gitlinks, sizes and changed files.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from trails import gitprobe
from trails.errors import TrailsError, ValidationError
from trails.models import CaptureSummary, CodeCapture, SecretFinding
from trails.secret_scan import scan_for_secrets

STORE_DIR = "captures.git"
MAX_CAPTURE_BYTES_ENV = "TRAILS_MAX_CAPTURE_BYTES"
DEFAULT_MAX_CAPTURE_BYTES = 50 * 1024 * 1024
LARGEST_SHOWN = 3
# Settings that make a read or a temporary-index add write into the user's .git. Filter
# drivers are found per repository and added to these by `_overrides`.
_FIXED_OVERRIDES = (
    "core.fsmonitor=false",
    "core.splitIndex=false",
    "core.untrackedCache=false",
    "gc.auto=0",
    "maintenance.auto=false",
)

# git's refusal to add a nested repository that has no commit, as printed by git 2.54.0 with
# LC_ALL=C. Without LC_ALL=C a translated message would not match and the add would fail.
_UNBORN_REFUSAL = re.compile(r"^error: '(.*)/' does not have a commit checked out$", re.MULTILINE)
# Variables that would point a git command at some other repository or index than intended.
_INHERITED_GIT = (
    "GIT_DIR",
    "GIT_WORK_TREE",
    "GIT_INDEX_FILE",
    "GIT_OBJECT_DIRECTORY",
    "GIT_ALTERNATE_OBJECT_DIRECTORIES",
    "GIT_COMMON_DIR",
)
_IDENTITY = {
    "GIT_AUTHOR_NAME": "trails",
    "GIT_AUTHOR_EMAIL": "trails@localhost",
    "GIT_COMMITTER_NAME": "trails",
    "GIT_COMMITTER_EMAIL": "trails@localhost",
}


def max_capture_bytes() -> int:
    """`$TRAILS_MAX_CAPTURE_BYTES` when it parses as an integer, else 50 MiB."""
    raw = os.environ.get(MAX_CAPTURE_BYTES_ENV, "").strip()
    try:
        return int(raw)
    except ValueError:
        return DEFAULT_MAX_CAPTURE_BYTES


def human_size(size: int) -> str:
    """Bytes as `512 B`, `1.5 KiB`, `50.0 MiB` or `2.1 GiB`."""
    if size < 1024:
        return f"{size} B"
    value = float(size)
    for unit in ("KiB", "MiB", "GiB"):
        value /= 1024
        if value < 1024 or unit == "GiB":
            return f"{value:.1f} {unit}"
    raise AssertionError("unreachable")  # pragma: no cover


def store_path(db_path: Path) -> Path:
    """Where the capture store of the database at `db_path` is, whether or not it exists."""
    return db_path / STORE_DIR


def open_store(db_path: Path) -> Path:
    """The capture store, created with `git init --bare` on first use.

    Raises:
        TrailsError: git init failed.
    """
    store = store_path(db_path)
    if (store / "HEAD").is_file():
        return store
    with gitprobe.git_lock(store):
        if not (store / "HEAD").is_file():
            _run(None, "init", "--bare", "-q", str(store))
    return store


@dataclass(frozen=True)
class CapturePlan:
    """What one repository's capture would take, found before anything is written."""

    top: Path  # the repository's top level
    objects: Path  # the repository's own object directory, used as an alternate
    head: str | None  # the commit checked out, None when there is none
    config: tuple[str, ...]  # the `-c` arguments every git command in this repository carries
    head_blobs: dict[str, str]  # committed path -> blob oid, from one ls-tree of head
    exclude: list[str]  # the publish call's exclude entries, relative to top
    large: list[tuple[str, int]]  # (relpath, bytes) over the cap and not allowed, largest first


@dataclass(frozen=True)
class CaptureOutcome:
    """One repository's capture, its report, the possible secrets in it, and what it left out."""

    code: CodeCapture
    summary: CaptureSummary
    findings: list[SecretFinding]
    warnings: list[str]


def plan_capture(
    repo: Path, store: Path, exclude: Sequence[str], allow_large: Sequence[str]
) -> CapturePlan:
    """List the files a capture of `repo` would take and find those over the size cap.

    Candidates are untracked files (`ls-files --others --exclude-standard`), files changed
    since HEAD (`diff --name-only`, or every indexed file when there is no commit), and
    committed files (`ls-tree -r -l`). A file is large when its size exceeds
    `max_capture_bytes()` and its blob is not already in the store; a committed file that a
    filter driver transforms is measured on disk. A path under an exclude entry, a path in allow_large, and a nested
    repository are skipped. Every command here only reads.

    Raises:
        ValidationError: repo is not a git repository.
        TrailsError: a git command failed.
    """
    top = _toplevel(repo)
    drivers = _filter_drivers(top)
    config = _overrides(drivers)
    objects = Path(
        _run(top, "rev-parse", "--path-format=absolute", "--git-common-dir", config=config)
    )
    objects = objects / "objects"
    head = _head(top)
    excluded = _relative(top, exclude)
    allowed = set(_relative(top, allow_large))
    cap = max_capture_bytes()

    def skipped(path: str) -> bool:
        return path in allowed or any(path == e or path.startswith(f"{e}/") for e in excluded)

    on_disk = [
        path
        for path in _split(
            _run(top, "ls-files", "-z", "--others", "--exclude-standard", config=config)
        )
        if not path.endswith("/")
    ]
    if head is None:
        on_disk += _split(_run(top, "ls-files", "-z", "--cached", config=config))
    else:
        on_disk += _split(
            _run(
                top,
                "--no-optional-locks",
                "diff",
                "--no-renames",
                "--name-only",
                "-z",
                head,
                config=config,
            )
        )
    # Every path listed above is measured on disk or, when it is gone, not captured at all; a
    # committed path is measured from HEAD only when it is in neither list, so a file deleted
    # since HEAD (or staged for removal, or the old side of a rename) is never size-checked.
    listed = set(on_disk)
    sizes: dict[str, int] = {}
    for path in dict.fromkeys(on_disk):
        try:
            stat = (top / path).lstat()
        except OSError:
            continue  # deleted since HEAD
        if not (top / path).is_dir():
            sizes[path] = stat.st_size

    head_blobs: dict[str, str] = {}
    committed: dict[str, tuple[str, int]] = {}
    if head is not None:
        for meta, path in _entries(_run(top, "ls-tree", "-r", "-l", "-z", head, config=config)):
            mode_type_oid, _, size = meta.rpartition(" ")
            _mode, kind, oid = mode_type_oid.split()
            if kind != "blob":
                continue
            head_blobs[path] = oid
            if path not in listed and size.strip().isdigit():
                committed[path] = (oid, int(size))

    large = _large_files(
        top,
        store,
        sizes=sizes,
        committed=committed,
        drivers=drivers,
        config=config,
        cap=cap,
        skipped=skipped,
    )
    large.sort(key=lambda item: (-item[1], item[0]))
    return CapturePlan(
        top=top,
        objects=objects,
        head=head,
        config=config,
        head_blobs=head_blobs,
        exclude=excluded,
        large=large,
    )


def size_refusal(
    plans: Sequence[CapturePlan], exclude: Sequence[str], allow_large: Sequence[str]
) -> str | None:
    """The text refusing a publish that would capture a file over the cap, or None.

    Every element comes from the plans, `max_capture_bytes()` and the given lists; `<the same
    arguments>` and `<what identifies this data>` are fixed placeholders for the agent to fill.
    """
    blocked = [plan for plan in plans if plan.large]
    if not blocked:
        return None
    cap = human_size(max_capture_bytes())
    lines: list[str] = []
    new: list[str] = []
    references: list[str] = []
    for plan in blocked:
        lines.append(
            f"not published: {len(plan.large)} file(s) over {cap} would be captured from "
            f"{plan.top}:"
        )
        for path, size in plan.large:
            lines.append(f"  {path}  {human_size(size)}")
            new.append(path)
            location = json.dumps(f"{plan.top}/{path}")
            references.append(f'{{"location": {location}, "notes": "<what identifies this data>"}}')
    added = list(dict.fromkeys(new))
    lines += [
        "If it is data, leave it out and reference it:",
        f"  publish(<the same arguments>, exclude={_list([*exclude, *added])}, "
        f"artifacts=[{', '.join(references)}])",
        "If it belongs in the capture:",
        f"  publish(<the same arguments>, allow_large={_list([*allow_large, *added])})",
    ]
    return "\n".join(lines)


def capture_repo(
    plan: CapturePlan,
    store: Path,
    record_id: str,
    position: int,
    captured_elsewhere: frozenset[Path] = frozenset(),
) -> CaptureOutcome:
    """Capture the planned repository's full working tree into the store, scan it, and report.

    A WARNING NAMES EVERY NESTED REPOSITORY WHOSE FILES OR CHANGES ARE NOT IN THE CAPTURE,
    because git records a nested repository as one pointer at a commit and never its files:
    an untracked nested repository, a nested repository with no commit (left out of the
    capture entirely, since git cannot record it), and a registered submodule with
    uncommitted changes. Each warning tells the agent to pass that path in repos, which
    captures it as its own repository. A path in `captured_elsewhere` (resolved paths of the
    other repos in the same publish) is being captured that way already and gets no warning.

    Raises:
        TrailsError: a git command failed; message carries the command and stderr.
    """
    top = plan.top
    code, unborn = _commit_tree(plan, store, record_id, position)
    listing = _entries(_run_store(store, "ls-tree", "-r", "-l", "-z", code.captured_sha))
    blobs: dict[str, tuple[str, int]] = {}
    gitlinks: list[str] = []
    for meta, path in listing:
        mode_type_oid, _, size = meta.rpartition(" ")
        mode, kind, oid = mode_type_oid.split()
        if mode == "160000":
            gitlinks.append(path)
        elif kind == "blob":
            blobs[path] = (oid, int(size))
    changed = {p: b for p, b in blobs.items() if plan.head_blobs.get(p) != b[0]}
    largest = sorted(((p, b[1]) for p, b in changed.items()), key=lambda i: (-i[1], i[0]))

    submodules = _submodule_paths(store, code.captured_sha) if gitlinks else set()
    warnings = [
        f"not captured: {top}/{path} is a nested git repository with no commit, so it was "
        "left out of this capture; pass it in repos to capture it"
        for path in unborn
        if (top / path).resolve() not in captured_elsewhere
    ]
    for path in gitlinks:
        if (top / path).resolve() in captured_elsewhere:
            continue
        if path not in submodules:
            warnings.append(
                f"not captured: {top}/{path} is a nested git repository, so its files are not "
                "in this capture; pass it in repos to capture it"
            )
        elif _has_uncommitted_changes(top / path, plan.config):
            warnings.append(
                f"not captured: {top}/{path} is a submodule with uncommitted changes, and this "
                "capture records only its commit; pass it in repos to capture the changes"
            )
    summary = CaptureSummary(
        repo_path=str(top),
        git_common_dir=str(plan.objects.parent),
        captured_sha=code.captured_sha,
        changed_files=len(changed),
        changed_bytes=sum(size for _oid, size in changed.values()),
        largest=largest[:LARGEST_SHOWN],
        excluded=plan.exclude,
    )
    findings = scan_capture(store, {path: oid for path, (oid, _size) in changed.items()})
    return CaptureOutcome(code=code, summary=summary, findings=findings, warnings=warnings)


def _commit_tree(
    plan: CapturePlan, store: Path, record_id: str, position: int
) -> tuple[CodeCapture, list[str]]:
    """Build the capture commit in the store through a temporary index, and pin it.

    Returns the capture and the nested repositories with no commit that were left out.
    Dirtiness compares the captured tree against `<head>^{tree}`, the commit read when the
    plan was made, so a commit landing during the capture cannot change the answer.
    """
    top, head, config = plan.top, plan.head, plan.config
    with gitprobe.git_lock(store), tempfile.TemporaryDirectory(prefix="trails-index-") as tmp:
        env = {
            **_clean_env(),
            **_IDENTITY,
            "GIT_INDEX_FILE": str(Path(tmp) / "index"),
            "GIT_OBJECT_DIRECTORY": str(store / "objects"),
            "GIT_ALTERNATE_OBJECT_DIRECTORIES": ":".join(
                _quoted_alternate(path) for path in _alternates(plan.objects)
            ),
        }
        if head is not None:
            _run(top, "read-tree", head, env=env, config=config)
        unborn = _add_all(top, plan.exclude, env, config)
        if plan.exclude:
            _run(
                top,
                "rm",
                "--cached",
                "-r",
                "-q",
                "--ignore-unmatch",
                "--",
                *(f":(literal){path}" for path in plan.exclude),
                env=env,
                config=config,
            )
        tree = _run(top, "write-tree", env=env, config=config)
        message = f"trails capture {record_id} of {top} at {head or 'no commit'}"
        sha = _run(top, "commit-tree", tree, "-m", message, env=env, config=config)
        _make_self_contained(top, store, sha, env, config)
        gitprobe.pin_commit(store, f"refs/research/{record_id}/{position}", sha)
    dirty = head is None or tree != _run(top, "rev-parse", f"{head}^{{tree}}", config=config)
    capture = CodeCapture(repo_path=str(top), head_sha=head, captured_sha=sha, dirty=dirty)
    return capture, unborn


def _make_self_contained(
    top: Path, store: Path, sha: str, env: dict[str, str], config: Sequence[str]
) -> None:
    """Pack into the store every object `sha` reaches that the store does not hold itself.

    Objects the repository already had were read through the alternate and never copied, so
    without this the capture would stop reading the moment the repository is deleted.
    """
    listing = _run(top, "rev-list", "--objects", sha, env=env, config=config)
    reached = [line.split(" ", 1)[0] for line in listing.splitlines() if line]
    held = _present_in_store(store, set(reached))
    missing = [oid for oid in reached if oid not in held]
    if not missing:
        return
    pack = str(store / "objects" / "pack" / "pack")
    completed = subprocess.run(
        ["git", f"--git-dir={store}", "pack-objects", "-q", pack],
        input="\n".join(missing) + "\n",
        capture_output=True,
        text=True,
        check=False,
        env={
            **_clean_env(),
            "GIT_ALTERNATE_OBJECT_DIRECTORIES": env["GIT_ALTERNATE_OBJECT_DIRECTORIES"],
        },
    )
    if completed.returncode != 0:
        raise TrailsError(f"git pack-objects failed in {store}: {completed.stderr.strip()}")


def _present_in_store(store: Path, oids: set[str]) -> set[str]:
    """The oids the store holds by itself, read with no alternate. Empty for no store yet."""
    if not oids or not (store / "HEAD").is_file():
        return set()
    completed = subprocess.run(
        ["git", f"--git-dir={store}", "cat-file", "--batch-check"],
        input="\n".join(sorted(oids)) + "\n",
        capture_output=True,
        text=True,
        check=False,
        env=_clean_env(),
    )
    if completed.returncode != 0:
        raise TrailsError(f"git cat-file --batch-check failed in {store}: {completed.stderr}")
    return {
        line.split(" ", 1)[0]
        for line in completed.stdout.splitlines()
        if line and not line.endswith(" missing")
    }


def _add_all(
    top: Path, exclude: Sequence[str], env: dict[str, str], config: Sequence[str]
) -> list[str]:
    """`git add -A` into the temporary index, leaving out excluded paths and nested
    repositories with no commit.

    git refuses such a nested directory ("does not have a commit checked out") and reports
    only the first one it meets, so each refusal adds an exclusion and the add is retried.
    Every retry excludes one more path, so the loop ends. Returns the nested paths left out.
    """
    unborn: list[str] = []
    while True:
        excludes = [f":(exclude,literal){path}" for path in [*exclude, *unborn]]
        completed = subprocess.run(
            ["git", *_flags(config), "-C", str(top), "add", "-A", "--", ".", *excludes],
            capture_output=True,
            text=True,
            check=False,
            env=env,
        )
        if completed.returncode == 0:
            return unborn
        refused = _UNBORN_REFUSAL.search(completed.stderr)
        if refused is None or refused.group(1) in unborn:
            raise TrailsError(f"git add -A failed in {top}: {completed.stderr.strip()}")
        unborn.append(refused.group(1))


def _has_uncommitted_changes(submodule: Path, config: Sequence[str]) -> bool:
    """Whether a checked-out submodule's working tree differs from its own HEAD.

    `--no-optional-locks` keeps status from refreshing and rewriting the submodule's index.
    An uninitialized submodule (an empty directory, not its own repository) is False.
    """
    toplevel = subprocess.run(
        ["git", "-C", str(submodule), "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        check=False,
        env=_clean_env(),
    )
    if toplevel.returncode != 0 or Path(toplevel.stdout.strip()).resolve() != submodule.resolve():
        return False
    # The submodule's own filter drivers are switched off too: status runs the clean filter on
    # a file whose stat data looks modified.
    flags = (*config, *_overrides(_filter_drivers(submodule)))
    return _run(submodule, "--no-optional-locks", "status", "--porcelain", config=flags) != ""


def _submodule_paths(store: Path, sha: str) -> set[str]:
    # --null prints each entry as "<key>\n<value>\0". The key embeds the submodule's name,
    # which may contain spaces, so splitting a plain output line on its first space misreads it.
    completed = subprocess.run(
        [
            "git",
            f"--git-dir={store}",
            "config",
            "--null",
            "--blob",
            f"{sha}:.gitmodules",
            "--get-regexp",
            r"\.path$",
        ],
        capture_output=True,
        text=True,
        check=False,
        env=_clean_env(),
    )
    if completed.returncode != 0:
        return set()
    return {entry.split("\n", 1)[1] for entry in completed.stdout.split("\0") if "\n" in entry}


def scan_capture(store: Path, blobs: dict[str, str]) -> list[SecretFinding]:
    """Scan each captured file that changed since HEAD, when it decodes as UTF-8.

    `blobs` maps a path to its blob oid in the store; all are read in one `cat-file --batch`.

    Raises:
        TrailsError: a listed blob is not in the store.
    """
    if not blobs:
        return []
    paths = list(blobs)
    completed = subprocess.run(
        ["git", f"--git-dir={store}", "cat-file", "--batch"],
        input=b"".join(f"{blobs[path]}\n".encode() for path in paths),
        capture_output=True,
        check=False,
        env=_clean_env(),
    )
    if completed.returncode != 0:
        raise TrailsError(f"git cat-file --batch failed in {store}: {completed.stderr!r}")
    out, offset = completed.stdout, 0
    fields: dict[str, str] = {}
    for path in paths:
        end = out.index(b"\n", offset)
        header = out[offset:end].decode()
        if header.endswith(" missing"):
            raise TrailsError(f"git cat-file: blob {blobs[path]} of {path} is not in {store}")
        size = int(header.rsplit(" ", 1)[1])
        content = out[end + 1 : end + 1 + size]
        offset = end + 1 + size + 1
        try:
            fields[f"file:{path}"] = content.decode("utf-8")
        except UnicodeDecodeError:
            continue
    return scan_for_secrets(fields)


def _filter_drivers(top: Path) -> list[str]:
    """Every filter driver configured for `top` in any scope, such as `lfs`."""
    completed = subprocess.run(
        ["git", "-C", str(top), "config", "--null", "--name-only", "--get-regexp", r"^filter\."],
        capture_output=True,
        text=True,
        check=False,
        env=_clean_env(),
    )
    return sorted(
        {
            name.removeprefix("filter.").rsplit(".", 1)[0]
            for name in completed.stdout.split("\0")
            if name.startswith("filter.") and name.count(".") >= 2
        }
    )


def _overrides(drivers: Sequence[str]) -> tuple[str, ...]:
    """The `-c` settings that keep git from writing into the repository's .git: the fixed
    ones, and an empty clean and process command for each of its filter `drivers`.

    Probed on git 2.54.0: an empty `filter.<driver>.clean` disables the driver, and with
    `required=false` a driver required by .gitattributes does not fail the add.
    """
    disabled = [
        f"filter.{driver}.{key}={value}"
        for driver in drivers
        for key, value in (("clean", ""), ("process", ""), ("required", "false"))
    ]
    return (*_FIXED_OVERRIDES, *disabled)


def _large_files(
    top: Path,
    store: Path,
    *,
    sizes: dict[str, int],
    committed: dict[str, tuple[str, int]],
    drivers: Sequence[str],
    config: Sequence[str],
    cap: int,
    skipped: Callable[[str], bool],
) -> list[tuple[str, int]]:
    """Files the capture would take that are over the cap, less those the store already holds.

    `sizes` are files measured on disk (untracked, or changed since HEAD); `committed` are
    the others, with their blob and size from HEAD. A committed file unchanged since HEAD is
    not large when the store already holds its blob, from an earlier capture that allowed it.

    A file a configured filter driver transforms (an LFS file) is captured as its bytes on
    disk, because the capture runs with every filter off, while HEAD holds only what the
    filter produced (the LFS pointer). So such a file, committed or listed as changed, is
    measured on disk, and when it is over the cap its unfiltered blob is computed with
    `hash-object --no-filters`, which writes nothing, and looked for in the store. Other files
    measured on disk are never hashed, because hashing a large data file only to refuse it
    would read all of it.
    """
    over = [p for p, size in sizes.items() if size > cap and not skipped(p)]
    filtered = set(_filtered_paths(top, [*committed, *over], drivers, config))
    size_of = {p: size for p, (_oid, size) in committed.items()} | sizes
    blobs: dict[str, str] = {}
    for path in [p for p in committed if p in filtered]:
        try:
            size_of[path] = (top / path).lstat().st_size
        except OSError:
            del size_of[path]
    for path, (oid, size) in committed.items():
        if path not in filtered and size > cap and not skipped(path):
            blobs[path] = oid
    hashed = [p for p in filtered if p in size_of and size_of[p] > cap and not skipped(p)]
    if hashed:
        blobs.update(_disk_blobs(top, sorted(hashed), config))
    stored = _present_in_store(store, set(blobs.values()))
    large = [(p, size_of[p]) for p in over if p not in filtered]
    large += [(p, size_of[p]) for p, oid in blobs.items() if oid not in stored]
    return large


def _filtered_paths(
    top: Path, paths: Sequence[str], drivers: Sequence[str], config: Sequence[str]
) -> list[str]:
    """The `paths` whose `filter` attribute names one of `drivers`, from one check-attr."""
    if not paths or not drivers:
        return []
    completed = subprocess.run(
        ["git", *_flags(config), "-C", str(top), "check-attr", "-z", "--stdin", "filter"],
        input="\0".join(paths) + "\0",
        capture_output=True,
        text=True,
        check=False,
        env=_clean_env(),
    )
    if completed.returncode != 0:
        raise TrailsError(f"git check-attr failed in {top}: {completed.stderr.strip()}")
    fields = completed.stdout.split("\0")
    named = set(drivers)
    return [fields[i] for i in range(0, len(fields) - 2, 3) if fields[i + 2] in named]


def _disk_blobs(top: Path, paths: Sequence[str], config: Sequence[str]) -> dict[str, str]:
    """The blob oid each file's bytes on disk would have, unfiltered, without writing it."""
    completed = subprocess.run(
        ["git", *_flags(config), "-C", str(top), "hash-object", "--no-filters", "--stdin-paths"],
        input="\n".join(paths) + "\n",
        capture_output=True,
        text=True,
        check=False,
        env=_clean_env(),
    )
    if completed.returncode != 0:
        raise TrailsError(f"git hash-object failed in {top}: {completed.stderr.strip()}")
    return dict(zip(paths, completed.stdout.split(), strict=True))


def _flags(config: Sequence[str]) -> list[str]:
    return [flag for setting in config for flag in ("-c", setting)]


def _quoted_alternate(path: str) -> str:
    """One GIT_ALTERNATE_OBJECT_DIRECTORIES entry, C-quoted when it holds a separator or a
    quote, which git otherwise splits or misreads."""
    if ":" not in path and '"' not in path and "\\" not in path:
        return path
    return '"' + path.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _alternates(objects: Path) -> list[str]:
    """The repository's object directory, then any alternates it borrows from itself."""
    found = [str(objects)]
    listed = objects / "info" / "alternates"
    if listed.is_file():
        for line in listed.read_text().splitlines():
            if line.strip() and not line.startswith("#"):
                found.append(str((objects / line.strip()).resolve()))
    return found


def _relative(top: Path, paths: Sequence[str]) -> list[str]:
    """Each entry relative to `top`: a relative entry as given, an absolute one inside `top`
    made relative, an absolute one elsewhere dropped as belonging to another repository."""
    found: list[str] = []
    for raw in paths:
        entry = raw.strip().rstrip("/")
        if not entry:
            continue
        if entry.startswith(("/", "~")):
            try:
                absolute = Path(entry).expanduser().resolve()
            except RuntimeError:
                continue
            if absolute == top or not absolute.is_relative_to(top):
                continue
            entry = absolute.relative_to(top).as_posix()
        found.append(entry)
    return list(dict.fromkeys(found))


def _list(items: Sequence[str]) -> str:
    return "[" + ", ".join(json.dumps(item) for item in dict.fromkeys(items)) + "]"


def _split(listing: str) -> list[str]:
    return [item for item in listing.split("\0") if item]


def _entries(listing: str) -> list[tuple[str, str]]:
    """`ls-tree -z` output as (metadata, path) pairs."""
    pairs = []
    for entry in _split(listing):
        meta, _, path = entry.partition("\t")
        pairs.append((meta, path))
    return pairs


def _toplevel(repo: Path) -> Path:
    completed = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "--show-toplevel"],
        capture_output=True,
        text=True,
        check=False,
        env=_clean_env(),
    )
    if completed.returncode != 0 or not completed.stdout.strip():
        raise ValidationError(f"{repo} is not a git repository")
    return Path(completed.stdout.strip()).resolve()


def _head(top: Path) -> str | None:
    completed = subprocess.run(
        ["git", "-C", str(top), "rev-parse", "--verify", "-q", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
        env=_clean_env(),
    )
    return completed.stdout.strip() if completed.returncode == 0 else None


def _clean_env() -> dict[str, str]:
    """The environment with git's repository-selecting variables removed and LC_ALL=C."""
    env = {key: value for key, value in os.environ.items() if key not in _INHERITED_GIT}
    env["LC_ALL"] = "C"
    return env


def _run_store(store: Path, *args: str) -> str:
    """One git command in the store, read with no alternate."""
    completed = subprocess.run(
        ["git", f"--git-dir={store}", *args],
        capture_output=True,
        text=True,
        check=False,
        env=_clean_env(),
    )
    if completed.returncode != 0:
        raise TrailsError(f"git {' '.join(args)} failed in {store}: {completed.stderr.strip()}")
    return completed.stdout.strip("\n")


def _run(
    top: Path | None,
    *args: str,
    env: dict[str, str] | None = None,
    config: Sequence[str] = (),
) -> str:
    """Run one git command; stdout without trailing newlines, or TrailsError with stderr.

    `config` is passed as `-c` settings; a command in the user's repository passes the plan's.
    """
    command = ["git", *_flags(config), *(["-C", str(top)] if top is not None else []), *args]
    completed = subprocess.run(
        command, capture_output=True, text=True, check=False, env=env or _clean_env()
    )
    if completed.returncode != 0:
        where = f" in {top}" if top is not None else ""
        raise TrailsError(f"git {' '.join(args)} failed{where}: {completed.stderr.strip()}")
    return completed.stdout.rstrip("\n")
