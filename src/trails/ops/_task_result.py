"""Build the response every task-state operation shares.

NOTE: no `from __future__ import annotations` in this package. See `ops/__init__.py`.
"""

from trails.entry_store import EntryStore
from trails.models import TaskOutcome
from trails.ops._entry_types import TaskResult
from trails.present import graph_lines, node_state


def task_result(store: EntryStore, event: str, outcome: TaskOutcome) -> TaskResult:
    """The new state, the closed entry's dependents and the redrawn graph. Read after a
    durable write, so a failed read becomes a warning."""
    entry_id = outcome.entry.id
    warnings = list(outcome.warnings)
    try:
        graph = store.task_graph()
        # After a supersede the old entry has no dependents left; the moved sources are its
        # dependents, now waiting on the replacements.
        moved = list(dict.fromkeys(item.source_id for item in outcome.moved))
        dependent_ids = list(dict.fromkeys([*graph.dependents(entry_id), *moved]))
        lines = graph_lines(graph, [entry_id, *outcome.follow_ups, *moved])
        wanted = set(dependent_ids)
        dependents = [line for line in graph_lines(graph, dependent_ids) if line.id in wanted]
        state = node_state(graph, entry_id)
    except Exception as exc:  # guidance only; the event is durable
        lines, dependents = [], []
        state = outcome.entry.state or ""
        warnings.append(f"the change landed, but redrawing the task graph failed: {exc}")
    return TaskResult(
        id=entry_id,
        event=event,
        changed=outcome.changed,
        state=state,
        follow_ups=outcome.follow_ups,
        moved=outcome.moved,
        dependents=dependents,
        graph=lines,
        warnings=warnings,
    )
