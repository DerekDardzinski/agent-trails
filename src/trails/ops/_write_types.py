"""What the publish, link, retire and version operations return.

Each result renders a confirmation line naming the record, and ends with `Next: show("<id>")`
for the record it names. `version` names no record, so it has no Next: line.

NOTE: no `from __future__ import annotations` in this package. See `ops/__init__.py`.
"""

import shlex

from trails.capture import human_size
from trails.models import CaptureSummary, ProjectRef
from trails.ops.types import RenderedResult
from trails.vocab import EntryType, Relation, WorkStatus


def _next_show(record_id: str) -> str:
    return f'Next: show("{record_id}")'


class PublishResult(RenderedResult):
    """A published record."""

    record_id: str
    entry_type: EntryType
    work_status: WorkStatus
    projects: list[ProjectRef]
    superseded_id: str | None
    embedded: bool
    captures: list[CaptureSummary]
    notes: list[str]
    warnings: list[str]

    def render_text(self) -> str:
        """Confirmation, one line per capture, the exclude commands, warnings, show() call.

        The two printf commands are printed for the user's repository and never run: trails
        writes nothing there.
        """
        line = f"published {self.record_id} · {self.entry_type} · work {self.work_status}"
        if self.superseded_id is not None:
            line += f", superseding {self.superseded_id}"
        line += " · embedded" if self.embedded else " · not embedded"
        lines = [line]
        scope = ", ".join(f"{item.id} ({item.name})" for item in self.projects) or "unscoped"
        lines.append(f"projects: {scope}")
        for item in self.captures:
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
        lines += [f"note: {note}" for note in self.notes]
        lines += [f"warning: {warning}" for warning in self.warnings]
        lines.append(_next_show(self.record_id))
        return "\n".join(lines)


class LinkResult(RenderedResult):
    """A stored link."""

    source_id: str
    relation: Relation
    target_id: str
    notes: list[str]

    def render_text(self) -> str:
        """Confirmation, notes, then the show() call for the source record."""
        return "\n".join(
            [
                f"linked {self.source_id} {self.relation} {self.target_id}",
                *(f"note: {note}" for note in self.notes),
                _next_show(self.source_id),
            ]
        )


class RetireResult(RenderedResult):
    """A retired record."""

    record_id: str
    reason: str  # the reason the store holds, which is the first one given
    already_retired: bool  # True when this call changed nothing

    def render_text(self) -> str:
        """Confirmation with the standing reason, then the show() call."""
        line = (
            f"already retired {self.record_id}, nothing changed: {self.reason}"
            if self.already_retired
            else f"retired {self.record_id}: {self.reason}"
        )
        return "\n".join([line, _next_show(self.record_id)])


class VersionResult(RenderedResult):
    """The installed version and the database a call would use."""

    version: str
    db_path: str

    def render_text(self) -> str:
        """One line. No Next: line, because no record is named."""
        return f"trails {self.version} · database {self.db_path}"


class SetWorkStatusResult(RenderedResult):
    """The latest work status after a requested transition."""

    record_id: str
    work_status: WorkStatus
    reason: str | None
    changed: bool

    def render_text(self) -> str:
        """Explicit changed or unchanged result, then the show call."""
        if self.changed:
            line = f"work status for {self.record_id} set to {self.work_status}: {self.reason}"
        else:
            line = f"work status for {self.record_id} already {self.work_status}; nothing changed"
        return "\n".join([line, _next_show(self.record_id)])
