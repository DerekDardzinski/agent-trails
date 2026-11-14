"""The `prime` operation.

NOTE: no `from __future__ import annotations` in this package. See `ops/__init__.py`.
"""

from typing import Annotated, NamedTuple

from pydantic import Field

from trails.entry_search import NO_MEMBERSHIP_CONDITION, membership_condition
from trails.entry_store import EntryStore
from trails.errors import StoreUnavailableError
from trails.models import Entry
from trails.ops import context
from trails.ops._entry_types import (
    PrimeParent,
    PrimePreference,
    PrimeProjectRow,
    PrimeResult,
    ShownProject,
    preference_lines,
)
from trails.ops._marks import Operation
from trails.present import PREFERENCE_CAP, node_state
from trails.taskgraph import TaskGraph
from trails.vocab import KINDS, WORK_KINDS

PROJECT_PAGE = 100
# Prime lists at most this many open top-level parent tasks and contradicted entries, with the
# totals and the search call for the rest, so its size does not grow with history.
LIST_LIMIT = 20

OPERATION = Operation(toolset="agent", read_only=True, destructive=False, idempotent=True)

_NO_MEMBERSHIP = NO_MEMBERSHIP_CONDITION
_NOT_RETIRED = "NOT EXISTS (SELECT 1 FROM retirement r WHERE r.entry_id = e.id)"


async def prime(
    project: Annotated[
        str | None,
        Field(description="A project id or exact name; omit for the index of every project."),
    ] = None,
) -> PrimeResult:
    """Start or resume a session: the person's preferences in full, then where work stands.

    Use this when: a session starts, after a handoff or compaction, or when you switch projects.

    Use instead: ready, for the next task; search, to find entries; show, to read one.

    Returns: preferences in full (the project's first, then global, newest first) under a 10,000-character cap, with conflicts between them; without a project, the project index and the count of unscoped entries; with one, its framing and paths, its open top-level tasks that have children (up to 20) with their state, the count of ready tasks and of entries with open comments with the calls that list them, contradicted entries, and counts per kind and state. A store that cannot be opened is reported, not raised.
    """
    try:
        store = context.store()
        ref = store.resolve_project(project) if project is not None else None
    except StoreUnavailableError as exc:
        return _unavailable(str(exc))
    found_preferences = _preferences(store, ref.id if ref else None)
    graph = store.task_graph()
    if ref is None:
        listed = store.list_projects()
        top_parents = _parents(graph)
        rows = [
            PrimeProjectRow(
                id=item.id,
                name=item.name,
                framing_title=item.current_framing.title if item.current_framing else None,
                open_parents=sum(
                    1 for node_id in top_parents if item.id in graph.nodes[node_id].projects
                ),
            )
            for item in listed[:PROJECT_PAGE]
        ]
        unscoped = store.query(
            f"SELECT COUNT(*) AS n FROM entry e WHERE {_NOT_RETIRED} AND {_NO_MEMBERSHIP}"
        )
        return PrimeResult(
            project=None,
            projects=rows,
            project_total=len(listed),
            unscoped=int(unscoped[0]["n"]),
            preferences=found_preferences.preferences,
            preference_cap_hit=found_preferences.cap_hit,
            preference_conflicts=found_preferences.conflicts,
            preferences_omitted=found_preferences.omitted,
            preference_conflicts_omitted=found_preferences.conflicts_omitted,
            parents=[],
            parent_total=0,
            ready=0,
            open_comments=0,
            contradicted=[],
            contradicted_total=0,
            counts={},
            unavailable=None,
        )
    found = store.get_project(ref.id)
    framing = found.current_framing if found else None
    shown = ShownProject(
        id=ref.id,
        name=ref.name,
        paths=found.paths if found else [],
        framing_title=framing.title if framing else None,
        framing_body=framing.body if framing else None,
        framing_version=framing.version if framing else None,
    )
    member = membership_condition([ref.id])
    kind_rows, open_rows, contradicted_rows = (
        store.query(
            f"SELECT e.id, e.kind FROM entry e WHERE {_NOT_RETIRED} AND {member} ORDER BY e.id"
        ),
        store.query(
            f"SELECT COUNT(*) AS n FROM entry e WHERE {_NOT_RETIRED} AND {member} AND EXISTS "
            "(SELECT 1 FROM comment c WHERE c.entry_id = e.id AND NOT EXISTS (SELECT 1 FROM "
            "comment_addresses a WHERE a.addressed_id = c.id) AND NOT EXISTS (SELECT 1 FROM "
            "comment_addresses r WHERE r.comment_id = c.id))"
        ),
        store.query(
            f"SELECT DISTINCT e.id FROM entry e WHERE {_NOT_RETIRED} AND "
            f"{member} AND EXISTS (SELECT 1 FROM link_event l WHERE l.target_id = e.id AND "
            "l.relation = 'contradicts')"
        ),
    )
    counts: dict[str, dict[str, int]] = {}
    for row in kind_rows:
        kind = str(row["kind"])
        entry_id = str(row["id"])
        if kind in WORK_KINDS and entry_id in graph.nodes:
            state = graph.nodes[entry_id].state or "open"
        else:
            state = "current"
        counts.setdefault(kind, {}).setdefault(state, 0)
        counts[kind][state] += 1
    ordered: dict[str, dict[str, int]] = {k: counts[k] for k in KINDS if k in counts}
    contradicted_ids = [str(row["id"]) for row in contradicted_rows]
    incoming = store.incoming_links(contradicted_ids, ["contradicts"])
    found_contradicted = store.get_entries(contradicted_ids)
    contradicted = [
        f"{entry_id}  {found_contradicted[entry_id].current.title}  (contradicted by "
        + ", ".join(link.source_id for link in incoming[entry_id])
        + ")"
        for entry_id in contradicted_ids
        if entry_id in found_contradicted and incoming.get(entry_id)
    ]
    parents = [
        PrimeParent(id=node_id, title=graph.nodes[node_id].title, state=node_state(graph, node_id))
        for node_id in _parents(graph)
        if ref.id in graph.nodes[node_id].projects
    ]
    return PrimeResult(
        project=shown,
        projects=[],
        project_total=0,
        unscoped=0,
        preferences=found_preferences.preferences,
        preference_cap_hit=found_preferences.cap_hit,
        preference_conflicts=found_preferences.conflicts,
        preferences_omitted=found_preferences.omitted,
        preference_conflicts_omitted=found_preferences.conflicts_omitted,
        parents=parents[:LIST_LIMIT],
        parent_total=len(parents),
        ready=len(graph.ready(ref.id)),
        open_comments=int(open_rows[0]["n"]),
        contradicted=contradicted[:LIST_LIMIT],
        contradicted_total=len(contradicted),
        counts=ordered,
        unavailable=None,
    )


def _parents(graph: TaskGraph) -> list[str]:
    """Open top-level tasks that have children, oldest first: the initiatives of the work."""
    return [
        node.id
        for node in sorted(graph.nodes.values(), key=lambda item: (item.created_at, item.id))
        if node.kind == "task"
        and node.state != "closed"
        and not node.retired
        and not graph.parents.get(node.id)
        and graph.children(node.id)
    ]


class PreferenceSection(NamedTuple):
    preferences: list[PrimePreference]
    cap_hit: bool
    conflicts: list[str]
    omitted: int
    conflicts_omitted: int


def _preferences(store: EntryStore, project_id: str | None) -> PreferenceSection:
    """Project preferences first, then global ones, each newest version first, in full until
    the next would push the section past the cap; the rest as title lines (D17, D24)."""
    rows = store.query(
        f"SELECT e.id FROM entry e WHERE e.kind = 'preference' AND {_NOT_RETIRED} AND "
        f"{_NO_MEMBERSHIP}"
    )
    global_ids = [str(row["id"]) for row in rows]
    project_ids: list[str] = []
    if project_id is not None:
        project_rows = store.query(
            f"SELECT e.id FROM entry e WHERE e.kind = 'preference' AND {_NOT_RETIRED} AND "
            f"{membership_condition([project_id])}"
        )
        project_ids = [str(row["id"]) for row in project_rows]
    found = store.get_entries([*project_ids, *global_ids])

    def newest(ids: list[str]) -> list[Entry]:
        return sorted(
            (found[item] for item in ids if item in found),
            key=lambda entry: (entry.current.created_at, entry.id),
            reverse=True,
        )

    ordered = [(entry, "project") for entry in newest(project_ids)] + [
        (entry, "global") for entry in newest(global_ids)
    ]
    incoming = store.incoming_links([entry.id for entry, _ in ordered], ["contradicts"])
    current = {entry.id for entry, _ in ordered}
    conflicts = [
        f"{link.source_id} contradicts {entry.id}"
        for entry, _scope in ordered
        for link in incoming.get(entry.id, [])
        if link.source_id in current
    ]

    def section(full: int, titles: int, kept_conflicts: int) -> PreferenceSection:
        shown = [
            PrimePreference(
                id=entry.id,
                title=entry.current.title,
                body=entry.current.body if position < full else "",
                scope=scope,
                full=position < full,
            )
            for position, (entry, scope) in enumerate(ordered[: full + titles])
        ]
        return PreferenceSection(
            preferences=shown,
            cap_hit=full < len(ordered) or kept_conflicts < len(conflicts),
            conflicts=conflicts[:kept_conflicts],
            omitted=len(ordered) - full - titles,
            conflicts_omitted=len(conflicts) - kept_conflicts,
        )

    def fits(candidate: PreferenceSection) -> bool:
        lines = preference_lines(
            candidate.preferences,
            cap_hit=candidate.cap_hit,
            conflicts=candidate.conflicts,
            omitted=candidate.omitted,
            conflicts_omitted=candidate.conflicts_omitted,
            project=project_id,
        )
        return len("\n".join(lines)) <= PREFERENCE_CAP

    # Most full texts first, the rest as title lines. When even every title line passes the
    # cap, keep as many title lines as fit, then as many conflicts as fit; the rest are counted
    # with the search call that lists them, so the section never passes the cap (D24).
    total = len(ordered)
    for full in range(total, -1, -1):
        candidate = section(full, total - full, len(conflicts))
        if fits(candidate):
            return candidate
    for titles in range(total, -1, -1):
        candidate = section(0, titles, len(conflicts))
        if fits(candidate):
            return candidate
    for kept in range(len(conflicts), -1, -1):
        candidate = section(0, 0, kept)
        if fits(candidate):
            return candidate
    return section(0, 0, 0)  # pragma: no cover - the two count lines alone fit the cap


def _unavailable(detail: str) -> PrimeResult:
    return PrimeResult(
        project=None,
        projects=[],
        project_total=0,
        unscoped=0,
        preferences=[],
        preference_cap_hit=False,
        preference_conflicts=[],
        parents=[],
        parent_total=0,
        ready=0,
        open_comments=0,
        contradicted=[],
        contradicted_total=0,
        counts={},
        unavailable=detail,
    )
