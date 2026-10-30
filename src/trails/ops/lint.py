"""The `lint` operation.

Read-only. Every check is a fixed rule over stored fields or local files: no score, no
judgment, and nothing written.

NOTE: no `from __future__ import annotations` in this package. See `ops/__init__.py`.
"""

import json
import socket
from collections.abc import Sequence
from datetime import datetime, timedelta
from pathlib import Path
from typing import Annotated

from pydantic import Field

from trails import capture, engine as engine_seam, hygiene
from trails.models import Project, Record
from trails.ops import context
from trails.ops._maint_types import LintItem, LintResult
from trails.ops._marks import Operation
from trails.reachability import code_reachability, path_state, reachability
from trails.secret_scan import scan_for_secrets
from trails.store import Store
from trails.vocab import stale_days

OPERATION = Operation(toolset="full", read_only=True, destructive=False, idempotent=True)

CATEGORIES = (
    "duplicate-tags",
    "changed-or-missing-artifacts",
    "unreachable-code",
    "orphan-research-refs",
    "missing-paths",
    "stale-procedures",
    "unembedded",
    "unembedded-framing",
    "possible-secrets",
)


async def lint(
    project: Annotated[
        str | None,
        Field(description="Stable project id or exact unique name. Omit for the whole store."),
    ] = None,
) -> LintResult:
    """List store problems for a project: likely duplicate tags, artifacts that changed or disappeared, captured commits that disappeared, capture-store refs with no record, registered project paths that do not exist, procedures never verified or stale, records without similarity embeddings, and possible secrets in record fields and project access instructions, by record, field and line. Changes nothing.

    Use this when: you are tidying a project, or before handing a project to another session.

    Use instead: prime, for a project's open problem statements and counts of open work, rather than store problems.

    Returns: one section per category, each item naming the record.
    """
    store = context.store()
    resolved = store.resolve_project(project) if project is not None else None
    if resolved is None:
        rows = store.query("SELECT id FROM record ORDER BY id")
        projects = store.list_projects()
    else:
        rows = store.query(
            "SELECT r.id FROM record r WHERE EXISTS (SELECT 1 FROM record_project_event e "
            "WHERE e.record_id = r.id AND e.project_id = ? AND e.action = 'add' AND NOT "
            "EXISTS (SELECT 1 FROM record_project_event newer WHERE newer.record_id = "
            "e.record_id AND newer.project_id = e.project_id AND newer.sequence > e.sequence)) "
            "ORDER BY r.id",
            [resolved.id],
        )
        selected = store.get_project(resolved.id)
        projects = [selected] if selected is not None else []
    ids = [str(row["id"]) for row in rows]
    found = store.get_records(ids)
    records = [found[record_id] for record_id in ids if record_id in found]
    capture_store = capture.store_path(store.db_path)

    categories: dict[str, list[LintItem]] = {
        "duplicate-tags": _duplicate_tags(store, records),
        "changed-or-missing-artifacts": _changed_or_missing_artifacts(records),
        "unreachable-code": _unreachable_code(records, capture_store),
        "orphan-research-refs": _orphan_refs(store, capture_store),
        "missing-paths": _missing_paths(projects),
        "stale-procedures": _stale_procedures(records),
        "unembedded": _unembedded(store, ids),
        "unembedded-framing": _unembedded_framing(store, projects),
        "possible-secrets": _possible_secrets(records, projects),
    }
    return LintResult(
        project=resolved,
        categories={name: categories[name] for name in CATEGORIES},
    )


def _duplicate_tags(store: Store, records: Sequence[Record]) -> list[LintItem]:
    """Near-duplicate tag pairs that no alias already joins."""
    tags = sorted({tag for record in records for tag in record.tags})
    aliases = store.alias_map()
    return [
        LintItem(record_id=None, detail=f"{first} ~ {second}")
        for first, second in hygiene._near_duplicates(tags)
        if aliases.get(first, first) != aliases.get(second, second)
    ]


def _changed_or_missing_artifacts(records: Sequence[Record]) -> list[LintItem]:
    """Artifacts whose check ran and found them changed or absent. `unresolvable` and
    `referenced` mean the check did not run, so they are not listed."""
    items = []
    for record in records:
        for position, ref in enumerate(record.artifacts):
            found = reachability(ref, verify=True)
            if found.status in {"changed", "absent"}:
                items.append(
                    LintItem(
                        record_id=record.id,
                        detail=f"artifact[{position}] {found.status}: {found.detail}",
                    )
                )
    return items


def _unreachable_code(records: Sequence[Record], store: Path) -> list[LintItem]:
    items = []
    for record in records:
        for item in record.code:
            found = code_reachability(item, store)
            if found.status == "absent":
                items.append(LintItem(record_id=record.id, detail=f"code absent: {found.detail}"))
    return items


def _orphan_refs(store: Store, capture_store: Path) -> list[LintItem]:
    """Capture-store refs `refs/research/<id>/<n>` whose id names no record in the store.

    The capture store is one per database, so this check reads every ref whatever project
    was named.
    """
    named = hygiene.research_refs(capture_store) or []
    if not named:
        return []
    existing = set(store.get_records(named))
    return [
        LintItem(
            record_id=None,
            detail=f"{capture_store}: {hygiene.RESEARCH_REF_PREFIX}{ref}/ names no record",
        )
        for ref in named
        if ref not in existing
    ]


def _missing_paths(projects: Sequence[Project]) -> list[LintItem]:
    """Registered local project paths on this host that do not exist."""
    return [
        LintItem(
            record_id=None,
            detail=f"project {item.name}: {path.path} does not exist on "
            f"{path.host or socket.gethostname()}",
        )
        for item in projects
        for path in item.paths
        if path_state(path) == "missing"
    ]


def _stale_procedures(records: Sequence[Record]) -> list[LintItem]:
    """Current procedures never verified, or last verified more than stale_days() ago.

    A retired or superseded procedure is skipped: it is not followed any more, so it would be
    reported forever.
    """
    cutoff = datetime.now().astimezone().date() - timedelta(days=stale_days())
    items = []
    for record in records:
        if record.entry_type != "procedure" or record.retired or record.superseded_by:
            continue
        if record.last_verified is None:
            items.append(LintItem(record_id=record.id, detail=f"{record.title}: never verified"))
        elif record.last_verified[0] < cutoff:
            day, environment = record.last_verified
            items.append(
                LintItem(
                    record_id=record.id,
                    detail=f"{record.title}: last verified {day.isoformat()} in {environment}",
                )
            )
    return items


def _unembedded(store: Store, ids: Sequence[str]) -> list[LintItem]:
    """Records with no chunk row for the current model. The model id is a constant of the
    engine, so this reads no model and needs no optional dependency."""
    if not ids:
        return []
    model = engine_seam.build_engine().model_id()
    placeholders = ", ".join("?" * len(ids))
    embedded = {
        str(row["record_id"])
        for row in store.query(
            "SELECT DISTINCT record_id FROM record_chunk_embedding WHERE model_id = ? "
            f"AND record_id IN ({placeholders})",
            [model, *ids],
        )
    }
    return [
        LintItem(record_id=record_id, detail=f"no embedding for {model}")
        for record_id in ids
        if record_id not in embedded
    ]


def _unembedded_framing(store: Store, projects: Sequence[Project]) -> list[LintItem]:
    """Current framing versions with no row for the current embedding model."""
    selected = [project for project in projects if project.current_framing is not None]
    if not selected:
        return []
    model = engine_seam.build_engine().model_id()
    conditions = " OR ".join("(project_id = ? AND version = ?)" for _item in selected)
    params: list[str | int] = [model]
    for project in selected:
        framing = project.current_framing
        if framing is not None:
            params.extend([project.id, framing.version])
    embedded = {
        (str(row["project_id"]), int(row["version"]))
        for row in store.query(
            "SELECT DISTINCT project_id, version FROM project_framing_embedding "
            f"WHERE model_id = ? AND ({conditions})",
            params,
        )
    }
    return [
        LintItem(
            record_id=None,
            detail=(
                f"project {project.id} ({project.name}) framing version "
                f"{project.current_framing.version}: no embedding for {model}"
            ),
        )
        for project in selected
        if project.current_framing is not None
        and (project.id, project.current_framing.version) not in embedded
    ]


def _possible_secrets(records: Sequence[Record], projects: Sequence[Project]) -> list[LintItem]:
    """Possible secrets in records, project framing, and path metadata, never the text."""
    items = []
    for record in records:
        fields = {"title": record.title, "body": record.body}
        if record.value is not None:
            fields["value"] = json.dumps(record.value, sort_keys=True)
        for position, ref in enumerate(record.artifacts):
            fields[f"artifact[{position}].location"] = ref.location
            fields[f"artifact[{position}].notes"] = ref.notes
        items += [
            LintItem(
                record_id=record.id,
                detail=f"{finding.detector} in {finding.field} line {finding.line}",
            )
            for finding in scan_for_secrets(fields)
        ]
    for item in projects:
        fields = {
            f"{path.path} {part}": text
            for path in item.paths
            for part, text in (
                ("label", path.label),
                ("access_instructions", path.access_instructions),
            )
        }
        if item.current_framing is not None:
            fields["framing.title"] = item.current_framing.title
            fields["framing.body"] = item.current_framing.body
        items += [
            LintItem(
                record_id=None,
                detail=f"project {item.id} ({item.name}): {finding.detector} in "
                f"{finding.field} line {finding.line}",
            )
            for finding in scan_for_secrets(fields)
        ]
    return items
