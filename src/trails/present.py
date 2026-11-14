"""Plain-text pieces every entry response shares: one-line states, graph lines, standing.

Every state word here is computed from stored links, events and version fields through
`TaskGraph`; nothing reads what an entry says.
"""

from __future__ import annotations

import shlex
from collections.abc import Sequence
from datetime import UTC, datetime

from pydantic import BaseModel

from trails.capture import human_size
from trails.models import CaptureSummary, Entry, EntryLink, TaskEvent
from trails.render import call
from trails.taskgraph import TaskGraph
from trails.vocab import WORK_KINDS


class GraphLine(BaseModel):
    """One entry in a redrawn task graph, with its state and its structural links."""

    id: str
    kind: str
    title: str
    state: str
    part_of: list[str]
    waits_on: list[str]


def claim_text(event: TaskEvent, now: datetime | None = None) -> str:
    """Who holds a claim and for how long."""
    moment = now or datetime.now(UTC)
    session = f" session {event.session_id}" if event.session_id else ""
    return f"claimed by {event.author} ({event.harness}{session}) {age(event.created_at, moment)}"


def age(then: datetime, now: datetime) -> str:
    seconds = max(0, int((now - then).total_seconds()))
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h ago"
    return f"{seconds // 86400}d ago"


def node_state(graph: TaskGraph, entry_id: str) -> str:
    """One phrase for where a goal or task stands; the kind for anything else."""
    node = graph.nodes.get(entry_id)
    if node is None:
        return "unknown"
    if node.retired:
        return "retired"
    if node.kind not in WORK_KINDS:
        return node.kind
    if node.state == "closed":
        return f"closed {node.outcome}"
    parts: list[str] = []
    if node.claim is not None:
        parts.append(claim_text(node.claim))
    failed = graph.failed_children(entry_id)
    if failed:
        parts.append(f"needs a decision: {', '.join(failed)} failed")
    elif graph.is_ready_to_close(entry_id):
        parts.append("ready to close: every child is closed")
    open_children = graph.open_children(entry_id)
    if open_children:
        parts.append(f"{len(open_children)} of {len(graph.children(entry_id))} children not closed")
    causes = graph.replanning_causes(entry_id)
    if causes:
        parts.append(
            "needs replanning: waits on "
            + ", ".join(f"{item} (closed {graph.nodes[item].outcome})" for item in causes)
        )
    waiting = [item for item in graph.unfinished_targets(entry_id) if item not in causes]
    if waiting:
        parts.append(f"waiting on {', '.join(waiting)}")
    if node.kind == "task" and graph.is_ready(entry_id):
        parts.insert(0, "ready")
    if not parts:
        parts.append("open")
    return "; ".join(parts)


def graph_lines(graph: TaskGraph, entry_ids: Sequence[str]) -> list[GraphLine]:
    """The neighborhood of `entry_ids` (TaskGraph.neighborhood), one line each."""
    return [
        GraphLine(
            id=item,
            kind=graph.nodes[item].kind,
            title=graph.nodes[item].title,
            state=node_state(graph, item),
            part_of=list(graph.parents.get(item, [])),
            waits_on=list(graph.waits.get(item, [])),
        )
        for item in graph.neighborhood(entry_ids)
    ]


def graph_text(lines: Sequence[GraphLine]) -> list[str]:
    if not lines:
        return []
    out = ["graph:"]
    for line in lines:
        links = []
        if line.part_of:
            links.append(f"part_of {', '.join(line.part_of)}")
        if line.waits_on:
            links.append(f"waits_on {', '.join(line.waits_on)}")
        suffix = f"  ({'; '.join(links)})" if links else ""
        out.append(f"  {line.id}  {line.kind}  [{line.state}]  {line.title}{suffix}")
    return out


def standing(entry: Entry, incoming: Sequence[EntryLink]) -> list[str]:
    """A claim's standing, every applicable word together: observation or assertion from the
    current version, supported and contradicted from current incoming links."""
    if entry.kind != "claim":
        return []
    current = entry.current
    method = bool(current.artifacts or current.code or current.value is not None)
    words = ["observation" if method else "assertion"]
    supporters = [link.source_id for link in incoming if link.relation == "supports"]
    contradicters = [link.source_id for link in incoming if link.relation == "contradicts"]
    if supporters:
        words.append(f"supported by {', '.join(supporters)}")
    if contradicters:
        words.append(f"contradicted by {', '.join(contradicters)}")
    return words


def evidence_users(entry: Entry, incoming: Sequence[EntryLink]) -> list[str]:
    """For a note, the entries that use it as evidence."""
    if entry.kind != "note":
        return []
    return [
        f"{link.source_id} {link.relation}"
        for link in incoming
        if link.relation in {"supports", "contradicts", "references"}
    ]


def stale_link_text(link: EntryLink, current_version: int | None) -> str:
    """`written against vN, now vM` when the target moved on since the link was written."""
    if link.target_version is None or current_version is None:
        return ""
    if link.target_version < current_version:
        return f" (written against v{link.target_version}, now v{current_version})"
    return ""


def show_call(entry_id: str) -> str:
    return call("show", entry_id)


# The preference section of prime is capped (dec-9b2ta: Claude Code's SessionStart hook limit).
PREFERENCE_CAP = 10_000
PREFERENCE_CAP_WARNING = (
    "warning: the preference section reached its 10,000-character cap; the preferences after "
    "it are listed by title only"
)


def preference_full_lines(entry_id: str, scope: str, title: str, body: str) -> list[str]:
    return [f"[{entry_id} · {scope}] {title}", *([body] if body else [])]


def preference_title_line(entry_id: str, scope: str, title: str) -> str:
    return f"[{entry_id} · {scope}] {title} · {call('show', entry_id)}"


def capture_lines(summaries: Sequence[CaptureSummary]) -> list[str]:
    """One line per captured repository, and for excluded paths the two commands that keep
    them out of later captures. trails runs neither command."""
    lines: list[str] = []
    for item in summaries:
        text = (
            f"captured {item.repo_path} @ {item.captured_sha[:12]}: {item.changed_files} "
            f"changed files, {human_size(item.changed_bytes)}"
        )
        if item.largest:
            shown = ", ".join(f"{path} {human_size(size)}" for path, size in item.largest)
            text += f"; largest: {shown}"
        lines.append(text)
        if item.excluded:
            quoted = " ".join(shlex.quote(f"/{path}") for path in item.excluded)
            lines += [
                f"excluded from this capture: {', '.join(item.excluded)}",
                "To keep them out of future captures and of git: printf '%s\\n' "
                f"{quoted} >> {shlex.quote(item.repo_path + '/.gitignore')}",
                "To keep them out locally without editing .gitignore: printf '%s\\n' "
                f"{quoted} >> {shlex.quote(item.git_common_dir + '/info/exclude')}",
            ]
    return lines
