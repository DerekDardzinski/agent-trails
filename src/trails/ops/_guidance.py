"""Deterministic guidance for the create and revise operations: code or data an entry names where a later reader will
not find it.

NOTE: no `from __future__ import annotations` in this package. See `ops/__init__.py`.
"""

import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from trails import gitprobe
from trails.locations import is_code, safe_resolve, temporary_paths_in_text, temporary_root
from trails.models import ArtifactRef
from trails.reachability import local_path


def _rewrite(kind: str, entry_id: str, version: int, repo: str | None = None) -> str:
    """The call that writes the next version with the location fixed. Only a note carries
    artifacts and captured code; any other kind records a location in its body."""
    call = f'revise_{kind}("{entry_id}", base_version={version}, reason="..."'
    if kind != "note":
        return call + ", body=...)"
    if repo is not None:
        return call + f', repos=[{{"path": "{repo}"}}])'
    return call + ", artifacts=[...])"


def _with_corrected(
    entry_id: str, version: int, artifacts: Sequence[ArtifactRef], position: int
) -> str:
    """The revise_note call that keeps every artifact and replaces one location. A revise's
    artifacts list replaces the whole list, so the call carries all of them."""
    refs = [ref.model_dump(mode="json") for ref in artifacts]
    refs[position]["location"] = "<corrected path>"
    return (
        f'revise_note("{entry_id}", base_version={version}, reason="...", '
        f"artifacts={json.dumps(refs)})"
    )


@dataclass(frozen=True)
class CapturedRepo:
    """One repository this write captured: its resolved top and its exclude entries."""

    top: Path
    exclude: tuple[str, ...]


TEXT_PATHS_SHOWN = 10


def uncaptured_location_warnings(  # noqa: PLR0917 — one warning check over a write
    kind: str,
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

    - a local artifact that does not exist: a later reader cannot find the data; existence
      only, never a digest (principle 3), and a URI such as s3:// is not checked;
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
        warning = (
            f"{where} does not exist on this machine, so a later reader cannot find the "
            "data it names; it is usually a typo or a file that was never saved. If it "
            "lives elsewhere, record the corrected path: "
            f"{_with_corrected(entry_id, version, artifacts, position)}"
            if not path.exists()
            else _located_warning(kind, entry_id, version, where, path, top, captured)
        )
        if warning is not None:
            warnings.append(warning)

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
        if kind != "note":
            # Only a note carries artifacts and captured code; any other kind records where
            # its files went in its body.
            scripts = []
            advice = "Move them somewhere permanent and write the new locations in the body"
        elif scripts:
            advice = (
                "Commit the scripts to a git repository and pass it in repos; move data "
                "somewhere permanent and name it in artifacts"
            )
        else:
            advice = "Move them somewhere permanent and name them in artifacts"
        warnings.append(
            f"the entry names {_count(len(mentioned), 'path')} in temporary directories, which the "
            f"operating system may delete, and trails keeps no copy: {shown}. {advice}: "
            f"{_rewrite(kind, entry_id, version, '<repository>' if scripts else None)}"
        )
    return warnings


def _located_warning(  # noqa: PLR0917 — the facts one artifact's warning is built from
    kind: str,
    entry_id: str,
    version: int,
    where: str,
    path: Path,
    top: Path | None,
    captured: Sequence[CapturedRepo],
) -> str | None:
    """The warning for an artifact that exists but where a later reader may not find it."""
    temp = temporary_root(path)
    code = is_code(path)
    if temp is not None and code:
        fix = (
            "Write a new version that captures that repository"
            if top is not None
            else "Commit the script to a git repository, then write a new version that captures it"
        )
        return (
            f"{where} is a script in the temporary directory {temp}, which the operating "
            "system may delete; trails keeps a reference to it, never a copy, so the code "
            f"that produced this entry will be lost. {fix}: "
            f"{_rewrite(kind, entry_id, version, str(top) if top else '<repository>')}"
        )
    if temp is not None:
        return (
            f"{where} is in the temporary directory {temp}, which the operating system "
            "may delete; trails keeps a reference to it, never a copy. Move it somewhere "
            f"permanent and record the new location: {_rewrite(kind, entry_id, version)}"
        )
    if code and top is None:
        return (
            f"{where} is code outside any git repository, so trails could not capture it, "
            "and a later edit or deletion loses the version that produced this entry. "
            f"Commit it to a git repository, then write a new version that captures it: "
            f"{_rewrite(kind, entry_id, version, '<repository>')}"
        )
    if code and top is not None:
        return (
            f"{where} is code in the git repository {top}, but {_why_not_captured(path, top, captured)}. "
            f"To pin the exact code that produced this entry: {_rewrite(kind, entry_id, version, str(top))}"
        )
    return None


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
