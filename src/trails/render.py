"""Every text rendering of a reading result, in one module."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from trails.models import Record
    from trails.ops._read_types import (
        ArtifactStatus,
        LineageResult,
        PathStatus,
        PrimeEntry,
        PrimeResult,
        ProjectSearchHit,
        RelatedRecord,
        SearchHit,
        SearchResult,
        ShownProject,
        ShownRecord,
        ShowResult,
    )


def lifecycle(record: Record) -> str:
    if record.retired:
        return "retired"
    if record.superseded_by is not None:
        return f"superseded by {record.superseded_by}"
    return "current"


def record_line(record: Record) -> str:
    line = (
        f"{record.id}  {record.entry_type}  {record.title}  {lifecycle(record)} "
        f"· work {record.work_status}"
    )
    if record.last_verified is not None:
        line += f" · verified {record.last_verified[0].isoformat()}"
    return line


def call(operation: str, *positional: Any, **arguments: Any) -> str:
    parts = [_value(value) for value in positional]
    parts += [
        f"{key}={_value(value)}"
        for key, value in arguments.items()
        if value is not None and value is not False and value not in ("", [])
    ]
    return f"{operation}({', '.join(parts)})"


def next_line(hints: list[str]) -> str:
    return "Next: " + " · ".join(hints)


def search_text(result: SearchResult) -> str:  # noqa: PLR0912 — project and record sections
    lines = [result.state]
    if result.project_hits:
        lines.append("project matches:")
        for hit in result.project_hits:
            lines += _project_hit_lines(hit)
        hidden = result.project_total - len(result.project_hits)
        if hidden > 0:
            lines.append(
                f"  {hidden} more project matches; {call('prime')} starts the project index"
            )
    if result.hits:
        lines.append("record matches:")
    for hit in result.hits:
        status = hit.lifecycle
        if status == "superseded" and hit.superseded_by is not None:
            status += f" by {hit.superseded_by}"
        lines.append(f"{hit.id}  {hit.entry_type}  {hit.title}  {status} · work {hit.work_status}")
        details = [f"created: {hit.created_at.date().isoformat()}"]
        if hit.projects:
            details.insert(
                0,
                "projects: " + ", ".join(f"{item.id} ({item.name})" for item in hit.projects),
            )
        else:
            details.insert(0, "projects: unscoped")
        lines.append("  " + " · ".join(details))
        matched = _matched(hit)
        if matched:
            lines.append(f"  matched by: {matched}")
        if hit.excerpt is not None:
            lines.append(f"  excerpt: {hit.excerpt}")
        lines.append(f"  → {call('show', hit.id)}")
    if result.total == 0 and result.project_total == 0:
        if result.filters:
            applied = ", ".join(f"{key}={_shown(value)}" for key, value in result.filters.items())
            lines.append(f"filters applied: {applied}")
        for name, count, first in result.relaxed:
            noun = "match" if count == 1 else "matches"
            lines.append(f"without the {name} filter: {count} {noun} ({first})")
        for tag, others in result.tag_aliases.items():
            if others:
                lines.append(f'tag aliases: "{tag}" also matches {", ".join(others)}')
            else:
                lines.append(f'tag aliases: none for "{tag}"')
    lines.append(next_line(result.next))
    return "\n".join(lines)


def _project_hit_lines(hit: ProjectSearchHit) -> list[str]:
    framing = hit.framing_title or "no framing recorded"
    lines = [f"  {hit.id}  {hit.name} · {framing} · updated {hit.updated_at.date().isoformat()}"]
    matched = _matched_project(hit)
    if matched:
        lines.append(f"    matched by: {matched}")
    if hit.excerpt is not None:
        lines.append(f"    excerpt: {hit.excerpt}")
    lines.append(f"    → {call('prime', project=hit.id)}")
    return lines


SUMMARY_LINES = 12
_UNREACHABLE = frozenset({"absent", "changed"})
_DETAILS = ("summary", "full", "provenance")


def show_text(result: ShowResult) -> str:
    if not result.multiple:
        if result.records:
            return _shown_record_text(result.records[0], result.detail, include_next=True)
        return _shown_project_text(result.projects[0], result.detail, include_next=True)

    lines = [
        f"state: {len(result.records)} records and {len(result.projects)} projects found · "
        f"{len(result.missing)} not found · detail {result.detail}"
    ]
    if result.missing:
        lines.append("not found: " + ", ".join(result.missing))
    records = {item.record.id: item for item in result.records}
    projects = {item.project.id: item for item in result.projects}
    for requested in result.requested:
        if requested in records:
            block = _shown_record_text(records[requested], result.detail, include_next=False)
        elif requested in projects:
            block = _shown_project_text(projects[requested], result.detail, include_next=False)
        else:
            continue
        lines += ["", *block.splitlines()]
    found_ids = [item for item in result.requested if item in records or item in projects]
    hints = [
        call("show", found_ids, detail=level) if level != "summary" else call("show", found_ids)
        for level in _DETAILS
        if level != result.detail
    ]
    first_record = next((item for item in result.requested if item in records), None)
    if first_record is not None:
        hints.append(call("lineage", first_record))
    if not hints:
        hints.append("search(query=...)")
    lines.append(next_line(hints))
    return "\n".join(lines)


def _shown_record_text(item: ShownRecord, detail: str, *, include_next: bool) -> str:
    record = item.record
    lines = [
        f"state: {record.id} · lifecycle {lifecycle(record)} · work {record.work_status} · detail {detail}",
        record_line(record),
        f"tags: {', '.join(record.tags) if record.tags else 'none'}",
        "projects: "
        + (
            ", ".join(f"{project.id} ({project.name})" for project in record.projects)
            if record.projects
            else "unscoped"
        ),
    ]
    if detail != "summary":
        lines += _work_status_history(record)
    if detail == "summary":
        body = record.body.splitlines()
        lines += body[:SUMMARY_LINES]
        if len(body) > SUMMARY_LINES:
            lines.append(f"({len(body) - SUMMARY_LINES} more lines)")
        lines += _related("links out", item.outgoing)
        lines += _related("links in", item.incoming)
        lines += _artifacts(item.artifacts)
    elif detail == "full":
        lines += record.body.splitlines()
        lines += _membership_history(item)
    else:
        lines.append("code:" if item.captures else "code: none captured")
        for capture_status in item.captures:
            capture = capture_status.capture
            lines.append(
                f"  {capture.repo_path} @ {capture.captured_sha} · {capture_status.reachability.status}"
            )
            lines.append(
                f"    read files: git --git-dir={item.capture_store} show {capture.captured_sha}:<path>"
            )
            if capture.dirty and capture.head_sha is not None:
                lines.append(
                    f"    uncommitted changes, while {capture.repo_path} exists: "
                    f"GIT_ALTERNATE_OBJECT_DIRECTORIES={item.capture_store}/objects git -C "
                    f"{capture.repo_path} diff {capture.head_sha} {capture.captured_sha}"
                )
        lines += _artifacts(item.artifacts)
        lines += _record_project_path_lines(item)
        value = json.dumps(record.value, ensure_ascii=False) if record.value is not None else "none"
        lines.append(f"value: {value}")

    if include_next:
        hints: list[str] = []
        if item.contradicted_by:
            hints.append(
                f"{item.contradicted_by} contradict this · "
                + call("search", links=f"contradicts:{record.id}")
            )
        if record.superseded_by is not None:
            hints.append(
                f"superseded by {record.superseded_by} · {call('show', record.superseded_by)}"
            )
        unreachable = sum(
            1 for artifact in item.artifacts if artifact.reachability.status in _UNREACHABLE
        )
        if unreachable:
            hints.append(f"{unreachable} artifacts absent or changed")
        hints += [
            call("show", record.id, detail=level) if level != "summary" else call("show", record.id)
            for level in _DETAILS
            if level != detail
        ]
        hints.append(call("lineage", record.id))
        lines.append(next_line(hints))
    return "\n".join(lines)


def _shown_project_text(item: ShownProject, detail: str, *, include_next: bool) -> str:
    project = item.project
    lines = [
        f"state: project {project.id} · {project.name} · detail {detail}",
        f"created {item.created_at.isoformat()} by {item.created_by} on {item.created_on_host}",
        f"current records: {item.current_record_count}",
    ]
    if detail == "summary":
        lines.append(
            "framing: "
            + (
                f"version {item.current_framing.version}: {item.current_framing.title}"
                if item.current_framing is not None
                else "no framing recorded"
            )
        )
        lines.append(f"paths: {len(item.paths)}")
    elif detail == "full":
        lines += _framing_history(item)
    else:
        lines += project_path_lines(item.paths)
    if include_next:
        hints = [call("prime", project=project.id)]
        hints += [
            call("show", project.id, detail=level)
            if level != "summary"
            else call("show", project.id)
            for level in _DETAILS
            if level != detail
        ]
        lines.append(next_line(hints))
    return "\n".join(lines)


def _framing_history(item: ShownProject) -> list[str]:
    if not item.framing_history:
        return ["framing history: none recorded"]
    lines = ["framing history:"]
    for framing in item.framing_history:
        line = (
            f"  version {framing.version} · {framing.created_at.isoformat()} · "
            f"{framing.author} · {framing.title}"
        )
        if framing.reason is not None:
            line += f" · {framing.reason}"
        lines.append(line)
        lines += [f"    {body_line}" for body_line in framing.body.splitlines()]
    return lines


def _membership_history(item: ShownRecord) -> list[str]:
    if not item.membership_history:
        return ["project membership history: none"]
    lines = ["project membership history:"]
    for event in item.membership_history:
        line = (
            f"  {event.created_at.isoformat()} · {event.project.id} ({event.project.name}) · "
            f"sequence {event.sequence} · {event.action} · {event.author}"
        )
        if event.reason is not None:
            line += f" · {event.reason}"
        lines.append(line)
    return lines


def project_path_lines(paths: list[PathStatus]) -> list[str]:
    if not paths:
        return ["project paths: none"]
    lines = ["project paths:"]
    for item in paths:
        line = f"  {item.path.path}"
        if item.path.label:
            line += f"  {item.path.label}"
        if item.path.access_instructions:
            line += f" · access: {item.path.access_instructions}"
        lines.append(f"{line} · {item.state}")
    return lines


def _record_project_path_lines(item: ShownRecord) -> list[str]:
    if not item.record.projects:
        return ["project paths: none, the record is unscoped"]
    if not item.project_paths:
        return ["project paths: none"]
    lines = ["project paths:"]
    for status in item.project_paths:
        line = f"  {status.project.id} ({status.project.name}) · {status.path.path}"
        if status.path.label:
            line += f"  {status.path.label}"
        if status.path.access_instructions:
            line += f" · access: {status.path.access_instructions}"
        lines.append(f"{line} · {status.state}")
    return lines


def lineage_text(result: LineageResult) -> str:
    record = result.record
    lines = [
        f"state: {record.id} · {len(result.ancestors)} ancestors · "
        f"{len(result.descendants)} descendants · up to 5 steps",
        record_line(record),
    ]
    for label, related in (("ancestor", result.ancestors), ("descendant", result.descendants)):
        for item in related:
            steps = "step" if item.depth == 1 else "steps"
            lines.append(
                f"  {label} · {item.relation} · {item.depth} {steps}  {record_line(item.record)}"
            )
    first = [*result.ancestors, *result.descendants]
    lines.append(
        next_line(
            [call("show", first[0].record.id)] if first else [call("search", query=record.title)]
        )
    )
    return "\n".join(lines)


def prime_text(result: PrimeResult) -> str:
    if result.store_error is not None:
        return "\n".join([f"state: store unavailable: {result.store_error}", "Next: version()"])
    if result.project is None:
        shown_from = result.offset + 1 if result.projects else 0
        shown_to = result.offset + len(result.projects)
        lines = [
            f"state: {result.project_total} projects · showing {shown_from}-{shown_to} · "
            f"{result.unscoped_count} current unscoped records"
        ]
        if not result.projects:
            lines.append("projects: none registered")
        for item in result.projects:
            framing = item.framing_title or "no framing recorded"
            lines.append(
                f"{item.project.id}  {item.project.name} · {framing} · "
                f"{item.record_count} current · {item.open_count} open · "
                f"last activity {item.last_activity.isoformat()}"
            )
            lines.append(f"  → {call('prime', project=item.project.id)}")
        hints = []
        if shown_to < result.project_total:
            hints.append(
                f"{result.project_total - shown_to} more projects · "
                + call("prime", limit=result.limit, offset=shown_to)
            )
        hints += ["register_project(name=..., paths=[...], framing=...)", "search(query=...)"]
        lines.append(next_line(hints))
        return "\n".join(lines)

    project = result.project
    lines = [
        f"state: project {project.id} · {project.name} · {result.current_count} current records · "
        f"{result.replaced_count} replaced by newer versions · {result.retired_count} retired"
    ]
    lines += project_path_lines(result.paths)
    lines += ["", "framing:"]
    if result.framing is None:
        lines.append("  no framing recorded")
    else:
        framing = result.framing
        line = (
            f"  version {framing.version} · {framing.created_at.isoformat()} · "
            f"{framing.author} · {framing.title}"
        )
        if framing.reason is not None:
            line += f" · {framing.reason}"
        lines.append(line)
        lines += [f"    {body_line}" for body_line in framing.body.splitlines()]
        lines.append(
            "  revise: "
            + call(
                "update_project",
                project=project.id,
                framing={"title": framing.title, "body": "..."},
                base_version=framing.version,
                reason="...",
            )
        )
    lines += _problem_statement_lines(
        result.problem_statements, result.problem_statement_contradicted_by
    )
    ordered = [name for name in _PRIME_GROUP_ORDER if name in result.groups]
    ordered += sorted(name for name in result.groups if name not in _PRIME_GROUP_ORDER)
    for kind in ordered:
        entries = result.groups[kind]
        lines += ["", f"{_group_label(kind)} ({len(entries)} current):"]
        for entry in entries:
            lines += _prime_entry_lines(kind, entry)
    shown = [entry.record.id for kind in ordered for entry in result.groups[kind]]
    hints = []
    if shown:
        hints.append(f"read full records: {call('show', shown, detail='full')}")
    hints.append(f"find anything else: {call('search', projects=[project.id], query='...')}")
    hints.append(f"framing history: {call('show', project.id, detail='full')}")
    lines += ["", next_line(hints)]
    return "\n".join(lines)


_PRIME_GROUP_ORDER = (
    "decision",
    "result",
    "hypothesis",
    "procedure",
    "fact",
    "insight",
    "verification",
    "report",
    "note",
)
_GROUP_LABELS = {
    "decision": "decisions",
    "result": "results",
    "hypothesis": "hypotheses",
    "procedure": "procedures",
    "fact": "facts",
    "insight": "insights",
    "verification": "verifications",
    "report": "reports",
    "note": "notes",
}


def _problem_statement_lines(
    records: list[Record], contradicted_by: dict[str, list[Record]]
) -> list[str]:
    lines = ["", f"problem statements ({len(records)} current):"]
    if not records:
        return [*lines, "  none recorded"]
    for record in records:
        lines.append(f"  {record.id}  {record.title} · work {record.work_status}")
        lines += _contradicted_by_lines(contradicted_by.get(record.id, []))
        lines += [f"    {line}" for line in record.body.splitlines()]
    return lines


def _contradicted_by_lines(sources: list[Record], skip: set[str] | None = None) -> list[str]:
    return [
        f"    contradicted by: {source.id} ({source.entry_type}) {source.title}"
        for source in sources
        if source.id not in (skip or set())
    ]


def _prime_entry_lines(kind: str, entry: PrimeEntry) -> list[str]:
    record = entry.record
    facts: list[str] = [f"work {record.work_status}"]
    if kind == "result":
        facts.append(
            "verified by " + ", ".join(item.id for item in entry.verifications)
            if entry.verifications
            else "not verified"
        )
        facts.append(
            "builds on " + ", ".join(_target(item) for item in entry.builds_on)
            if entry.builds_on
            else "builds on nothing"
        )
    elif kind == "hypothesis":
        results = [item.record for item in entry.incoming_results if item.relation == "builds_on"]
        facts.append(
            "results built on this version: " + ", ".join(item.id for item in results)
            if results
            else "no result built on this version"
        )
    elif kind == "procedure":
        facts.append(
            f"verified {record.last_verified[0].isoformat()} in {record.last_verified[1]}"
            if record.last_verified is not None
            else "never verified"
        )
    if replaced := _replaces(record):
        facts.append(f"replaces {replaced}")
    lines = [f"  {record.id}  {record.title} · " + " · ".join(facts)]
    listed: set[str] = set()
    if kind == "hypothesis":
        for link in entry.incoming_results:
            if link.relation in {"cites", "contradicts"}:
                verb = "cited by" if link.relation == "cites" else "contradicted by"
                lines.append(
                    f"    {verb}: {link.record.id} (result; {link.relation} does not count as a test, use builds_on)"
                )
                if link.relation == "contradicts":
                    listed.add(link.record.id)
    return lines + _contradicted_by_lines(entry.contradicted_by, listed)


def _target(record: Record) -> str:
    labels = [record.entry_type, f"work {record.work_status}"]
    if record.superseded_by is not None:
        labels.append(f"replaced by {record.superseded_by}")
    elif record.retired:
        labels.append("retired")
    return f"{record.id} ({', '.join(labels)})"


def _replaces(record: Record) -> str | None:
    return next((link.target_id for link in record.links if link.relation == "supersedes"), None)


def _group_label(kind: str) -> str:
    return _GROUP_LABELS.get(kind, f"{kind} records")


def _work_status_history(record: Record) -> list[str]:
    lines = ["work status history:"]
    for event in record.work_status_history:
        line = f"  {event.sequence}  {event.work_status} · {event.changed_at.isoformat()} · {event.author}"
        if event.reason is not None:
            line += f" · {event.reason}"
        lines.append(line)
    return lines


def _related(label: str, related: list[RelatedRecord]) -> list[str]:
    if not related:
        return [f"{label}: none"]
    return [f"{label}:"] + [f"  {item.relation}  {record_line(item.record)}" for item in related]


def _artifacts(artifacts: list[ArtifactStatus]) -> list[str]:
    if not artifacts:
        return ["artifacts: none"]
    return ["artifacts:"] + [
        f"  {item.reachability.status}  {item.ref.location} · {item.reachability.detail}"
        for item in artifacts
    ]


def _matched(hit: SearchHit) -> str:
    parts: list[str] = []
    for method in hit.matched_by:
        if method != "keyword":
            parts.append(method)
        elif not hit.keyword_matches:
            parts.append("keyword")
        else:
            locations = ", ".join(
                f'"{match.term}" in {match.field}' for match in hit.keyword_matches
            )
            parts.append(f"keyword {locations}")
    return " · ".join(parts)


def _matched_project(hit: ProjectSearchHit) -> str:
    parts: list[str] = []
    for method in hit.matched_by:
        if method != "keyword":
            parts.append(method)
        elif not hit.keyword_matches:
            parts.append("keyword")
        else:
            locations = ", ".join(
                f'"{match.term}" in {match.field}' for match in hit.keyword_matches
            )
            parts.append(f"keyword {locations}")
    return " · ".join(parts)


def _value(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _shown(value: Any) -> str:
    if isinstance(value, list):
        return "[" + ", ".join(str(item) for item in value) + "]"
    return str(value)
