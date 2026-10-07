"""Deterministic agent guidance shared by writing operations."""

from collections.abc import Mapping, Sequence

from trails.models import Link, Record
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
            f"this result {link.relation} {target.id} (hypothesis). prime counts only "
            "builds_on when it decides whether a hypothesis has a result. If this result "
            f'tests that hypothesis, run: link("{record_id}", "builds_on", "{target.id}")'
        )
        seen.add(key)
    return notes
