"""The `lint` operation.

Read-only. Every check is a fixed rule over stored fields, links or local files: no score, no
judgment, and nothing written.

NOTE: no `from __future__ import annotations` in this package. See `ops/__init__.py`.
"""

import json
import socket
from collections.abc import Sequence
from pathlib import Path
from typing import Annotated

from pydantic import Field

from trails import capture, engine as engine_seam, entry_search, hygiene
from trails.entry_store import EntryStore, cycle_warnings
from trails.models import Entry, Project
from trails.ops import context
from trails.ops._maint_types import LintItem, LintResult
from trails.ops._marks import Operation
from trails.reachability import code_reachability, path_state, reachability
from trails.secret_scan import scan_for_secrets

OPERATION = Operation(toolset="full", read_only=True, destructive=False, idempotent=True)

CATEGORIES = (
    "stale-link-versions",
    "dependency-cycles",
    "duplicate-tags",
    "changed-or-missing-artifacts",
    "unreachable-code",
    "orphan-capture-refs",
    "missing-paths",
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
    """List store problems: links written against an older version of their target, dependency cycles, likely duplicate tags, artifacts that changed or disappeared, captured commits that disappeared, capture refs naming no entry, registered paths that do not exist, entries without similarity embeddings, and possible secrets by entry, field and line. Changes nothing.

    Use this when: you are tidying a project, or before handing a project to another session.

    Use instead: prime, for where work stands; ready, for the next task.

    Returns: one section per category, each item naming the entry.
    """
    store = context.store()
    resolved = store.resolve_project(project) if project is not None else None
    if resolved is None:
        rows = store.query("SELECT id FROM entry ORDER BY id")
        projects = store.list_projects()
    else:
        rows = store.query(
            "SELECT e.id FROM entry e WHERE "
            + entry_search.membership_condition([resolved.id])
            + " ORDER BY e.id"
        )
        selected = store.get_project(resolved.id)
        projects = [selected] if selected is not None else []
    ids = [str(row["id"]) for row in rows]
    found = store.get_entries(ids)
    entries = [found[entry_id] for entry_id in ids if entry_id in found]
    capture_store = capture.store_path(store.db_path)
    categories: dict[str, list[LintItem]] = {
        "stale-link-versions": _stale_links(store, entries),
        "dependency-cycles": _cycles(store, ids),
        "duplicate-tags": _duplicate_tags(store, entries),
        "changed-or-missing-artifacts": _changed_or_missing_artifacts(entries),
        "unreachable-code": _unreachable_code(entries, capture_store),
        "orphan-capture-refs": _orphan_refs(store, capture_store),
        "missing-paths": _missing_paths(projects),
        "unembedded": _unembedded(store, ids),
        "unembedded-framing": _unembedded_framing(store, projects),
        "possible-secrets": _possible_secrets(entries, projects),
    }
    return LintResult(project=resolved, categories={name: categories[name] for name in CATEGORIES})


def _stale_links(store: EntryStore, entries: Sequence[Entry]) -> list[LintItem]:
    """Current links whose stored target version is below the target's current version."""
    targets = store.get_entries(sorted({link.target_id for e in entries for link in e.links}))
    items = []
    for entry in entries:
        for link in entry.links:
            target = targets.get(link.target_id)
            if (
                target is not None
                and link.target_version is not None
                and link.target_version < target.current.version
            ):
                items.append(
                    LintItem(
                        entry_id=entry.id,
                        detail=f"{link.relation} {link.target_id} written against "
                        f"v{link.target_version}, now v{target.current.version}",
                    )
                )
    return items


def _cycles(store: EntryStore, ids: Sequence[str]) -> list[LintItem]:
    graph = store.task_graph()
    return [
        LintItem(entry_id=None, detail=warning)
        for warning in cycle_warnings(graph, [item for item in ids if graph.is_work(item)])
    ]


def _duplicate_tags(store: EntryStore, entries: Sequence[Entry]) -> list[LintItem]:
    """Near-duplicate tag pairs that no alias already joins."""
    tags = sorted({tag for entry in entries for tag in entry.current.tags})
    aliases = store.alias_map()
    return [
        LintItem(entry_id=None, detail=f"{first} ~ {second}")
        for first, second in hygiene._near_duplicates(tags)
        if aliases.get(first, first) != aliases.get(second, second)
    ]


def _changed_or_missing_artifacts(entries: Sequence[Entry]) -> list[LintItem]:
    """Artifacts whose check ran and found them changed or absent. `unresolvable` and
    `referenced` mean the check did not run, so they are not listed."""
    items = []
    for entry in entries:
        for position, ref in enumerate(entry.current.artifacts):
            found = reachability(ref, verify=True)
            if found.status in {"changed", "absent"}:
                items.append(
                    LintItem(
                        entry_id=entry.id,
                        detail=f"artifact[{position}] {found.status}: {found.detail}",
                    )
                )
    return items


def _unreachable_code(entries: Sequence[Entry], store: Path) -> list[LintItem]:
    items = []
    for entry in entries:
        for item in entry.current.code:
            found = code_reachability(item, store)
            if found.status == "absent":
                items.append(LintItem(entry_id=entry.id, detail=f"code absent: {found.detail}"))
    return items


def _orphan_refs(store: EntryStore, capture_store: Path) -> list[LintItem]:
    """Capture-store refs `refs/research/<id>/<n>` whose id names no entry in the store.

    The capture store is one per database, so this check reads every ref whatever project
    was named.
    """
    named = hygiene.research_refs(capture_store) or []
    if not named:
        return []
    existing = set(store.get_entries(named))
    return [
        LintItem(
            entry_id=None,
            detail=f"{capture_store}: {hygiene.RESEARCH_REF_PREFIX}{ref}/ names no entry",
        )
        for ref in named
        if ref not in existing
    ]


def _missing_paths(projects: Sequence[Project]) -> list[LintItem]:
    """Registered local project paths on this host that do not exist."""
    return [
        LintItem(
            entry_id=None,
            detail=f"project {item.name}: {path.path} does not exist on "
            f"{path.host or socket.gethostname()}",
        )
        for item in projects
        for path in item.paths
        if path_state(path) == "missing"
    ]


def _unembedded(store: EntryStore, ids: Sequence[str]) -> list[LintItem]:
    """Entries whose current version has no chunk row for the current model. The model id
    is a constant of the engine, so this reads no model and needs no optional dependency."""
    if not ids:
        return []
    model = engine_seam.build_engine().model_id()
    wanted = set(ids)
    return [
        LintItem(entry_id=entry_id, detail=f"no embedding for {model}")
        for entry_id in entry_search.unembedded_ids(store, model)
        if entry_id in wanted
    ]


def _unembedded_framing(store: EntryStore, projects: Sequence[Project]) -> list[LintItem]:
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
            entry_id=None,
            detail=(
                f"project {project.id} ({project.name}) framing version "
                f"{project.current_framing.version}: no embedding for {model}"
            ),
        )
        for project in selected
        if project.current_framing is not None
        and (project.id, project.current_framing.version) not in embedded
    ]


def _possible_secrets(entries: Sequence[Entry], projects: Sequence[Project]) -> list[LintItem]:
    """Possible secrets in entries, project framing, and path metadata, never the text."""
    items = []
    for entry in entries:
        fields = {"title": entry.current.title, "body": entry.current.body}
        if entry.current.value is not None:
            fields["value"] = json.dumps(entry.current.value, sort_keys=True)
        for position, ref in enumerate(entry.current.artifacts):
            fields[f"artifact[{position}].location"] = ref.location
            fields[f"artifact[{position}].notes"] = ref.notes
        items += [
            LintItem(
                entry_id=entry.id,
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
                entry_id=None,
                detail=f"project {item.id} ({item.name}): {finding.detector} in "
                f"{finding.field} line {finding.line}",
            )
            for finding in scan_for_secrets(fields)
        ]
    return items
