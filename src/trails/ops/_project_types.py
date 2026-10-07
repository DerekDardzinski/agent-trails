"""What the register_project operation returns.

NOTE: no `from __future__ import annotations` in this package. See `ops/__init__.py`.
"""

from trails.models import ProjectPath
from trails.ops.types import RenderedResult
from trails.reachability import PathState


class RegisterProjectResult(RenderedResult):
    """A project after register_project: its paths as stored, and its project context."""

    project: str
    created: bool  # True when this call registered the project
    paths: list[tuple[ProjectPath, PathState]]  # every registered path, by path
    project_context_id: str
    project_context_created: bool
    warnings: list[str]

    def render_text(self) -> str:
        """The project, one line per path, warnings, then the show() call for the project context."""
        verb = "registered" if self.created else "updated"
        lines = [f"{verb} project {self.project} · {len(self.paths)} paths"]
        for item, state in self.paths:
            line = f"  {item.path}"
            if item.label:
                line += f"  {item.label}"
            if item.access_instructions:
                line += f" · access: {item.access_instructions}"
            lines.append(f"{line} · {state}")
        lines.append(
            f"{'created' if self.project_context_created else 'exists'} project context {self.project_context_id}"
        )
        lines += [f"warning: {warning}" for warning in self.warnings]
        lines.append(f'Next: show("{self.project_context_id}")')
        return "\n".join(lines)
