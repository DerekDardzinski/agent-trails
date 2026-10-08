"""Results for project registration and record membership operations.

NOTE: no `from __future__ import annotations` in this package. See `ops/__init__.py`.
"""

from trails.models import (
    ProjectFramingVersion,
    ProjectMembershipChange,
    ProjectPath,
    ProjectRef,
)
from trails.ops.types import RenderedResult
from trails.reachability import PathState


class RegisterProjectResult(RenderedResult):
    """Identity, paths, and framing affected by one atomic registration call."""

    project: ProjectRef
    created: bool
    changed: bool
    paths: list[tuple[ProjectPath, PathState]]
    framing: ProjectFramingVersion | None
    framing_created: bool
    current_framing_version: int | None
    notes: list[str]
    warnings: list[str]

    def render_text(self) -> str:
        """Render identity, framing summary, paths, notes, warnings, and next publish call."""
        state = "registered" if self.created else "already registered"
        lines = [f"project {self.project.id} · {self.project.name} · {state}"]
        if not self.changed:
            lines.append("no change detected")
        if self.framing is not None:
            verb = "created" if self.framing_created else "recognized retry of"
            lines.append(f"{verb} framing version {self.framing.version}: {self.framing.title}")
            if self.current_framing_version != self.framing.version:
                lines.append(f"current framing version is {self.current_framing_version}")
        elif self.current_framing_version is None:
            lines.append("no framing recorded")
        for item, state in self.paths:
            line = f"  {item.path}"
            if item.label:
                line += f"  {item.label}"
            if item.access_instructions:
                line += f" · access: {item.access_instructions}"
            lines.append(f"{line} · {state}")
        lines += [f"note: {note}" for note in self.notes]
        lines += [f"warning: {warning}" for warning in self.warnings]
        lines.append(f'Next: publish(..., projects=["{self.project.id}"])')
        return "\n".join(lines)


class UpdateProjectResult(RenderedResult):
    """Current project state after applying only effective updates."""

    project: ProjectRef
    changed: bool
    paths: list[tuple[ProjectPath, PathState]]
    framing: ProjectFramingVersion | None
    framing_changed: bool
    current_framing_version: int | None
    notes: list[str]
    warnings: list[str]

    def render_text(self) -> str:
        lines = [
            f"project {self.project.id} · {self.project.name} · "
            + ("updated" if self.changed else "no change detected")
        ]
        if self.framing is None:
            lines.append("framing: none recorded")
        elif self.framing_changed:
            lines.append(f"framing: created version {self.framing.version}: {self.framing.title}")
        else:
            lines.append(
                f"framing: no change detected, still version {self.framing.version}: "
                f"{self.framing.title}"
            )
        if self.framing is not None and self.current_framing_version != self.framing.version:
            lines.append(f"current framing version is {self.current_framing_version}")
        for item, state in self.paths:
            line = f"  {item.path}"
            if item.label:
                line += f"  {item.label}"
            if item.access_instructions:
                line += f" · access: {item.access_instructions}"
            lines.append(f"{line} · {state}")
        lines += [f"note: {note}" for note in self.notes]
        lines += [f"warning: {warning}" for warning in self.warnings]
        lines.append(f'Next: prime(project="{self.project.id}")')
        return "\n".join(lines)


class SetProjectsResult(RenderedResult):
    """The effective and idempotent parts of a record membership request."""

    record_id: str
    changed: list[ProjectMembershipChange]
    unchanged_add: list[ProjectRef]
    unchanged_remove: list[ProjectRef]
    projects: list[ProjectRef]

    def render_text(self) -> str:
        """Render changes, no-ops, complete current membership, and exact show call."""
        lines = [f"project memberships for {self.record_id}"]
        lines += [
            f"  {change.action}: {change.project.id} · {change.project.name}"
            for change in self.changed
        ]
        lines += [
            f"  already added: {project.id} · {project.name}" for project in self.unchanged_add
        ]
        lines += [
            f"  already absent: {project.id} · {project.name}" for project in self.unchanged_remove
        ]
        current = ", ".join(f"{item.id} ({item.name})" for item in self.projects) or "unscoped"
        lines.append(f"current: {current}")
        lines.append(f'Next: show("{self.record_id}")')
        return "\n".join(lines)
