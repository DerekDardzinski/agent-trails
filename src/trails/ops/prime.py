"""The `prime` operation.

NOTE: no `from __future__ import annotations` in this package. See `ops/__init__.py`.
"""

from typing import Annotated

from pydantic import Field

from trails.entry_search import membership_condition
from trails.entry_store import EntryStore
from trails.errors import StoreUnavailableError
from trails.models import Entry
from trails.ops import context
from trails.ops._entry_types import (
    PrimeGoal,
    PrimePreference,
    PrimeProjectRow,
    PrimeResult,
    ShownProject,
)
from trails.ops._marks import Operation
from trails.present import (
    PREFERENCE_CAP,
    PREFERENCE_CAP_WARNING,
    node_state,
    preference_full_lines,
    preference_title_line,
)
from trails.vocab import KINDS, WORK_KINDS

PROJECT_PAGE = 100

OPERATION = Operation(toolset="agent", read_only=True, destructive=False, idempotent=True)

_NO_MEMBERSHIP = (
    "NOT EXISTS (SELECT 1 FROM entry_project_event m WHERE m.entry_id = e.id AND m.action = "
    "'add' AND NOT EXISTS (SELECT 1 FROM entry_project_event later WHERE later.entry_id = "
    "m.entry_id AND later.project_id = m.project_id AND later.sequence > m.sequence))"
)
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

    Returns: preferences in full (the project's first, then global, newest first) under a 10,000-character cap, with conflicts between them; without a project, the project index and the count of unscoped entries; with one, its framing and paths, open goals with their state, the count of ready tasks and of entries with open comments with the calls that list them, contradicted claims, and counts per kind and state. A store that cannot be opened is reported, not raised.
    """
    try:
        store = context.store()
        ref = store.resolve_project(project) if project is not None else None
    except StoreUnavailableError as exc:
        return _unavailable(str(exc))
    preferences, cap_hit, conflicts = _preferences(store, ref.id if ref else None)
    graph = store.task_graph()
    if ref is None:
        listed = store.list_projects()
        rows = [
            PrimeProjectRow(
                id=item.id,
                name=item.name,
                framing_title=item.current_framing.title if item.current_framing else None,
                open_goals=sum(
                    1
                    for node in graph.nodes.values()
                    if node.kind == "goal"
                    and node.state != "closed"
                    and not node.retired
                    and item.id in node.projects
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
            preferences=preferences,
            preference_cap_hit=cap_hit,
            preference_conflicts=conflicts,
            goals=[],
            ready=0,
            open_comments=0,
            contradicted=[],
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
            f"SELECT DISTINCT e.id FROM entry e WHERE e.kind = 'claim' AND {_NOT_RETIRED} AND "
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
    claim_ids = [str(row["id"]) for row in contradicted_rows]
    incoming = store.incoming_links(claim_ids, ["contradicts"])
    claims = store.get_entries(claim_ids)
    contradicted = [
        f"{entry_id}  {claims[entry_id].current.title}  (contradicted by "
        + ", ".join(link.source_id for link in incoming[entry_id])
        + ")"
        for entry_id in claim_ids
        if entry_id in claims and incoming.get(entry_id)
    ]
    goals = [
        PrimeGoal(id=node.id, title=node.title, state=node_state(graph, node.id))
        for node in sorted(graph.nodes.values(), key=lambda item: (item.created_at, item.id))
        if node.kind == "goal"
        and node.state != "closed"
        and not node.retired
        and ref.id in node.projects
    ]
    return PrimeResult(
        project=shown,
        projects=[],
        project_total=0,
        unscoped=0,
        preferences=preferences,
        preference_cap_hit=cap_hit,
        preference_conflicts=conflicts,
        goals=goals,
        ready=len(graph.ready(ref.id)),
        open_comments=int(open_rows[0]["n"]),
        contradicted=contradicted,
        counts=ordered,
        unavailable=None,
    )


def _preferences(
    store: EntryStore, project_id: str | None
) -> tuple[list[PrimePreference], bool, list[str]]:
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

    def size(lines: list[str]) -> int:
        return len("\n".join(lines))

    titles = [preference_title_line(e.id, scope, e.current.title) for e, scope in ordered]
    conflict_lines = [f"conflict: {item}" for item in conflicts]
    full_count = 0
    for count in range(len(ordered), -1, -1):
        lines = ["preferences:"]
        for entry, scope in ordered[:count]:
            lines += preference_full_lines(entry.id, scope, entry.current.title, entry.current.body)
        lines += titles[count:]
        if count < len(ordered):
            lines.append(PREFERENCE_CAP_WARNING)
        lines += conflict_lines
        if size(lines) <= PREFERENCE_CAP:
            full_count = count
            break
    preferences = [
        PrimePreference(
            id=entry.id,
            title=entry.current.title,
            body=entry.current.body if position < full_count else "",
            scope=scope,
            full=position < full_count,
        )
        for position, (entry, scope) in enumerate(ordered)
    ]
    return preferences, full_count < len(ordered), conflicts


def _unavailable(detail: str) -> PrimeResult:
    return PrimeResult(
        project=None,
        projects=[],
        project_total=0,
        unscoped=0,
        preferences=[],
        preference_cap_hit=False,
        preference_conflicts=[],
        goals=[],
        ready=0,
        open_comments=0,
        contradicted=[],
        counts={},
        unavailable=detail,
    )
