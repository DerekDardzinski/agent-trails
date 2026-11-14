"""Result models of the entry operations, each rendering its own text.

NOTE: no `from __future__ import annotations` in this package. See `ops/__init__.py`.
"""

from datetime import datetime
from typing import Any

from pydantic import BaseModel

from trails.models import CaptureSummary, MovedLink, ProjectPath, ProjectRef
from trails.ops.types import RenderedResult
from trails.present import (
    PREFERENCE_CAP_WARNING,
    GraphLine,
    capture_lines,
    graph_text,
    preference_full_lines,
    preference_title_line,
)
from trails.render import call, next_line


def _warnings(warnings: list[str]) -> list[str]:
    return [f"warning: {item}" for item in warnings]


class WriteResult(RenderedResult):
    """A created entry or a new version."""

    id: str
    kind: str
    version: int
    created: bool
    retry: bool
    projects: list[ProjectRef]
    embedded: bool
    captures: list[CaptureSummary]
    graph: list[GraphLine]
    warnings: list[str]

    def render_text(self) -> str:
        if self.created:
            head = f"created {self.id} · {self.kind} · version 1"
        elif self.retry:
            head = f"{self.id} · version {self.version} had already landed; nothing written"
        else:
            head = f"{self.id} · {self.kind} · version {self.version}"
        lines = [head]
        scope = ", ".join(f"{p.id} ({p.name})" for p in self.projects) or "unscoped"
        lines.append(f"projects: {scope}")
        lines += capture_lines(self.captures)
        lines += graph_text(self.graph)
        lines += _warnings(self.warnings)
        lines.append(next_line([call("show", self.id)]))
        return "\n".join(lines)


class LinkResult(RenderedResult):
    """A link added or removed, and the redrawn graph when it changed task state."""

    source_id: str
    relation: str
    target_id: str
    action: str
    changed: bool
    target_version: int | None
    graph: list[GraphLine]
    warnings: list[str]

    def render_text(self) -> str:
        verb = "linked" if self.action == "add" else "unlinked"
        against = f" (against v{self.target_version})" if self.target_version else ""
        head = f"{verb} {self.source_id} {self.relation} {self.target_id}{against}"
        if not self.changed:
            head += " · already so; nothing changed"
        lines = [head, *graph_text(self.graph), *_warnings(self.warnings)]
        lines.append(next_line([call("show", self.source_id)]))
        return "\n".join(lines)


class TaskResult(RenderedResult):
    """A claim, release, close or reopen, with dependents and the redrawn graph."""

    id: str
    event: str
    changed: bool
    state: str
    follow_ups: list[str]
    moved: list[MovedLink]
    dependents: list[GraphLine]
    graph: list[GraphLine]
    warnings: list[str]

    def render_text(self) -> str:
        head = f"{self.event} {self.id} · now [{self.state}]"
        if not self.changed:
            head = f"{self.id} · [{self.state}] · nothing changed"
        lines = [head]
        lines += [f"follow-up created: {item}" for item in self.follow_ups]
        lines += [
            f"moved: {item.source_id} waits_on {item.from_id} -> {item.to_id}"
            for item in self.moved
        ]
        if self.dependents:
            lines.append("dependents:")
            lines += [f"  {item.id}  [{item.state}]  {item.title}" for item in self.dependents]
        lines += graph_text(self.graph)
        lines += _warnings(self.warnings)
        hints = [call("show", self.id)]
        if self.event == "close":
            hints.append(call("ready"))
        lines.append(next_line(hints))
        return "\n".join(lines)


class ReadyItem(BaseModel):
    id: str
    version: int
    title: str
    priority: int | None
    state: str
    claimed_by: str | None
    claimed_at: datetime | None


class ReadyResult(RenderedResult):
    """Ready tasks, and the entries waiting on a decision."""

    project: ProjectRef | None
    ready: list[ReadyItem]
    total: int
    ready_to_close: list[GraphLine]
    needs_decision: list[GraphLine]
    needs_replanning: list[GraphLine]

    def render_text(self) -> str:
        scope = f" in {self.project.id} ({self.project.name})" if self.project else ""
        lines = [f"ready{scope}: {len(self.ready)} of {self.total} shown"]
        for item in self.ready:
            priority = f"P{item.priority}" if item.priority is not None else "P-"
            lines.append(f"  {item.id}  {priority}  [{item.state}]  {item.title}")
        for label, group in (
            ("ready to close", self.ready_to_close),
            ("needs a decision", self.needs_decision),
            ("needs replanning", self.needs_replanning),
        ):
            if group:
                lines.append(f"{label}:")
                lines += [f"  {item.id}  [{item.state}]  {item.title}" for item in group]
        hints = (
            [call("claim", self.ready[0].id, base_version=self.ready[0].version)]
            if self.ready
            else [call("search", kinds=["task"], status="open")]
        )
        lines.append(next_line(hints))
        return "\n".join(lines)


class CommentResult(RenderedResult):
    """A written comment."""

    comment_id: str
    entry_id: str
    entry_version: int
    verdict: str | None
    addresses: list[str]
    pinned: int
    warnings: list[str]

    def render_text(self) -> str:
        lines = [f"comment {self.comment_id} on {self.entry_id} v{self.entry_version}"]
        lines += _warnings(self.warnings)
        if self.verdict:
            lines.append(f"verdict: {self.verdict}")
        if self.addresses:
            lines.append("addresses (now closed): " + ", ".join(self.addresses))
        if self.pinned:
            lines.append(f"pinned the current version of {self.pinned} entries under it")
        lines.append(
            next_line(
                [
                    call("show", self.entry_id),
                    call("show", self.entry_id, since_comment=self.comment_id),
                ]
            )
        )
        return "\n".join(lines)


class ShownLink(BaseModel):
    direction: str  # "out" or "in"
    relation: str
    other_id: str
    note: str


class ShownComment(BaseModel):
    id: str
    entry_version: int
    author: str
    harness: str
    session_id: str | None
    created_at: datetime
    body: str
    verdict: str | None
    addresses: list[str]
    open: bool
    unchanged: bool


class ShownVersion(BaseModel):
    version: int
    title: str
    reason: str | None
    author: str
    harness: str
    session_id: str | None
    created_at: datetime


class ShownEntry(BaseModel):
    """One entry as `show` presents it."""

    id: str
    kind: str
    version: int
    title: str
    body: str
    tags: list[str]
    value: dict[str, Any] | None
    priority: int | None
    stated_by: str | None
    artifacts: list[str]
    code: list[str]
    projects: list[ProjectRef]
    state: str
    standing: list[str]
    evidence_for: list[str]
    links: list[ShownLink]
    task_events: list[str]
    retired_reason: str | None
    author: str
    harness: str
    session_id: str | None
    transcript: str | None
    agent: str | None
    created_at: datetime
    comments: list[ShownComment]
    comment_total: int
    comment_open: int
    versions: list[ShownVersion]
    since: list[str]


class ShownProject(BaseModel):
    id: str
    name: str
    paths: list[ProjectPath]
    framing_title: str | None
    framing_body: str | None
    framing_version: int | None


class ShowResult(RenderedResult):
    """Entries and projects by id, in request order."""

    entries: list[ShownEntry]
    projects: list[ShownProject]
    order: list[str]
    missing: list[str]

    def render_text(self) -> str:
        blocks: list[str] = []
        by_id: dict[str, ShownEntry | ShownProject] = {
            **{item.id: item for item in self.entries},
            **{item.id: item for item in self.projects},
        }
        for item_id in self.order:
            item = by_id.get(item_id)
            if isinstance(item, ShownEntry):
                blocks.append(_entry_text(item))
            elif isinstance(item, ShownProject):
                blocks.append(_project_text(item))
        if self.missing:
            blocks.append("not found: " + ", ".join(self.missing))
        hints = _show_hints([by_id[item] for item in self.order if item in by_id])
        if hints:
            blocks.append(next_line(hints))
        return "\n\n".join(blocks)


def _entry_text(item: ShownEntry) -> str:  # noqa: PLR0912, PLR0915 — one section per field
    lines = [f"{item.id}  {item.kind}  v{item.version}  [{item.state}]  {item.title}"]
    if item.retired_reason is not None:
        lines.append(f"retired: {item.retired_reason}")
    if item.standing:
        lines.append("standing: " + "; ".join(item.standing))
    if item.stated_by:
        lines.append(f"stated by: {item.stated_by}")
    if item.priority is not None:
        lines.append(f"priority: {item.priority}")
    if item.tags:
        lines.append("tags: " + ", ".join(item.tags))
    if item.projects:
        lines.append("projects: " + ", ".join(f"{p.id} ({p.name})" for p in item.projects))
    session = f", session {item.session_id}" if item.session_id else ""
    agent = f", agent {item.agent}" if item.agent else ""
    lines.append(
        f"written by {item.author} ({item.harness}{session}{agent}) at {item.created_at.isoformat()}"
    )
    if item.transcript:
        lines.append(f"transcript: {item.transcript}")
    if item.body:
        lines.append(item.body)
    if item.value is not None:
        lines.append(f"value: {item.value}")
    lines += [f"artifact: {artifact}" for artifact in item.artifacts]
    lines += [f"code: {code}" for code in item.code]
    if item.evidence_for:
        lines.append("used as evidence by: " + ", ".join(item.evidence_for))
    if item.links:
        lines.append("links:")
        for link in item.links:
            arrow = "->" if link.direction == "out" else "<-"
            lines.append(f"  {arrow} {link.relation} {link.other_id}{link.note}")
    if item.task_events:
        lines.append("task events:")
        lines += [f"  {event}" for event in item.task_events]
    if item.versions:
        lines.append("versions:")
        for version in item.versions:
            reason = f" · {version.reason}" if version.reason else ""
            lines.append(
                f"  v{version.version} {version.created_at.isoformat()} {version.author} "
                f"({version.harness}) {version.title}{reason}"
            )
    if item.since:
        lines.append("since the comment:")
        lines += [f"  {line}" for line in item.since]
    if item.comment_total:
        lines.append(f"comments: {item.comment_total} total, {item.comment_open} open")
        for comment in item.comments:
            flags = []
            if comment.open:
                flags.append("open")
            if comment.verdict:
                flags.append(comment.verdict)
            if comment.addresses:
                flags.append("addresses " + ", ".join(comment.addresses))
            if comment.unchanged:
                flags.append("addressed with no new version")
            marker = f" [{'; '.join(flags)}]" if flags else ""
            lines.append(
                f"  {comment.id} on v{comment.entry_version} by {comment.author} "
                f"({comment.harness}){marker}: {comment.body}"
            )
        if item.comment_total > len(item.comments):
            lines.append("  more: " + call("show", item.id, comments=item.comment_total))
    return "\n".join(lines)


def _project_text(item: ShownProject) -> str:
    lines = [f"{item.id}  project  {item.name}"]
    if item.framing_title:
        lines.append(f"framing v{item.framing_version}: {item.framing_title}")
        if item.framing_body:
            lines.append(item.framing_body)
    for path in item.paths:
        label = f" ({path.label})" if path.label else ""
        lines.append(f"path: {path.path}{label}")
    return "\n".join(lines)


def _show_hints(items: list["ShownEntry | ShownProject"]) -> list[str]:
    """One Next: line for the whole response, from the first item shown."""
    if not items:
        return []
    first = items[0]
    if isinstance(first, ShownProject):
        return [call("prime", project=first.id)]
    hints = [
        call("write", id=first.id, base_version=first.version, reason="..."),
        call("add_comment", first.id, comment="..."),
    ]
    if first.kind in {"goal", "task"}:
        hints.insert(0, call("ready"))
    return hints


class SearchHit(BaseModel):
    id: str
    kind: str
    version: int
    title: str
    state: str
    tags: list[str]
    projects: list[str]
    standing: list[str]
    evidence_for: list[str]
    open_comments: int
    matched: list[str]
    excerpt: str


class ProjectHit(BaseModel):
    id: str
    name: str
    framing_title: str | None


class SearchResult(RenderedResult):
    """Projects matching an unfiltered query, then one page of entry hits."""

    query: str
    projects: list[ProjectHit]
    project_total: int
    hits: list[SearchHit]
    total: int
    offset: int

    def render_text(self) -> str:
        lines = [f"search: {len(self.hits)} of {self.total} entries shown (offset {self.offset})"]
        if self.projects:
            lines.append(f"projects ({self.project_total} matched):")
            lines += [
                f"  {item.id}  {item.name}"
                + (f"  {item.framing_title}" if item.framing_title else "")
                for item in self.projects
            ]
        for hit in self.hits:
            lines.append(f"{hit.id}  {hit.kind}  v{hit.version}  [{hit.state}]  {hit.title}")
            details = []
            if hit.standing:
                details.append("standing: " + "; ".join(hit.standing))
            if hit.evidence_for:
                details.append("used as evidence by: " + ", ".join(hit.evidence_for))
            if hit.open_comments:
                details.append(f"{hit.open_comments} open comments")
            if hit.tags:
                details.append("tags: " + ", ".join(hit.tags))
            if hit.matched:
                details.append("matched: " + ", ".join(hit.matched))
            lines += [f"  {detail}" for detail in details]
            if hit.excerpt:
                lines.append(f"  {hit.excerpt}")
        hints = [call("show", [hit.id for hit in self.hits[:3]])] if self.hits else []
        if self.total > self.offset + len(self.hits):
            hints.append(call("search", self.query, offset=self.offset + len(self.hits)))
        if not hints:
            hints.append(call("write", kind="note", title="..."))
        lines.append(next_line(hints))
        return "\n".join(lines)


class PrimePreference(BaseModel):
    id: str
    title: str
    body: str
    scope: str
    full: bool


class PrimeGoal(BaseModel):
    id: str
    title: str
    state: str


class PrimeProjectRow(BaseModel):
    id: str
    name: str
    framing_title: str | None
    open_goals: int


class PrimeResult(RenderedResult):
    """The session-start page: preferences in full under a cap, then orientation."""

    project: ShownProject | None
    projects: list[PrimeProjectRow]
    project_total: int
    unscoped: int
    preferences: list[PrimePreference]
    preference_cap_hit: bool
    preference_conflicts: list[str]
    goals: list[PrimeGoal]
    goal_total: int
    ready: int
    open_comments: int
    contradicted: list[str]
    contradicted_total: int
    counts: dict[str, dict[str, int]]
    unavailable: str | None

    def render_text(self) -> str:
        if self.unavailable is not None:
            return f"trails store unavailable: {self.unavailable}"
        lines: list[str] = []
        lines += preference_section(self)
        if self.project is None:
            lines.append(f"projects: {len(self.projects)} of {self.project_total} shown")
            lines += [
                f"  {row.id}  {row.name}  {row.open_goals} open goals"
                + (f"  {row.framing_title}" if row.framing_title else "")
                for row in self.projects
            ]
            lines.append(f"unscoped entries: {self.unscoped}")
            hints = [call("prime", project=self.projects[0].id)] if self.projects else []
            hints.append(call("ready"))
            lines.append(next_line(hints))
            return "\n".join(lines)
        project = self.project
        lines.append(f"project {project.id} ({project.name})")
        if project.framing_title:
            lines.append(f"framing v{project.framing_version}: {project.framing_title}")
            if project.framing_body:
                lines.append(project.framing_body)
        for path in project.paths:
            label = f" ({path.label})" if path.label else ""
            lines.append(f"path: {path.path}{label}")
        if self.goals:
            lines.append(f"open goals: {len(self.goals)} of {self.goal_total} shown")
            lines += [f"  {goal.id}  [{goal.state}]  {goal.title}" for goal in self.goals]
            if self.goal_total > len(self.goals):
                lines.append(
                    "  more: "
                    + call("search", kinds=["goal"], projects=[project.id], status="open")
                )
        lines.append(f"ready tasks: {self.ready} · {call('ready', project=project.id)}")
        if self.open_comments:
            lines.append(
                f"open comments: {self.open_comments} · "
                + call("search", projects=[project.id], open_comments=True)
            )
        if self.contradicted:
            lines.append(
                f"contradicted claims: {len(self.contradicted)} of {self.contradicted_total} shown"
            )
            lines += [f"  {item}" for item in self.contradicted]
            if self.contradicted_total > len(self.contradicted):
                lines.append("  more: " + call("search", kinds=["claim"], projects=[project.id]))
        if self.counts:
            lines.append("counts:")
            for kind, states in self.counts.items():
                parts = ", ".join(f"{state} {count}" for state, count in states.items())
                lines.append(
                    f"  {kind}: {parts} · {call('search', kinds=[kind], projects=[project.id])}"
                )
        lines.append(
            next_line(
                [call("ready", project=project.id), call("search", "...", projects=[project.id])]
            )
        )
        return "\n".join(lines)


class RetireResult(RenderedResult):
    entry_id: str
    reason: str
    already_retired: bool
    warnings: list[str]

    def render_text(self) -> str:
        head = (
            f"{self.entry_id} was already retired: {self.reason}"
            if self.already_retired
            else f"retired {self.entry_id}: {self.reason}"
        )
        return "\n".join(
            [head, *_warnings(self.warnings), next_line([call("show", self.entry_id)])]
        )


def preference_section(result: PrimeResult) -> list[str]:
    """The preference section's lines, exactly as counted against the cap (D24)."""
    if not (result.preferences or result.preference_conflicts):
        return []
    lines = ["preferences:"]
    for item in result.preferences:
        if item.full:
            lines += preference_full_lines(item.id, item.scope, item.title, item.body)
        else:
            lines.append(preference_title_line(item.id, item.scope, item.title))
    if result.preference_cap_hit:
        lines.append(PREFERENCE_CAP_WARNING)
    lines += [f"conflict: {item}" for item in result.preference_conflicts]
    return lines
