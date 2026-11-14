"""What the project-writing operations share: path normalization before the write, and the
warnings and framing embedding after it.

NOTE: no `from __future__ import annotations` in this package. See `ops/__init__.py`.
"""

from collections.abc import Sequence
from pathlib import Path

from trails import engine as engine_seam, vector
from trails.entry_store import EntryStore
from trails.errors import ValidationError
from trails.models import ProjectFramingDraft, ProjectPath, ProjectWriteOutcome
from trails.reachability import is_uri, path_state
from trails.secret_scan import scan_for_secrets


def normalized_paths(paths: Sequence[ProjectPath], host: str) -> list[ProjectPath]:
    """Each path stripped and `~`-expanded, with this host filled in for a local path."""
    given: list[ProjectPath] = []
    for item in paths:
        location = item.path.strip()
        if not is_uri(location):
            try:
                location = str(Path(location).expanduser())
            except RuntimeError as exc:
                raise ValidationError(f"cannot expand {item.path}: {exc}") from exc
        given.append(
            item.model_copy(
                update={"path": location, "host": "" if is_uri(location) else (item.host or host)}
            )
        )
    return given


def after_project_write(
    store: EntryStore,
    outcome: ProjectWriteOutcome,
    given: Sequence[ProjectPath],
    framing: ProjectFramingDraft | None,
) -> list[str]:
    """Warnings about what is outside the store (a path that does not exist, a URI trails
    cannot check, a possible secret) and a failed framing embedding. Never raises: the write
    is durable."""
    warnings: list[str] = []
    for item in given:
        if is_uri(item.path):
            kind = "s3 URI" if item.path.startswith("s3://") else "URI"
            warnings.append(f"{item.path}: not checked ({kind})")
        elif path_state(item) == "missing":
            warnings.append(f"{item.path} does not exist on {item.host}")
    fields = {
        f"{item.path} {part}": text
        for item in given
        for part, text in (("label", item.label), ("access_instructions", item.access_instructions))
    }
    if framing is not None:
        fields.update({"framing.title": framing.title, "framing.body": framing.body})
    warnings += [
        f"possible secret: {finding.detector} in {finding.field} line {finding.line}"
        for finding in scan_for_secrets(fields)
    ]
    if outcome.framing_created and outcome.framing is not None:
        try:
            engine = engine_seam.build_engine()
            if engine.is_available():
                vector.embed_project_framings(store, engine, [(outcome.project, outcome.framing)])
        except Exception as exc:  # the framing is durable; report the embedding failure
            warnings.append(
                f"the framing was written but not embedded for similarity search: {exc}"
            )
    return warnings
