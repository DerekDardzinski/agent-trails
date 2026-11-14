"""Plain-text pieces every entry response shares: one-line states and graph lines.

Every state word here is computed from stored links, events and version fields through
`TaskGraph`; nothing reads what an entry says.
"""

from __future__ import annotations

import shlex
from collections.abc import Sequence
from datetime import UTC, datetime

from pydantic import BaseModel

from trails.capture import human_size
from trails.models import CaptureSummary, EntryLink, TaskEvent
from trails.render import call
from trails.taskgraph import TaskGraph
from trails.vocab import EVIDENCE_RELATIONS, WORK_KINDS


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
    """Where a task stands, as stored facts; the kind for anything else. "ready" is the one
    state trails works out (principle 3); the rest is what the children and the waits_on
    targets recorded."""
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
    if graph.is_ready(entry_id):
        parts.append("ready")
    if node.claim is not None:
        parts.append(claim_text(node.claim))
    counts = graph.child_counts(entry_id)
    if counts:
        parts.append(
            "children: " + ", ".join(f"{count} {label}" for label, count in sorted(counts.items()))
        )
    for target, via in graph.waiting_on(entry_id):
        through = f" through {via}" if via is not None else ""
        parts.append(f"waits on {target}{through} ({target_state(graph, target)})")
    if not parts:
        parts.append("open")
    return "; ".join(parts)


def target_state(graph: TaskGraph, entry_id: str) -> str:
    """A waits_on target's stored state in a few words: open, claimed, closed <outcome>,
    retired."""
    node = graph.nodes[entry_id]
    if node.retired:
        return "retired"
    if node.state == "closed":
        return f"closed {node.outcome}"
    return str(node.state)


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


def evidence_links(incoming: Sequence[EntryLink]) -> list[str]:
    """The current supports and contradicts links into an entry, as plain facts."""
    words = {"supports": "supported by", "contradicts": "contradicted by"}
    return [
        f"{words[link.relation]} {link.source_id}"
        for link in incoming
        if link.relation in EVIDENCE_RELATIONS
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
