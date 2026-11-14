"""Derived task state over tasks, computed in Python from one read (D15).

Everything here is a function of stored links, task events, retirements and version fields.
Nothing reads what an entry says. Readiness is the one state trails works out for itself
(docs/philosophy.md, principle 3); everything else is reported as the stored facts it comes
from: how each child ended, and what each waits_on target's state is.

Definitions:

- A child of X is a not-retired task with a current `part_of` link to X.
- A task is ready when it is not closed and not retired, none of its children is not closed,
  and every current `waits_on` target that is a task, of it and of each ancestor, is closed
  `completed`. A retired target counts as not completed.
- A task is blocked when it is not closed and not retired and one of those `waits_on` targets
  is not closed `completed`. A parent that waits only on its own children is not blocked.
- A cycle is a cycle in the "must finish first" graph: a `waits_on` source needs its target, a
  parent needs its children, and a child needs every `waits_on` target of its ancestors (D8).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime

from trails.models import TaskEvent
from trails.vocab import WORK_KINDS, Kind, Outcome, TaskState


@dataclass(frozen=True)
class Node:
    """One entry the graph knows about. Non-work entries appear only as link targets."""

    id: str
    kind: Kind
    title: str
    version: int
    priority: int | None
    created_at: datetime
    state: TaskState | None
    outcome: Outcome | None
    claim: TaskEvent | None
    retired: bool
    projects: frozenset[str] = frozenset()


@dataclass
class TaskGraph:
    """Tasks, and the current `part_of` and `waits_on` links between entries."""

    nodes: dict[str, Node]
    parents: dict[str, list[str]] = field(default_factory=dict)  # child -> part_of targets
    waits: dict[str, list[str]] = field(default_factory=dict)  # source -> waits_on targets

    # ------------------------------------------------------------- structure

    def is_work(self, entry_id: str) -> bool:
        node = self.nodes.get(entry_id)
        return node is not None and node.kind in WORK_KINDS

    def children(self, entry_id: str) -> list[str]:
        """Not-retired tasks with a current part_of link to `entry_id`."""
        return sorted(
            (
                child
                for child, targets in self.parents.items()
                if entry_id in targets and self.is_work(child) and not self.nodes[child].retired
            ),
            key=self._order,
        )

    def descendants(self, entry_id: str) -> list[str]:
        """Children, transitively, each once."""
        found: list[str] = []
        frontier = [entry_id]
        seen = {entry_id}
        while frontier:
            current = frontier.pop(0)
            for child in self.children(current):
                if child not in seen:
                    seen.add(child)
                    found.append(child)
                    frontier.append(child)
        return found

    def ancestors(self, entry_id: str) -> list[str]:
        """Part_of targets, transitively, nearest first, each once."""
        found: list[str] = []
        frontier = list(self.parents.get(entry_id, []))
        seen = {entry_id}
        while frontier:
            current = frontier.pop(0)
            if current in seen:
                continue
            seen.add(current)
            found.append(current)
            frontier.extend(self.parents.get(current, []))
        return found

    def dependents(self, entry_id: str) -> list[str]:
        """Entries with a current waits_on link to `entry_id`."""
        return sorted(
            (source for source, targets in self.waits.items() if entry_id in targets),
            key=self._order,
        )

    # ------------------------------------------------------------- state

    def unfinished_targets(self, entry_id: str) -> list[str]:
        """The task targets of `entry_id`'s waits_on links, and its ancestors', that are not
        closed `completed`."""
        return [target for target, _via in self.waiting_on(entry_id)]

    def open_children(self, entry_id: str) -> list[str]:
        return [child for child in self.children(entry_id) if self.nodes[child].state != "closed"]

    def is_ready(self, entry_id: str) -> bool:
        node = self.nodes.get(entry_id)
        if node is None or node.kind != "task" or node.retired or node.state == "closed":
            return False
        return not self.open_children(entry_id) and not self.unfinished_targets(entry_id)

    def ready(self, project: str | None = None) -> list[str]:
        """Ready tasks, by priority ascending with none last, then oldest first."""
        return sorted(
            (
                entry_id
                for entry_id, node in self.nodes.items()
                if self.is_ready(entry_id) and (project is None or project in node.projects)
            ),
            key=self._order,
        )

    def waiting_on(self, entry_id: str) -> list[tuple[str, str | None]]:
        """The task targets of `entry_id`'s waits_on links and its ancestors', not closed
        `completed`, each as (target, the ancestor whose link it is or None)."""
        found: list[tuple[str, str | None]] = []
        seen: set[str] = set()
        for holder in [entry_id, *self.ancestors(entry_id)]:
            for target in self.waits.get(holder, []):
                if target in seen or not self.is_work(target):
                    continue
                node = self.nodes[target]
                if node.retired or not (node.state == "closed" and node.outcome == "completed"):
                    seen.add(target)
                    found.append((target, None if holder == entry_id else holder))
        return found

    def is_blocked(self, entry_id: str) -> bool:
        node = self.nodes.get(entry_id)
        if node is None or node.kind != "task" or node.retired or node.state == "closed":
            return False
        return bool(self.unfinished_targets(entry_id))

    def blocked(self, project: str | None = None) -> list[str]:
        """Blocked tasks, in the ready queue's order."""
        return sorted(
            (
                entry_id
                for entry_id, node in self.nodes.items()
                if self.is_blocked(entry_id) and (project is None or project in node.projects)
            ),
            key=self._order,
        )

    def child_counts(self, entry_id: str) -> dict[str, int]:
        """How many children are in each state: open, claimed, or the outcome of a close."""
        counts: dict[str, int] = {}
        for child in self.children(entry_id):
            node = self.nodes[child]
            label = str(node.outcome) if node.state == "closed" else str(node.state)
            counts[label] = counts.get(label, 0) + 1
        return counts

    # ------------------------------------------------------------- cycles

    def needs(self, entry_id: str) -> list[str]:
        """What must finish before `entry_id` can: its waits_on targets, its children, and
        its ancestors' waits_on targets (D8)."""
        found = list(self.waits.get(entry_id, []))
        found += [child for child in self.children(entry_id) if child not in found]
        for ancestor in self.ancestors(entry_id):
            found += [target for target in self.waits.get(ancestor, []) if target not in found]
        return found

    def cycle_through(self, entry_id: str) -> list[str] | None:
        """A shortest cycle in the must-finish-first graph that passes through `entry_id`,
        as the ids along it starting and ending there, or None."""
        previous: dict[str, str] = {}
        frontier = [entry_id]
        seen = {entry_id}
        while frontier:
            current = frontier.pop(0)
            for nxt in self.needs(current):
                if nxt == entry_id:
                    path = [current]
                    while path[-1] != entry_id:
                        path.append(previous[path[-1]])
                    return [entry_id, *reversed(path[:-1]), entry_id]
                if nxt not in seen:
                    seen.add(nxt)
                    previous[nxt] = current
                    frontier.append(nxt)
        return None

    def cycles_among(self, entry_ids: Iterable[str]) -> list[list[str]]:
        """One cycle through each of `entry_ids` that is on one, without repeating a cycle."""
        found: list[list[str]] = []
        covered: set[str] = set()
        for entry_id in entry_ids:
            if entry_id in covered:
                continue
            cycle = self.cycle_through(entry_id)
            if cycle is not None:
                found.append(cycle)
                covered.update(cycle)
        return found

    # ------------------------------------------------------------- neighborhood

    def neighborhood(self, entry_ids: Sequence[str]) -> list[str]:
        """What a task-changing response redraws: each changed entry, its parent chain to the
        root, its children, and the entries at both ends of its waits_on links."""
        shown: list[str] = []

        def add(item: str) -> None:
            if item in self.nodes and item not in shown:
                shown.append(item)

        for entry_id in entry_ids:
            for ancestor in reversed(self.ancestors(entry_id)):
                add(ancestor)
            add(entry_id)
            for child in self.children(entry_id):
                add(child)
            for target in self.waits.get(entry_id, []):
                add(target)
            for source in self.dependents(entry_id):
                add(source)
        return shown

    def _order(self, entry_id: str) -> tuple[int, int, datetime, str]:
        node = self.nodes[entry_id]
        priority = node.priority
        return (
            priority is None,
            priority if priority is not None else 0,
            node.created_at,
            entry_id,
        )
