"""The Markdown record file: TOML front matter between `+++` lines, then the body.

TOML rather than YAML because YAML silently coerces values such as `no` to false. The file
is written by a small serializer here, because the standard library reads TOML (`tomllib`)
but does not write it, and the format needs only strings, string arrays and tables of strings.

`value` is written as ONE TOML string holding the value's JSON. TOML has no null, so a JSON
object containing one could not be written as a TOML table, and two representations chosen
by content would be two formats. Reading also accepts a TOML table for `value`, which is what
an agent writing a file by hand is likely to type.
"""

from __future__ import annotations

import json
import tomllib
from typing import Any

import pydantic
from pydantic import BaseModel

from trails.errors import ValidationError
from trails.models import Record, RecordDraft
from trails.vocab import ENTRY_TYPES, WORK_STATUSES

FENCE = "+++"
READ_KEYS = frozenset(
    {
        "id",
        "entry_type",
        "work_status",
        "title",
        "tags",
        "project",
        "value",
        "links",
        "artifacts",
        "reason",
    }
)
# The keys a list item may carry. Checked here rather than left to pydantic, whose default
# drops an unknown key: a misspelled `digest` would then publish an artifact with no digest.
ITEM_KEYS = {
    "links": frozenset({"relation", "target_id"}),
    "artifacts": frozenset({"location", "digest", "notes"}),
}


class RecordFile(BaseModel):
    """A parsed record file."""

    id: str | None  # present when exported from an existing record
    reason: str | None
    draft: RecordDraft  # with supersedes set to id when id is present


def to_markdown(record: Record) -> str:
    """Render a record as Markdown with TOML front matter between `+++` lines.

    Front-matter keys written: id, entry_type, work_status, title, tags, project, value, links
    (list of {relation, target_id}), and artifacts (list of ArtifactRef fields). The body follows the
    closing `+++`. Code captures are not written to the file; they are provenance, and a
    record published from the file carries only the code captured by that publish call.
    from_markdown accepts the same keys plus `reason`.
    """
    lines = [
        FENCE,
        f"id = {_string(record.id)}",
        f"entry_type = {_string(record.entry_type)}",
        f"work_status = {_string(record.work_status)}",
        f"title = {_string(record.title)}",
        f"tags = [{', '.join(_string(tag) for tag in record.tags)}]",
    ]
    if record.project is not None:
        lines.append(f"project = {_string(record.project)}")
    if record.value is not None:
        lines.append(f"value = {_string(json.dumps(record.value, sort_keys=True))}")
    # A `supersedes` link is NOT written. Publishing the file supersedes THIS record, through
    # the header's id; the record's own supersedes link names its predecessor, and carried
    # into the file it would make the republished record supersede two records, which
    # write_record refuses as a supersedes in links.
    for link in record.links:
        if link.relation == "supersedes":
            continue
        lines += ["", "[[links]]", f"relation = {_string(link.relation)}"]
        lines.append(f"target_id = {_string(link.target_id)}")
    for artifact in record.artifacts:
        lines += ["", "[[artifacts]]", f"location = {_string(artifact.location)}"]
        if artifact.digest is not None:
            lines.append(f"digest = {_string(artifact.digest)}")
        lines.append(f"notes = {_string(artifact.notes)}")
    lines += [FENCE, ""]
    return "\n".join(lines) + "\n" + record.body


def from_markdown(text: str) -> RecordFile:
    """Parse the format to_markdown writes, with tomllib.

    Raises:
        ValidationError: missing or unterminated front matter; TOML syntax error with
            its line; unknown front-matter key; a field of the wrong type.
    """
    front, body = _split(text)
    try:
        header = tomllib.loads(front)
    except tomllib.TOMLDecodeError as exc:
        # The message carries the line and column within the front matter.
        raise ValidationError(f"front matter is not valid TOML: {exc}") from exc
    unknown = sorted(set(header) - READ_KEYS)
    if unknown:
        raise ValidationError(
            f"unknown front-matter key {', '.join(unknown)}; allowed: {', '.join(sorted(READ_KEYS))}"
        )
    if "entry_type" not in header:
        raise ValidationError(
            f"front matter is missing entry_type; allowed values: {', '.join(ENTRY_TYPES)}"
        )
    if "work_status" not in header:
        raise ValidationError(
            f"front matter is missing work_status; allowed values: {', '.join(WORK_STATUSES)}"
        )
    record_id = header.get("id")
    reason = header.get("reason")
    fields: dict[str, Any] = {
        key: header[key]
        for key in (
            "entry_type",
            "work_status",
            "title",
            "tags",
            "project",
            "links",
            "artifacts",
        )
        if key in header
    }
    if "value" in header:
        fields["value"] = _value(header["value"])
    for section, allowed in ITEM_KEYS.items():
        for position, item in enumerate(fields.get(section, [])):
            extra = (
                sorted(str(key) for key in set(item) - allowed) if isinstance(item, dict) else []
            )
            if extra:
                raise ValidationError(
                    f"unknown key {', '.join(extra)} in {section}[{position}]; "
                    f"allowed: {', '.join(sorted(allowed))}"
                )
    for artifact in fields.get("artifacts", []):
        if isinstance(artifact, dict):
            artifact.setdefault("digest", None)
    try:
        draft = RecordDraft(**fields, body=body, supersedes=record_id, reason=reason)
        return RecordFile(id=record_id, reason=reason, draft=draft)
    except pydantic.ValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
            for error in exc.errors()
        )
        raise ValidationError(f"front matter has a field of the wrong type: {problems}") from exc


def _split(text: str) -> tuple[str, str]:
    """The front matter and the body, split on the first two `+++` lines."""
    lines = text.split("\n")
    if not lines or lines[0].rstrip("\r") != FENCE:
        raise ValidationError(f"a record file starts with a {FENCE} line; this one does not")
    for index in range(1, len(lines)):
        if lines[index].rstrip("\r") == FENCE:
            body = "\n".join(lines[index + 1 :])
            # to_markdown separates the fence from the body with one blank line.
            return "\n".join(lines[1:index]), body.removeprefix("\n")
    raise ValidationError(f"the front matter is not closed: no second {FENCE} line")


def _value(raw: Any) -> Any:
    """`value` as written (a JSON string) or as typed by hand (a TOML table)."""
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValidationError(f"value is not JSON: {exc}") from exc
    return raw


def _string(text: str) -> str:
    """A TOML basic string.

    `json.dumps` escapes the quote, the backslash and U+0000 to U+001F, all with escapes TOML
    shares. `ensure_ascii=False`, because ASCII mode writes astral characters as surrogate
    pairs, which TOML refuses. U+007F is the one control character JSON leaves raw and TOML
    forbids, so it is escaped here.
    """
    return json.dumps(text, ensure_ascii=False).replace("\x7f", "\\u007f")
