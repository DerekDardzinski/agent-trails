"""The `publish` operation.

NOTE: no `from __future__ import annotations` in this package. See `ops/__init__.py`.
"""

import json
from pathlib import Path
from typing import Annotated, Any

from pydantic import Field

from trails import capture, dolt, engine as engine_seam, gitprobe, retrieval, vector
from trails.errors import RecordNotFoundError, ValidationError
from trails.locations import safe_resolve
from trails.models import ArtifactRef, Link, Record, RecordDraft
from trails.ops import context
from trails.ops._guidance import (
    CapturedRepo,
    contradiction_notes,
    repoint_notes,
    result_hypothesis_link_notes,
    superseded_target_warnings,
    uncaptured_location_warnings,
)
from trails.ops._marks import Operation
from trails.ops._write_types import PublishResult
from trails.record_file import from_markdown
from trails.store import Store
from trails.vocab import EntryType, WorkStatus

OPERATION = Operation(toolset="agent", read_only=False, destructive=False, idempotent=False)


async def publish(  # noqa: PLR0912, PLR0915, PLR0917 — operation orchestration; public params
    entry_type: Annotated[
        EntryType | None,
        Field(
            description=(
                "What this record is: problem-statement, hypothesis, procedure, result, "
                "verification, fact, insight, decision, report or note. Use decision for a "
                "rule or choice and its reason, procedure for steps someone can follow and "
                "verify, and fact for a statement about data or an environment. Required "
                "unless from_file supplies it."
            )
        ),
    ] = None,
    work_status: Annotated[
        WorkStatus | None,
        Field(
            description=(
                "Open means a later action is expected on this exact record, for example an "
                "untested hypothesis or a note with an unresolved question; closed means no "
                "later action is expected on it. A record may be current and closed. Required "
                "unless from_file supplies it. Work status says nothing about truth."
            )
        ),
    ] = None,
    title: Annotated[
        str,
        Field(
            description=(
                "One line stating the claim or outcome rather than only naming the activity; "
                "search and prime list this title when orienting an agent."
            )
        ),
    ] = "",
    body: Annotated[str, Field(description="The content, in Markdown.")] = "",
    tags: Annotated[
        list[str],
        Field(
            description=(
                "Open-ended descriptive labels for subjects, methods, datasets or analysis "
                "properties. Do not repeat entry_type here."
            )
        ),
    ] = [],  # noqa: B006 — contracted default; never mutated
    projects: Annotated[
        list[str] | None,
        Field(
            description=(
                "Project ids or exact unique names for this record. For a new record, omitted "
                "means unscoped. For a superseding record, omitted inherits current memberships; "
                "an explicit list replaces them, and [] intentionally makes it unscoped."
            )
        ),
    ] = None,
    value: Annotated[
        dict[str, Any] | None,
        Field(
            description=(
                "Structured numbers or fields as a JSON object, for example "
                '{"did_estimate": -0.0041, "ci95": [-0.0093, 0.0011]}.'
            )
        ),
    ] = None,
    links: Annotated[
        list[Link],
        Field(
            description=(
                'List of {"relation": ..., "target_id": ...} from this new record. builds_on: '
                "this record builds on the target (a result built on a hypothesis, a "
                "verification of a result). contradicts: this record conflicts with or "
                "corrects the target, including a correction of a number or a cause; prime "
                "and search show it on the target. cites: the target is a source. Use supersedes, not "
                "links, to replace a record."
            )
        ),
    ] = [],  # noqa: B006 — contracted default; never mutated
    artifacts: Annotated[
        list[ArtifactRef],
        Field(
            description=(
                'References to data, each {"location": ..., "digest": null or '
                '"sha256:<64 lowercase hex>", "notes": ...}. location is a path, an s3:// URI, '
                "a query, or a description; notes say what identifies the data, such as a "
                "version, a row count or a query date. Data is referenced, never copied, so "
                "a location in /tmp or another temporary directory will disappear. An "
                "uppercase hex digest is refused."
            )
        ),
    ] = [],  # noqa: B006 — contracted default; never mutated
    repos: Annotated[
        list[str],
        Field(
            description=(
                "Git repositories whose full working tree, uncommitted and untracked files "
                "included, is captured as a pinned commit without changing your files or "
                "index. Pass every repository whose code produced this record. Write analysis "
                "scripts inside a git repository, not in /tmp or another temporary directory: "
                "trails captures code only from repos, and publish warns about a script it "
                "could not capture. Captured code "
                "is not carried into a superseding record; pass repos again when publishing a "
                "revision of a result. Each entry must be a different repository; a "
                "repository and its own linked worktree cannot be captured in one publish. A "
                "file over 50 MiB (TRAILS_MAX_CAPTURE_BYTES) refuses the publish until it is "
                "named in exclude or allow_large."
            )
        ),
    ] = [],  # noqa: B006 — contracted default; never mutated
    exclude: Annotated[
        list[str],
        Field(
            description=(
                "Paths relative to a repository's top level to leave out of its capture, "
                "tracked or not. A directory leaves out everything under it."
            )
        ),
    ] = [],  # noqa: B006 — contracted default; never mutated
    allow_large: Annotated[
        list[str],
        Field(
            description=(
                "Paths relative to a repository's top level to capture even though they are "
                "over the size cap."
            )
        ),
    ] = [],  # noqa: B006 — contracted default; never mutated
    supersedes: Annotated[
        str | None,
        Field(
            description=(
                "Id of the record this one replaces. Requires reason. Links, artifacts, "
                "captured code and verification stamps do not carry into the new record; "
                "supply them again when they still apply, or use export as an editable "
                "starting copy of the old links and artifacts."
            )
        ),
    ] = None,
    reason: Annotated[
        str | None,
        Field(description="One sentence saying what changed relative to the superseded record."),
    ] = None,
    from_file: Annotated[
        str | None,
        Field(
            description=(
                "Path to a file written by export. The file supplies entry_type, work_status, "
                "title, body, tags, projects, value, links and artifacts; leave those parameters "
                "empty. When the file's header names an existing id, the new record supersedes "
                "it and the "
                "header must include a reason line. repos, exclude and allow_large may be "
                "combined with from_file."
            )
        ),
    ] = None,
) -> PublishResult:
    """Write a record. Use it for anything worth keeping: a result, a procedure that finally worked, a decision and its reason, a fact about an environment, a hypothesis, a verification of another result, or a note. Nothing is reviewed or gated; the record is stored as given. A write is refused only for malformed input or an attempt to overwrite a record. A possible secret does not stop the write: it is reported in warnings, and you must tell the user, because removing it from history requires a manual database-history rewrite by the user.

    Use this when: you learned something a later session would otherwise have to rediscover. Store a procedure the moment it works, not at handoff.

    Do not write a script or data file that a record will name to /tmp or another temporary directory: the operating system deletes files there, and trails keeps only the reference, never a copy. Write scripts inside a git repository and pass it in repos, so the exact code is captured, and keep data at a permanent location named in artifacts.

    Use instead: mark_verified (full toolset), to record that a procedure still works: a verification record does not stamp the procedure, and show and search keep reporting it as never verified; link, to relate two records that already exist; retire, when a record is no longer true and nothing replaces it; export and then publish(from_file=...), to revise a long record from an editable copy of its links and artifacts. A superseding record starts unverified and inherits no links, artifacts, captured code or verification stamps unless this call supplies them again.

    Returns: the new record's id, the id it superseded if any, whether it was embedded for similarity search, one line per captured repository with its changed files and largest files, notes about expected link behavior, and warnings that need attention, including any possible secret by field, line and detector.
    """
    if from_file is not None:
        given = {
            "entry_type": entry_type,
            "work_status": work_status,
            "title": title,
            "body": body,
            "tags": tags,
            "projects": projects,
            "value": value,
            "links": links,
            "artifacts": artifacts,
            "supersedes": supersedes,
            "reason": reason,
        }
        clashing = [
            name
            for name, argument in given.items()
            if argument or (name == "projects" and argument is not None)
        ]
        if clashing:
            raise ValidationError(
                f"from_file supplies {', '.join(clashing)}; leave those parameters empty"
            )
        try:
            # expanduser raises RuntimeError for an unknown user, as in "~nosuchuser/x".
            text = Path(from_file).expanduser().read_text()
        except (OSError, UnicodeDecodeError, RuntimeError) as exc:
            raise ValidationError(f"cannot read {from_file}: {exc}") from exc
        draft = from_markdown(text).draft
    else:
        if entry_type is None or work_status is None:
            missing = [
                name
                for name, value in (("entry_type", entry_type), ("work_status", work_status))
                if value is None
            ]
            raise ValidationError(
                f"direct publish requires {', '.join(missing)}; a file supplies them through "
                "from_file"
            )
        draft = RecordDraft(
            entry_type=entry_type,
            work_status=work_status,
            title=title,
            body=body,
            tags=tags,
            projects=projects,
            value=value,
            links=links,
            artifacts=artifacts,
            supersedes=supersedes,
            reason=reason,
        )

    if (exclude or allow_large) and not repos:
        raise ValidationError("exclude and allow_large apply to repos; pass repos too")
    store = context.store()
    if draft.projects is not None:
        resolved_projects = store.resolve_projects(draft.projects)
        draft = draft.model_copy(update={"projects": [item.id for item in resolved_projects]})
    store.validate_record_draft(draft)
    target_ids = list(dict.fromkeys(link.target_id for link in draft.links))
    link_targets = store.get_records(target_ids)
    missing_targets = [target for target in target_ids if target not in link_targets]
    if missing_targets:
        raise ValidationError(f"no existing record has id {', '.join(missing_targets)}")
    to_replace: Record | None = None
    if draft.supersedes is not None:
        try:
            to_replace = store.get_record(draft.supersedes)
        except RecordNotFoundError as exc:
            raise ValidationError(f"no existing record has id {draft.supersedes}") from exc
        current = store.superseding_tip(draft.supersedes)
        if current is not None:
            raise ValidationError(
                f"{draft.supersedes} is already superseded; current version is {current}; "
                f"supersede {current} instead"
            )
    # Read before the write, so nothing after the durable point has to read the store.
    tips = {
        target.id: store.superseding_tip(target.id) or str(target.superseded_by)
        for target in link_targets.values()
        if target.superseded_by is not None
    }
    repoint = (
        _repoint_sources(store, to_replace)
        if to_replace is not None and to_replace.entry_type == "problem-statement"
        else ([], [])
    )

    repo_paths = _distinct_repos(repos)
    capture_store = capture.store_path(store.db_path)
    plans = [capture.plan_capture(path, capture_store, exclude, allow_large) for path in repo_paths]
    refusal = capture.size_refusal(plans, exclude, allow_large)
    if refusal is not None:
        raise ValidationError(refusal)

    record_id = draft.id
    captures = []
    summaries = []
    findings = []
    notices: list[str] = []
    if plans:
        # The id is minted before capture so the ref name and the record agree.
        record_id = record_id or store.new_record_id(draft.entry_type)
        capture_store = capture.open_store(store.db_path)
        resolved = [plan.top for plan in plans]
        for position, plan in enumerate(plans):
            others = frozenset(other for other in resolved if other != plan.top)
            captured = capture.capture_repo(plan, capture_store, record_id, position, others)
            captures.append(captured.code)
            summaries.append(captured.summary)
            findings.extend(captured.findings)
            notices.extend(captured.warnings)

    draft = draft.model_copy(update={"id": record_id, "code": [*draft.code, *captures]})
    try:
        if draft.supersedes is None:
            replaced = None
            outcome = store.write_record(draft)
        else:
            # The warning and the superseding write see one database state. In particular, a
            # verification stamp cannot land after the read and disappear from the warning.
            with dolt.exclusive(store.db_path):
                replaced = store.get_record(draft.supersedes)
                outcome = store.write_record(draft)
    except ValidationError:
        if plans and record_id is not None and record_id not in store.get_records([record_id]):
            capture.discard_unpublished_refs(capture_store, record_id)
        raise
    # THE RECORD IS DURABLE from here, so nothing below may raise: an exception would hide a
    # successful write and invite a retry that duplicates it.
    record_id = outcome.record.id
    warnings = [
        f"possible secret: {finding.detector} in {finding.field} line {finding.line}"
        for finding in [*outcome.secret_findings, *findings]
    ]
    warnings.extend(notices)
    notes = _supersession_notes(replaced)
    warnings.extend(_supersession_warnings(replaced, outcome.record))
    notes.extend(
        result_hypothesis_link_notes(
            record_id, outcome.record.entry_type, outcome.record.links, link_targets
        )
    )
    # Guidance only, so broad on purpose: path resolution raises RuntimeError on a symlink
    # loop, git may be missing, and none of it may cost the agent the confirmation of a write.
    try:
        notes.extend(
            contradiction_notes(
                record_id, outcome.record.links, link_targets, superseded_now=draft.supersedes
            )
        )
        warnings.extend(
            superseded_target_warnings(record_id, outcome.record.links, link_targets, tips)
        )
        notes.extend(repoint_notes(record_id, *repoint))
        warnings.extend(
            uncaptured_location_warnings(
                record_id,
                outcome.record.artifacts,
                [
                    outcome.record.body,
                    json.dumps(outcome.record.value or {}, ensure_ascii=False),
                ],
                [
                    CapturedRepo(top=safe_resolve(plan.top), exclude=tuple(plan.exclude))
                    for plan in plans
                ],
            )
        )
    except Exception as exc:
        warnings.append(f"the record was written, but checking its links and paths failed: {exc}")

    # Broad on purpose, because model download and inference raise types owned by urllib,
    # onnxruntime and tokenizers.
    embedded = False
    try:
        engine = engine_seam.build_engine()
        if engine.is_available():
            embedded = vector.embed_record(store, engine, record_id) > 0
    except Exception as exc:
        warnings.append(f"the record was written but not embedded for similarity search: {exc}")

    return PublishResult(
        record_id=record_id,
        entry_type=outcome.record.entry_type,
        work_status=outcome.record.work_status,
        projects=outcome.record.projects,
        superseded_id=draft.supersedes,
        embedded=embedded,
        captures=summaries,
        notes=notes,
        warnings=warnings,
    )


def _distinct_repos(repos: list[str]) -> list[Path]:
    """The repos as paths, refused before anything is written to git.

    Two entries sharing one git common dir are one repository: capturing it twice under one
    record would record the same code twice under two paths.

    Raises:
        ValidationError: an entry is not a git repository, or two share one git common dir.
    """
    try:
        paths = [Path(repo).expanduser() for repo in repos]
    except RuntimeError as exc:  # expanduser on an unknown user, as in "~nosuchuser/x"
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


def _repoint_sources(store: Store, replaced: Record) -> tuple[list[str], list[str]]:
    """The replaced record and its earlier versions, and the current records that build on
    any of them, in link-creation order. One read per earlier version: chains are short."""
    chain = [replaced.id]
    current: Record | None = replaced
    while current is not None:
        earlier = next(
            (link.target_id for link in current.links if link.relation == "supersedes"), None
        )
        if earlier is None or earlier in chain:
            break
        chain.append(earlier)
        current = store.get_records([earlier]).get(earlier)
    incoming = retrieval.incoming_links_many(store, chain)
    source_ids = list(
        dict.fromkeys(
            source
            for target in chain
            for relation, source in incoming.get(target, [])
            if relation == "builds_on"
        )
    )
    sources = store.get_records(source_ids)
    return chain, [
        source
        for source in source_ids
        if source in sources
        and sources[source].superseded_by is None
        and not sources[source].retired
    ]


def _supersession_notes(replaced: Record | None) -> list[str]:
    """Expected information about what an append-only replacement does not inherit."""
    if replaced is None:
        return []
    return [
        "superseding records inherit no links, artifacts, captured code or verification "
        "stamps; this record contains only what this call supplied"
    ]


def _supersession_warnings(replaced: Record | None, written: Record) -> list[str]:
    """A prior procedure stamp the replacement no longer holds."""
    if replaced is None or written.entry_type != "procedure" or replaced.last_verified is None:
        return []
    day, environment = replaced.last_verified
    return [
        f"the procedure this replaces was verified {day.isoformat()} in {environment}; "
        f"{written.id} starts unverified"
    ]
