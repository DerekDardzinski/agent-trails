"""What the export and maintenance operations return.

A result that names a record ends with a Next: line.

NOTE: no `from __future__ import annotations` in this package. See `ops/__init__.py`.
"""

from datetime import date

from pydantic import BaseModel

from trails.models import ProjectRef
from trails.ops.types import RenderedResult


class ExportResult(RenderedResult):
    """A record written to a file."""

    record_id: str
    path: str

    def render_text(self) -> str:
        """Confirmation, then the publish call that reads the file back."""
        return "\n".join(
            [
                f"exported {self.record_id} to {self.path}; add a reason line to the header "
                "before publishing it back",
                f'Next: publish(from_file="{self.path}")',
            ]
        )


class AliasResult(RenderedResult):
    """Two tags recorded as meaning the same."""

    tag: str
    canonical: str

    def render_text(self) -> str:
        """The pair, then a search over the canonical tag."""
        return "\n".join(
            [
                f"alias {self.tag} -> {self.canonical}: a search for either finds both",
                f'Next: search(tags=["{self.canonical}"])',
            ]
        )


class MarkVerifiedResult(RenderedResult):
    """A verified stamp."""

    record_id: str
    verified_at: date
    environment: str

    def render_text(self) -> str:
        """The date and environment stamped, then the show() call."""
        return "\n".join(
            [
                f"verified {self.record_id} on {self.verified_at.isoformat()} in "
                f"{self.environment}",
                f'Next: show("{self.record_id}")',
            ]
        )


class EmbedResult(RenderedResult):
    """A backfill of record and current-framing similarity embeddings."""

    available: bool
    records: int
    framing_versions: int
    record_vectors: int
    framing_vectors: int

    def render_text(self) -> str:
        if not self.available:
            return (
                "embedding runtime not available: install the search extra "
                "(uv sync --extra search); nothing was written"
            )
        return "\n".join(
            [
                f"{self.record_vectors} record vectors written for {self.records} records",
                f"{self.framing_vectors} framing vectors written for "
                f"{self.framing_versions} current framing versions",
                "Next: search(...)",
            ]
        )


class LintItem(BaseModel):
    """One store problem, naming the record it is about when there is one."""

    record_id: str | None  # None for a tag pair or a ref that names no record
    detail: str


class LintResult(RenderedResult):
    """Store problems by category. Every category is present, empty when nothing was found."""

    project: ProjectRef | None
    categories: dict[str, list[LintItem]]

    def render_text(self) -> str:
        """One section per category, each item with a show() call for its record.

        "no problems found" alone when every category is empty. A named project that is not
        registered says so on the LAST line, after the counts, because a report ending in "no
        problems found" reads as a clean project when the name may simply be misspelled.
        """
        scope = (
            f"project {self.project.id} ({self.project.name})"
            if self.project is not None
            else "whole store"
        )
        found = sum(len(items) for items in self.categories.values())
        if found == 0:
            lines = [f"lint: {scope} · no problems found"]
        else:
            lines = [f"lint: {scope} · {found} problems"]
            for category, items in self.categories.items():
                lines.append(f"{category}: {len(items)}")
                for item in items:
                    call = f' · show("{item.record_id}")' if item.record_id is not None else ""
                    lines.append(f"  {item.detail}{call}")
        return "\n".join(lines)
