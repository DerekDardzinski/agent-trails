"""The response every link operation shares.

NOTE: no `from __future__ import annotations` in this package. See `ops/__init__.py`.
"""

from trails.entry_store import EntryStore
from trails.models import LinkOutcome
from trails.ops._common import redraw
from trails.ops._entry_types import LinkResult


def link_result(store: EntryStore, outcome: LinkOutcome, *, work: bool) -> LinkResult:
    """The link as stored, and for a work link the task graph around both ends."""
    warnings = list(outcome.warnings)
    graph = []
    if work:
        graph, extra = redraw(store, [outcome.source_id, outcome.target_id])
        warnings += extra
    return LinkResult(
        source_id=outcome.source_id,
        relation=outcome.relation,
        target_id=outcome.target_id,
        action=outcome.action,
        changed=outcome.changed,
        target_version=outcome.target_version,
        refreshed_from=outcome.refreshed_from,
        graph=graph,
        warnings=warnings,
    )
