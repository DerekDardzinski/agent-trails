"""Helpers the entry operations share: graph redraws, embedding after a write, code capture,
and the presentation of one entry.

NOTE: no `from __future__ import annotations` in this package. See `ops/__init__.py`.
"""

from collections.abc import Sequence
from pathlib import Path

from trails import capture, engine as engine_seam, entry_search, gitprobe
from trails.entry_store import EntryStore, is_open_comment, unchanged_responses
from trails.errors import ValidationError
from trails.models import CaptureSummary, CodeCapture, Entry
from trails.ops._entry_types import ShownComment, ShownEntry, ShownLink, ShownVersion
from trails.present import (
    GraphLine,
    evidence_users,
    graph_lines,
    node_state,
    stale_link_text,
    standing,
)


def redraw(store: EntryStore, entry_ids: Sequence[str]) -> tuple[list[GraphLine], list[str]]:
    """The neighborhood graph of `entry_ids`, read after a durable write. A failed read is a
    warning, never an exception: the write already landed."""
    try:
        return graph_lines(store.task_graph(), entry_ids), []
    except Exception as exc:  # guidance only; the write is durable
        return [], [f"the write landed, but redrawing the task graph failed: {exc}"]


def embed_after_write(store: EntryStore, entry_ids: Sequence[str]) -> tuple[bool, list[str]]:
    """Embed the current versions for similarity search. Model download and inference raise
    types owned by urllib, onnxruntime and tokenizers, so this is broad on purpose."""
    try:
        engine = engine_seam.build_engine()
        if engine.is_available():
            return entry_search.embed_entries(store, engine, entry_ids) > 0, []
    except Exception as exc:
        return False, [f"written, but not embedded for similarity search: {exc}"]
    return False, []


def capture_code(
    store: EntryStore,
    ref_name: str,
    repos: Sequence[str],
    exclude: Sequence[str],
    allow_large: Sequence[str],
) -> tuple[list[CodeCapture], list[CaptureSummary], list[str]]:
    """Capture each repository's working tree into the store's capture repository under
    `refs/research/<ref_name>/<position>`. Returns captures, summaries and warnings, possible
    secrets included.

    Raises:
        ValidationError: an entry is not a git repository, two name one repository, or a file
            is over the size cap.
    """
    if (exclude or allow_large) and not repos:
        raise ValidationError("exclude and allow_large apply to repos; pass repos too")
    paths = _distinct_repos(repos)
    if not paths:
        return [], [], []
    capture_store = capture.store_path(store.db_path)
    plans = [capture.plan_capture(path, capture_store, exclude, allow_large) for path in paths]
    refusal = capture.size_refusal(plans, exclude, allow_large)
    if refusal is not None:
        raise ValidationError(refusal)
    capture_store = capture.open_store(store.db_path)
    tops = [plan.top for plan in plans]
    captures: list[CodeCapture] = []
    summaries: list[CaptureSummary] = []
    warnings: list[str] = []
    for position, plan in enumerate(plans):
        others = frozenset(top for top in tops if top != plan.top)
        outcome = capture.capture_repo(plan, capture_store, ref_name, position, others)
        captures.append(outcome.code)
        summaries.append(outcome.summary)
        warnings += outcome.warnings
        warnings += [
            f"possible secret: {finding.detector} in {finding.field} line {finding.line}"
            for finding in outcome.findings
        ]
    return captures, summaries, warnings


def discard_capture(store: EntryStore, ref_name: str) -> None:
    capture.discard_unpublished_refs(capture.store_path(store.db_path), ref_name)


def _distinct_repos(repos: Sequence[str]) -> list[Path]:
    try:
        paths = [Path(repo).expanduser() for repo in repos]
    except RuntimeError as exc:
        raise ValidationError(f"cannot expand a repos path: {exc}") from exc
    seen: dict[str, Path] = {}
    for path in paths:
        common = gitprobe.repo_id(path)
        if common is None:
            raise ValidationError(f"{path} is not a git repository")
        if common in seen:
            raise ValidationError(
                f"{seen[common]} and {path} are the same repository ({common}); each repos "
                "entry must be a different repository"
            )
        seen[common] = path
    return paths


def shown_entries(
    store: EntryStore,
    entries: Sequence[Entry],
    *,
    comments: int = 5,
    versions: bool = False,
    since_comment: str | None = None,
) -> list[ShownEntry]:
    """Present entries for `show`, with a bounded number of reads for any number of ids."""
    ids = [entry.id for entry in entries]
    graph = store.task_graph()
    incoming = store.incoming_links(ids)
    targets = {link.target_id for entry in entries for link in entry.links}
    others = {link.source_id for links in incoming.values() for link in links}
    related = store.get_entries(sorted(targets | others))
    sessions = store.get_sessions([entry.current.session_id or "" for entry in entries])
    shown: list[ShownEntry] = []
    for entry in entries:
        current = entry.current
        links = [
            ShownLink(
                direction="out",
                relation=link.relation,
                other_id=link.target_id,
                note=stale_link_text(
                    link,
                    related[link.target_id].current.version if link.target_id in related else None,
                ),
            )
            for link in entry.links
        ] + [
            ShownLink(direction="in", relation=link.relation, other_id=link.source_id, note="")
            for link in incoming.get(entry.id, [])
        ]
        every = store.get_comments(entry.id, limit=1_000_000)
        page = every.model_copy(update={"comments": every.comments[: max(comments, 0)]})
        unchanged = unchanged_responses(every.comments)
        session = sessions.get(current.session_id or "")
        since_lines: list[str] = []
        if since_comment is not None:
            since = store.changes_since_comment(since_comment)
            if since.comment.entry_id == entry.id:
                since_lines = [
                    f"{change.entry_id} {change.title}: "
                    + (
                        f"v{change.pinned} -> v{change.current}"
                        if change.pinned is not None
                        else f"new since the comment (v{change.current})"
                    )
                    for change in since.changed
                ] + [
                    f"{event.action} link {event.source_id} {event.relation} {event.target_id}"
                    + (f" · {event.reason}" if event.reason else "")
                    for event in since.links
                ]
                if not since_lines:
                    since_lines = ["nothing changed"]
        shown.append(
            ShownEntry(
                id=entry.id,
                kind=entry.kind,
                version=current.version,
                title=current.title,
                body=current.body,
                tags=current.tags,
                value=current.value,
                priority=current.priority,
                stated_by=current.stated_by,
                artifacts=[
                    artifact.location
                    + (f" {artifact.digest}" if artifact.digest else "")
                    + (f" · {artifact.notes}" if artifact.notes else "")
                    for artifact in current.artifacts
                ],
                code=[
                    f"{item.repo_path} captured {item.captured_sha[:12]}"
                    + (f" from {item.head_sha[:12]}" if item.head_sha else "")
                    + (" with uncommitted changes" if item.dirty else "")
                    for item in current.code
                ],
                projects=entry.projects,
                state=node_state(graph, entry.id) if entry.id in graph.nodes else entry.kind,
                standing=standing(entry, incoming.get(entry.id, [])),
                evidence_for=evidence_users(entry, incoming.get(entry.id, [])),
                links=links,
                task_events=[
                    f"{event.created_at.isoformat()} {event.event}"
                    + (f" {event.outcome}" if event.outcome else "")
                    + f" by {event.author} ({event.harness})"
                    + (f": {event.reason}" if event.reason else "")
                    for event in entry.task_events
                ],
                retired_reason=entry.retired_reason,
                author=current.author,
                harness=current.harness,
                session_id=current.session_id,
                transcript=session.transcript if session is not None else None,
                agent=session.agent if session is not None else None,
                created_at=current.created_at,
                comments=[
                    ShownComment(
                        id=item.id,
                        entry_version=item.entry_version,
                        author=item.author,
                        harness=item.harness,
                        session_id=item.session_id,
                        created_at=item.created_at,
                        body=item.body,
                        verdict=item.verdict,
                        addresses=item.addresses,
                        open=is_open_comment(item),
                        unchanged=item.id in unchanged,
                    )
                    for item in page.comments
                ],
                comment_total=page.total,
                comment_open=page.open,
                versions=[
                    ShownVersion(
                        version=item.version,
                        title=item.title,
                        reason=item.reason,
                        author=item.author,
                        harness=item.harness,
                        session_id=item.session_id,
                        created_at=item.created_at,
                    )
                    for item in (store.get_versions(entry.id) if versions else [])
                ],
                since=since_lines,
            )
        )
    return shown
