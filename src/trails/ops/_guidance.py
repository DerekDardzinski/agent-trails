"""Deterministic agent guidance shared by writing operations."""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from trails import gitprobe
from trails.locations import is_code, safe_resolve, temporary_paths_in_text, temporary_root
from trails.models import ArtifactRef, Link, Record
from trails.reachability import local_path
from trails.vocab import EntryType


def result_hypothesis_link_notes(
    record_id: str,
    entry_type: EntryType,
    links: Sequence[Link],
    targets: Mapping[str, Record],
) -> list[str]:
    """Calls that turn a result's cites/contradicts link into evidence when intended."""
    if entry_type != "result":
        return []
    built_on = {link.target_id for link in links if link.relation == "builds_on"}
    notes = []
    seen: set[tuple[str, str]] = set()
    for link in links:
        target = targets.get(link.target_id)
        key = (link.relation, link.target_id)
        if (
            key in seen
            or link.relation not in {"cites", "contradicts"}
            or link.target_id in built_on
            or target is None
            or target.entry_type != "hypothesis"
        ):
            continue
        notes.append(
            f"this result {link.relation} {target.id} (hypothesis). Only builds_on "
            "records that a result tests a hypothesis. If this result "
            f'tests that hypothesis, run: link("{record_id}", "builds_on", "{target.id}")'
        )
        seen.add(key)
    return notes


# How a superseding copy of a record is written: export it, add the reason the header needs,
# and publish the file. Shared by every fix below that asks for a revised record.
def _republish(record_id: str, repo: str | None = None) -> str:
    call = (
        f'export("{record_id}", "<file>"), add reason = "..." to its header, then '
        f'publish(from_file="<file>"'
    )
    if repo is not None:
        call += f', repos=["{repo}"]'
    return call + ")"


@dataclass(frozen=True)
class CapturedRepo:
    """One repository this publish captured: its resolved top and its exclude entries."""

    top: Path
    exclude: tuple[str, ...]


TEXT_PATHS_SHOWN = 10


def uncaptured_location_warnings(
    record_id: str,
    artifacts: Sequence[ArtifactRef],
    texts: Sequence[str],
    captured: Sequence[CapturedRepo],
) -> list[str]:
    """Warnings for code or data the record names where a later reader will not find it.

    Fixed rules over paths and git, no judgment. An artifact that a repository captured by
    this publish holds (present, not excluded, not ignored by git) gets no warning, because
    its content is pinned in the capture store. Otherwise:

    - a local artifact in a temporary directory: the OS may delete it;
    - an artifact with a code suffix outside any git repository: it cannot be captured;
    - an artifact with a code suffix in a repository this publish did not capture, or that the
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
                f"Republish with {top} captured"
                if top is not None
                else "Commit the script to a git repository, then republish with it captured"
            )
            warnings.append(
                f"{where} is a script in the temporary directory {temp}, which the operating "
                "system may delete; trails keeps a reference to it, never a copy, so the code "
                f"that produced this record will be lost. {fix}: "
                f"{_republish(record_id, str(top) if top else '<repository>')}"
            )
        elif temp is not None:
            warnings.append(
                f"{where} is in the temporary directory {temp}, which the operating system "
                "may delete; trails keeps a reference to it, never a copy. Move it somewhere "
                f"permanent and record the new location: {_republish(record_id)}"
            )
        elif code and top is None:
            warnings.append(
                f"{where} is code outside any git repository, so trails could not capture it, "
                "and a later edit or deletion loses the version that produced this record. "
                f"Commit it to a git repository, then republish with it captured: "
                f"{_republish(record_id, '<repository>')}"
            )
        elif code and top is not None:
            warnings.append(
                f"{where} is code in the git repository {top}, but {_why_not_captured(path, top, captured)}. "
                f"To pin the exact code that produced this record: {_republish(record_id, str(top))}"
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
            f"the record names {_count(len(mentioned), 'path')} in temporary directories, which the "
            f"operating system may delete, and trails keeps no copy: {shown}. {advice}: "
            f"{_republish(record_id, '<repository>' if scripts else None)}"
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
        return "this publish did not capture that repository"
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


def contradiction_notes(
    record_id: str,
    links: Sequence[Link],
    targets: Mapping[str, Record],
    superseded_now: str | None = None,
) -> list[str]:
    """For each current target of a contradicts link, the calls that correct it at its source.

    A contradicts link is visible only as an incoming link on the target; a reader who opens
    the target sees its old content first. Superseded and retired targets get no note, nor
    does `superseded_now`, the record this same publish replaces (`targets` were read before
    that write). Neither does a hypothesis: evidence against a hypothesis is its purpose, and
    the hypothesis stays the record of what was tested.
    """
    notes = []
    seen: set[str] = set()
    for link in links:
        target = targets.get(link.target_id)
        if (
            link.relation != "contradicts"
            or target is None
            or target.id in seen
            or target.id == superseded_now
            or target.entry_type == "hypothesis"
            or target.superseded_by is not None
            or target.retired
        ):
            continue
        seen.add(target.id)
        notes.append(
            f"{record_id} contradicts {target.id} ({target.entry_type}), which is still "
            f"current, so a reader of {target.id} sees its content as current belief. If "
            f"{target.id} is wrong, correct it there: {_republish(target.id)} with the corrected "
            f'content, or retire("{target.id}", reason="...") when nothing replaces it'
        )
    return notes


def superseded_target_warnings(
    record_id: str,
    links: Sequence[Link],
    targets: Mapping[str, Record],
    tips: Mapping[str, str],
) -> list[str]:
    """For each builds_on or cites link to a superseded record, its current version and the
    link call that also relates this record to it. `tips` maps a superseded id to its current
    version; a missing entry falls back to the immediate successor."""
    warnings = []
    seen: set[tuple[str, str]] = set()
    for link in links:
        target = targets.get(link.target_id)
        key = (link.relation, link.target_id)
        if (
            link.relation not in {"builds_on", "cites"}
            or target is None
            or target.superseded_by is None
            or key in seen
        ):
            continue
        seen.add(key)
        tip = tips.get(target.id, target.superseded_by)
        warnings.append(
            f"{record_id} {link.relation} {target.id}, which is superseded; its current "
            f"version is {tip}, and a reader of {tip} does not see this link. If this record "
            f'is about the current version, add: link("{record_id}", "{link.relation}", "{tip}")'
        )
    return warnings


REPOINT_CALLS_SHOWN = 20


def repoint_notes(new_id: str, replaced_ids: Sequence[str], sources: Sequence[str]) -> list[str]:
    """For a superseded problem statement: the current records that build on any earlier
    version, each with the link call that also builds them on `new_id`."""
    if not sources:
        return []
    calls = [f'link("{source}", "builds_on", "{new_id}")' for source in sources]
    shown = " · ".join(calls[:REPOINT_CALLS_SHOWN])
    if len(calls) > REPOINT_CALLS_SHOWN:
        shown += (
            f" · and {len(calls) - REPOINT_CALLS_SHOWN} more: "
            f'search(links="builds_on:{replaced_ids[0]}")'
        )
    return [
        f"{_count(len(sources), 'current record')} "
        f"{'builds' if len(sources) == 1 else 'build'} on the replaced problem statement "
        f'({", ".join(replaced_ids)}), and show and search(links="builds_on:{new_id}") do not '
        f"list them under {new_id}. To relate each to {new_id}: {shown}"
    ]
