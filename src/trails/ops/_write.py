"""The write path every create and revise operation shares: refuse before capture, capture
outside the write lock, write, then guidance, embedding and the graph redraw.

NOTE: no `from __future__ import annotations` in this package. See `ops/__init__.py`.
"""

import json
import secrets
from collections.abc import Sequence
from pathlib import Path

from trails.entry_store import EntryStore
from trails.errors import ValidationError
from trails.locations import safe_resolve
from trails.models import CodeCapture, EntryDraft, RepoCapture
from trails.ops._common import capture_code, discard_capture, embed_after_write, redraw
from trails.ops._entry_types import WriteResult
from trails.ops._guidance import CapturedRepo, uncaptured_location_warnings
from trails.vocab import WORK_KINDS, Kind


def write(
    store: EntryStore, operation: str, draft: EntryDraft, repos: Sequence[RepoCapture] | None
) -> WriteResult:
    """Write `draft` as `operation` (the name a refusal's retry call uses), capturing `repos`
    when given; `repos=[]` clears a revision's captures and None keeps them."""
    creating = draft.id is None
    new_id: str | None = None
    ref_name: str | None = None
    entry_id, version = "", 0
    summaries = []
    warnings: list[str] = []
    # Every refusal before capture, so a refused write leaves no capture ref or store. The
    # capture itself runs OUTSIDE the write lock, because every reader in every session waits
    # for that lock: a search took 7.35 s during a 7.5 s capture (trails fac-wqx5c).
    # `write_entry` repeats every check under the lock, so a base_version that went stale
    # during the capture is still refused, and the capture's refs are then removed; an id
    # another write took meanwhile is replaced there and the refs are moved to the new id.
    store.check_entry_draft(draft)
    if repos:
        if creating:
            kind: Kind = draft.kind  # ty: ignore[invalid-assignment]
            new_id = store.new_entry_id(kind)
            entry_id, version = new_id, 1
        else:
            entry_id = str(draft.id)
            version = store.get_entry(entry_id).current.version + 1
        # A token per attempt, so two writers capturing the same version never share a ref.
        ref_name = f"{entry_id}/v{version}/{secrets.token_hex(4)}"
        captures, summaries, warnings = capture_code(store, ref_name, repos, operation)
        draft = draft.model_copy(update={"code": captures})
    elif repos is not None:
        draft = draft.model_copy(update={"code": []})
    try:
        outcome = store.write_entry(draft, new_id=new_id, capture_ref=ref_name)
    except ValidationError:
        if ref_name is not None and not _landed(store, entry_id, version, draft.code or []):
            discard_capture(store, ref_name)
        raise
    # THE ENTRY IS DURABLE from here, so nothing below may raise.
    entry = outcome.entry
    warnings += [
        f"possible secret: {finding.detector} in {finding.field} line {finding.line}; tell the "
        "person, because removing it needs a manual database-history rewrite"
        for finding in outcome.secret_findings
    ]
    warnings += outcome.warnings
    # Guidance only, so broad on purpose: path resolution raises RuntimeError on a symlink
    # loop and git may be missing; none of it may cost the agent the confirmation of a write.
    try:
        warnings += uncaptured_location_warnings(
            entry.kind,
            entry.id,
            entry.current.version,
            entry.current.artifacts if draft.artifacts is not None or creating else [],
            [draft.body or "", json.dumps(draft.value or {}, ensure_ascii=False)],
            [
                CapturedRepo(top=safe_resolve(Path(item.repo_path)), exclude=tuple(item.excluded))
                for item in summaries
            ],
        )
    except Exception as exc:
        warnings.append(f"written, but checking its paths failed: {exc}")
    embedded, embed_warnings = embed_after_write(store, [entry.id])
    warnings += embed_warnings
    graph = []
    if entry.kind in WORK_KINDS:
        graph, graph_warnings = redraw(store, [entry.id])
        warnings += graph_warnings
    return WriteResult(
        id=entry.id,
        kind=entry.kind,
        version=entry.current.version,
        created=outcome.created,
        retry=outcome.retry,
        unchanged_from=outcome.unchanged_from,
        projects=entry.projects,
        embedded=embedded,
        captures=summaries,
        graph=graph,
        warnings=warnings,
    )


def _landed(store: EntryStore, entry_id: str, version: int, code: list[CodeCapture]) -> bool:
    """Whether version `version` of `entry_id` is durable with THIS capture, in which case its
    pin must stay. A version written by a competing writer holds other captures, so its
    existence alone does not count."""
    if not code:
        return False
    rows = store.query(
        "SELECT captured_sha FROM version_code WHERE entry_id = ? AND version = ? AND "
        "captured_sha = ?",
        [entry_id, version, code[0].captured_sha],
    )
    return bool(rows)
