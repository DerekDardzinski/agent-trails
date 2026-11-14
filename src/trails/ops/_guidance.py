"""Deterministic guidance for `write`: code or data an entry names where a later reader will
not find it.

NOTE: no `from __future__ import annotations` in this package. See `ops/__init__.py`.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from trails import gitprobe
from trails.locations import is_code, safe_resolve, temporary_paths_in_text, temporary_root
from trails.models import ArtifactRef
from trails.reachability import local_path


def _rewrite(entry_id: str, version: int, repo: str | None = None) -> str:
    """The call that writes the next version with the location fixed."""
    call = f'write(id="{entry_id}", base_version={version}, reason="..."'
    if repo is not None:
        call += f', repos=["{repo}"]'
    return call + ", artifacts=[...])" if repo is None else call + ")"


@dataclass(frozen=True)
class CapturedRepo:
    """One repository this write captured: its resolved top and its exclude entries."""

    top: Path
    exclude: tuple[str, ...]


TEXT_PATHS_SHOWN = 10


def uncaptured_location_warnings(
    entry_id: str,
    version: int,
    artifacts: Sequence[ArtifactRef],
    texts: Sequence[str],
    captured: Sequence[CapturedRepo],
) -> list[str]:
    """Warnings for code or data the entry names where a later reader will not find it.

    Fixed rules over paths and git, no judgment. An artifact that a repository captured by
    this write holds (present, not excluded, not ignored by git) gets no warning, because
    its content is pinned in the capture store. Otherwise:

    - a local artifact in a temporary directory: the OS may delete it;
    - an artifact with a code suffix outside any git repository: it cannot be captured;
    - an artifact with a code suffix in a repository this write did not capture, or that the
      capture left out: the call that captures it, with the repository filled in;
    - paths in temporary directories named in `texts` (body, value) that are not artifacts:
      one warning listing them.
    """
    warnings: list[str] = []
    named: set[Path] = set()
    for position, ref in enumerate(artifacts):
        path = local_path(ref.location)
        if path is None:
            continue
        named.add(safe_resolve(path))
        top = gitprobe.containing_toplevel(path)
        if top is not None and _held_by_capture(path, top, captured):
            continue
        where = f"artifact[{position}] {path}"
        temp = temporary_root(path)
        code = is_code(path)
        if temp is not None and code:
            fix = (
                f"Write a new version with {top} captured"
                if top is not None
                else "Commit the script to a git repository, then write a new version with it captured"
            )
            warnings.append(
                f"{where} is a script in the temporary directory {temp}, which the operating "
                "system may delete; trails keeps a reference to it, never a copy, so the code "
                f"that produced this entry will be lost. {fix}: "
                f"{_rewrite(entry_id, version, str(top) if top else '<repository>')}"
            )
        elif temp is not None:
            warnings.append(
                f"{where} is in the temporary directory {temp}, which the operating system "
                "may delete; trails keeps a reference to it, never a copy. Move it somewhere "
                f"permanent and record the new location: {_rewrite(entry_id, version)}"
            )
        elif code and top is None:
            warnings.append(
                f"{where} is code outside any git repository, so trails could not capture it, "
                "and a later edit or deletion loses the version that produced this entry. "
                f"Commit it to a git repository, then write a new version with it captured: "
                f"{_rewrite(entry_id, version, '<repository>')}"
            )
        elif code and top is not None:
            warnings.append(
                f"{where} is code in the git repository {top}, but {_why_not_captured(path, top, captured)}. "
                f"To pin the exact code that produced this entry: {_rewrite(entry_id, version, str(top))}"
            )

    mentioned = [
        text_path
        for text in texts
        for text_path in temporary_paths_in_text(text)
        if safe_resolve(Path(text_path)) not in named
    ]
    mentioned = list(dict.fromkeys(mentioned))
    if mentioned:
        shown = ", ".join(mentioned[:TEXT_PATHS_SHOWN])
        if len(mentioned) > TEXT_PATHS_SHOWN:
            shown += f" and {len(mentioned) - TEXT_PATHS_SHOWN} more"
        scripts = [item for item in mentioned if is_code(Path(item))]
        advice = (
            "Commit the scripts to a git repository and pass it in repos; move data somewhere "
            "permanent and name it in artifacts"
            if scripts
            else "Move them somewhere permanent and name them in artifacts"
        )
        warnings.append(
            f"the entry names {_count(len(mentioned), 'path')} in temporary directories, which the "
            f"operating system may delete, and trails keeps no copy: {shown}. {advice}: "
            f"{_rewrite(entry_id, version, '<repository>' if scripts else None)}"
        )
    return warnings


def _held_by_capture(path: Path, top: Path, captured: Sequence[CapturedRepo]) -> bool:
    repo = next((item for item in captured if item.top == top), None)
    return (
        repo is not None
        and path.is_file()
        and not _excluded(path, repo)
        and not gitprobe.is_ignored(top, path)
    )


def _why_not_captured(path: Path, top: Path, captured: Sequence[CapturedRepo]) -> str:
    repo = next((item for item in captured if item.top == top), None)
    if repo is None:
        return "this write did not capture that repository"
    if not path.is_file():
        return "the file does not exist, so the capture could not hold it"
    if _excluded(path, repo):
        return "it is excluded from this capture"
    return "git ignores it, so the capture left it out"


def _excluded(path: Path, repo: CapturedRepo) -> bool:
    try:
        relative = safe_resolve(path).relative_to(repo.top).as_posix()
    except ValueError:
        return False
    return any(
        relative == entry or relative.startswith(f"{entry}/")
        for entry in (item.strip("/") for item in repo.exclude)
    )


def _count(number: int, noun: str) -> str:
    return f"{number} {noun}" if number == 1 else f"{number} {noun}s"
