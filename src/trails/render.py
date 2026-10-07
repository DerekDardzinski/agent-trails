"""Every text rendering of a reading result, in one module.

Each function formats data a result model already holds. Nothing here reads the store or
explains why records are semantically similar.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from trails.vocab import PROJECT_CONTEXT_TEMPLATE

if TYPE_CHECKING:
    from trails.models import Record
    from trails.ops._read_types import (
        ArtifactStatus,
        LineageResult,
        PathStatus,
        PrimeEntry,
        PrimeResult,
        RelatedRecord,
        SearchHit,
        SearchResult,
        ShownRecord,
        ShowResult,
    )


def lifecycle(record: Record) -> str:
    """current, superseded by <id>, or retired."""
    if record.retired:
        return "retired"
    if record.superseded_by is not None:
        return f"superseded by {record.superseded_by}"
    return "current"


def record_line(record: Record) -> str:
    """One-line record identity, lifecycle, work status, and verification date."""
    line = (
        f"{record.id}  {record.entry_type}  {record.title}  {lifecycle(record)} "
        f"· work {record.work_status}"
    )
    if record.last_verified is not None:
        line += f" · verified {record.last_verified[0].isoformat()}"
    return line


def call(name: str, *positional: Any, **arguments: Any) -> str:
    """A tool call as an agent would write it: `show("rec-1", detail="full")`.

    Values are JSON, so strings are quoted and escaped. A keyword argument that is None, an
    empty string, an empty list or False is omitted.
    """
    parts = [_value(value) for value in positional]
    parts += [
        f"{key}={_value(value)}"
        for key, value in arguments.items()
        if value is not None and value is not False and value not in ("", [])
    ]
    return f"{name}({', '.join(parts)})"


def next_line(hints: list[str]) -> str:
    """The final line of every reading response."""
    return "Next: " + " · ".join(hints)


def search_text(result: SearchResult) -> str:
    """State line, one compact preview per hit or the no-match report, then Next:."""
    lines = [result.state]
    for hit in result.hits:
        lifecycle = hit.lifecycle
        if lifecycle == "superseded" and hit.superseded_by is not None:
            lifecycle += f" by {hit.superseded_by}"
        lines.append(
            f"{hit.id}  {hit.entry_type}  {hit.title}  {lifecycle} · work {hit.work_status}"
        )
        details = []
        if hit.project is not None:
            details.append(f"project: {hit.project}")
        details.append(f"created: {hit.created_at.date().isoformat()}")
        lines.append("  " + " · ".join(details))
        matched = _matched(hit)
        if matched:
            lines.append(f"  matched by: {matched}")
        if hit.excerpt is not None:
            lines.append(f"  excerpt: {hit.excerpt}")
        lines.append(f"  → {call('show', hit.id)}")
    if result.total == 0:
        if result.filters:
            applied = ", ".join(f"{k}={_shown(v)}" for k, v in result.filters.items())
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


SUMMARY_LINES = 12
_UNREACHABLE = frozenset({"absent", "changed"})
_DETAILS = ("summary", "full", "provenance")


def show_text(result: ShowResult) -> str:
    """One unchanged single-record view, or ordered record blocks and one Next for an array."""
    if not result.multiple:
        return _shown_record_text(result.records[0], result.detail, include_next=True)

    lines = [
        f"state: {len(result.records)} records found · {len(result.missing)} not found · "
        f"detail {result.detail}"
    ]
    if result.missing:
        lines.append("not found: " + ", ".join(result.missing))
    for item in result.records:
        lines += ["", *_shown_record_text(item, result.detail, include_next=False).splitlines()]

    found_ids = [item.record.id for item in result.records]
    hints: list[str] = []
    for item in result.records:
        record = item.record
        if item.contradicted_by:
            hints.append(
                f"{item.contradicted_by} contradict {record.id}: "
                + call("search", links=f"contradicts:{record.id}")
            )
        if record.superseded_by is not None:
            hints.append(f"{record.id} was replaced: {call('show', record.superseded_by)}")
    if found_ids:
        for level in _DETAILS:
            if level != result.detail:
                hints.append(
                    call("show", found_ids, detail=level)
                    if level != "summary"
                    else call("show", found_ids)
                )
        hints.append(call("lineage", found_ids[0]))
    else:
        hints.append("search(query=...)")
    lines.append(next_line(hints))
    return "\n".join(lines)


def _shown_record_text(  # noqa: PLR0912 — three explicit detail modes plus navigation
    item: ShownRecord, detail: str, *, include_next: bool
) -> str:
    """The established single-record text, optionally without its navigation line."""
    record = item.record
    lines = [
        f"state: {record.id} · lifecycle {lifecycle(record)} · work {record.work_status} · "
        f"detail {detail}",
        record_line(record),
        f"tags: {', '.join(record.tags) if record.tags else 'none'}",
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
    else:
        lines.append("code:" if item.captures else "code: none captured")
        store = item.capture_store
        for capture_status in item.captures:
            capture = capture_status.capture
            lines.append(
                f"  {capture.repo_path} @ {capture.captured_sha} · "
                f"{capture_status.reachability.status}"
            )
            lines.append(
                f"    read files: git --git-dir={store} show {capture.captured_sha}:<path>"
            )
            if capture.dirty and capture.head_sha is not None:
                lines.append(
                    f"    uncommitted changes, while {capture.repo_path} exists: "
                    f"GIT_ALTERNATE_OBJECT_DIRECTORIES={store}/objects git -C "
                    f"{capture.repo_path} diff {capture.head_sha} {capture.captured_sha}"
                )
        lines += _artifacts(item.artifacts)
        if record.project is None:
            lines.append("project paths: none, the record has no project")
        elif not item.project_registered:
            lines.append(
                f"project paths: project {record.project} is not registered: "
                f"register_project(name={_value(record.project)}, paths=[...])"
            )
        else:
            lines += project_path_lines(item.project_paths)
        value = json.dumps(record.value, ensure_ascii=False) if record.value is not None else "none"
        lines.append(f"value: {value}")

    if not include_next:
        return "\n".join(lines)
    hints: list[str] = []
    if item.contradicted_by:
        hints.append(
            f"{item.contradicted_by} contradict this · "
            + call("search", links=f"contradicts:{record.id}")
        )
    if record.superseded_by is not None:
        hints.append(f"superseded by {record.superseded_by} · {call('show', record.superseded_by)}")
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


def project_path_lines(paths: list[PathStatus]) -> list[str]:
    """`project paths:` then one line per registered path, or `project paths: none`."""
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


def lineage_text(result: LineageResult) -> str:
    """State line, the record, one line per ancestor and descendant, Next: line."""
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
    if first:
        lines.append(next_line([call("show", first[0].record.id)]))
    else:
        lines.append(next_line([call("search", query=record.title)]))
    return "\n".join(lines)


def prime_text(result: PrimeResult) -> str:
    """The current state, grouped by entry type, with the links that determine scientific state."""
    if result.store_error is not None:
        return "\n".join([f"state: store unavailable: {result.store_error}", "Next: version()"])
    scope = f"project {result.project}" if result.project is not None else "whole store"
    lines = [
        f"state: {scope} · {result.current_count} current records · "
        f"{result.replaced_count} replaced by newer versions · {result.retired_count} retired"
    ]
    if result.project is not None:
        if not result.registered:
            lines.append(
                f"project {result.project} is not registered: "
                f"register_project(name={_value(result.project)}, paths=[...])"
            )
        else:
            lines += project_path_lines(result.paths)
    lines += _project_context_lines(result.project_contexts, whole_store=result.project is None)
    lines += _problem_statement_lines(result.problem_statements, whole_store=result.project is None)

    ordered = [name for name in _PRIME_GROUP_ORDER if name in result.groups]
    ordered += sorted(name for name in result.groups if name not in _PRIME_GROUP_ORDER)
    for kind in ordered:
        entries = result.groups[kind]
        lines += ["", f"{_group_label(kind)} ({len(entries)} current):"]
        for entry in entries:
            lines += _prime_entry_lines(kind, entry)

    shown = [entry.record.id for kind in ordered for entry in result.groups[kind]]
    replacement = next(
        (
            entry.record.id
            for kind in ordered
            for entry in result.groups[kind]
            if _replaces(entry.record) is not None
        ),
        None,
    )
    lines += ["", "Next:"]
    if shown:
        lines.append(f"  read full records: {call('show', shown, detail='full')}")
    if replacement is not None:
        lines.append(f"  inspect replacement history: {call('lineage', replacement)}")
    if result.project is not None:
        lines.append(f"  find anything else: search(project={_value(result.project)}, query=...)")
    else:
        lines.append("  find anything else: search(query=...)")
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


def _project_context_lines(records: list[Record], *, whole_store: bool) -> list[str]:
    label = f"project contexts ({len(records)} current):" if whole_store else "project context:"
    lines = ["", label]
    if not records:
        return [*lines, "  none recorded"]
    for position, record in enumerate(records):
        if position:
            lines.append("")
        lines += [
            f"  project: {record.project or 'none'}",
            f"  record id: {record.id}",
            f"  lifecycle: {lifecycle(record)}",
            f"  work status: {record.work_status}",
            f"  read full record: {call('show', record.id)}",
        ]
        if record.body == PROJECT_CONTEXT_TEMPLATE:
            lines.append("  template only, nothing recorded")
        elif not record.body:
            lines.append("  empty")
        else:
            lines += ["", record.body]
    return lines


def _problem_statement_lines(records: list[Record], *, whole_store: bool) -> list[str]:
    lines = ["", f"problem statements ({len(records)} current):"]
    if not records:
        return [*lines, "  none recorded"]
    for record in records:
        if whole_store:
            lines += [
                f"  project: {record.project or 'none'}",
                f"  record id: {record.id}",
                f"  title: {record.title}",
                f"  work status: {record.work_status}",
            ]
        else:
            lines.append(f"  {record.id}  {record.title} · work {record.work_status}")
        if record.body:
            lines += [f"    {line}" for line in record.body.splitlines()]
    return lines


def _prime_entry_lines(kind: str, entry: PrimeEntry) -> list[str]:
    record = entry.record
    facts: list[str] = [f"work {record.work_status}"]
    if kind == "result":
        facts.append(
            "verified by " + ", ".join(item.id for item in entry.verifications)
            if entry.verifications
            else "not verified"
        )
        if entry.builds_on:
            facts.append("builds on " + ", ".join(_target(item) for item in entry.builds_on))
        else:
            facts.append("builds on nothing")
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
    line = f"  {record.id}  {record.title}"
    if facts:
        line += " · " + " · ".join(facts)
    lines = [line]
    if kind == "hypothesis":
        for link in entry.incoming_results:
            if link.relation not in {"cites", "contradicts"}:
                continue
            verb = "cited by" if link.relation == "cites" else "contradicted by"
            lines.append(
                f"    {verb}: {link.record.id} (result; {link.relation} does not count as a "
                "test, use builds_on)"
            )
    return lines


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
        line = (
            f"  {event.sequence}  {event.work_status} · {event.changed_at.isoformat()} · "
            f"{event.author}"
        )
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
            continue
        if not hit.keyword_matches:
            parts.append("keyword")
            continue
        locations = ", ".join(f'"{match.term}" in {match.field}' for match in hit.keyword_matches)
        parts.append(f"keyword {locations}")
    return " · ".join(parts)


def _value(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def _shown(value: Any) -> str:
    if isinstance(value, list):
        return "[" + ", ".join(str(item) for item in value) + "]"
    return str(value)
