"""The store: schema, reads, and the one write path.

The store stores, links and tracks records and decides nothing. It refuses exactly two things,
both as ValidationError: malformed input, and an attempt to overwrite a record. Every record
write is an INSERT; nothing updates or deletes record, framing, or membership history. Current
project paths alone are mutable metadata, with older states retained in Dolt history.

Three facts about Dolt shape almost every function here, all probed:

* One `dolt sql -q` invocation is ONE transaction, and `CALL DOLT_COMMIT` must be its single
  finisher. It raises "nothing to commit" on a transaction that changed nothing, so every
  idempotent path returns BEFORE reaching it.
* `dolt sql -r json` OMITS a NULL column from the row object rather than emitting null, so
  nullable columns are read with `.get`.
* Every read and write is a subprocess, and invocations are the cost. `get_records` therefore
  assembles any number of records in ONE invocation, one SELECT per table.

Every text column but project_path.path is declared `COLLATE utf8mb4_0900_ai_ci`. Dolt's default, utf8mb4_0900_bin,
makes FULLTEXT MATCH and LIKE case-sensitive (measured on dolt 2.4.0: MATCH 'lift' did not find
'Lift'). Note that `SHOW FULL COLUMNS` reports the declared collation inside its `Type` field
while its `Collation` field reads utf8mb4_0900_bin regardless; `information_schema.columns`
reports it in COLLATION_NAME.
"""

from __future__ import annotations

import difflib
import json
import os
import re
import secrets
import unicodedata
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Literal

from trails import dolt
from trails.author import current_author
from trails.errors import DoltError, RecordNotFoundError, ValidationError
from trails.models import (
    ArtifactRef,
    CodeCapture,
    Link,
    Project,
    ProjectFramingDraft,
    ProjectFramingVersion,
    ProjectMembershipChange,
    ProjectMembershipEvent,
    ProjectPath,
    ProjectRef,
    ProjectWriteOutcome,
    Record,
    RecordDraft,
    SetProjectsOutcome,
    WorkStatusEvent,
    WriteOutcome,
)
from trails.secret_scan import scan_for_secrets
from trails.vocab import (
    ENTRY_TYPE_PREFIXES,
    ENTRY_TYPES,
    ID_ALPHABET,
    ID_ATTEMPTS_PER_LENGTH,
    ID_LENGTHS,
    ID_PATTERN,
    PROJECT_ACTIONS,
    PROJECT_ID_PATTERN,
    PROJECT_ID_PREFIX,
    RELATIONS,
    WORK_STATUSES,
    EntryType,
    ProjectAction,
    Relation,
    WorkStatus,
    enum_column,
)

DEFAULT_DB_ENV = "TRAILS_DB"
# Written in the database directory once the schema is known to exist.
SCHEMA_MARKER = ".trails-schema"
# The redesign starts on a fresh database (D13, D25); the old `~/.trails/db` is left as it is.
DEFAULT_DB_PATH = Path.home() / ".agent-trails" / "db"

EMBEDDING_DIMENSIONS = 384

_TEXT = "COLLATE utf8mb4_0900_ai_ci"

# VARCHAR widths, in characters. A value over its width is refused as ValidationError rather
# than left to fail inside the transaction as a DoltError.
TITLE_CHARS = 1000
TAG_CHARS = 200
NAME_CHARS = 200
PATH_CHARS = 1000
HOST_CHARS = 255
TEXT_BYTES = 65_535
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")

TABLES = (
    "artifact",
    "code_capture",
    "link",
    "project",
    "project_framing_embedding",
    "project_framing_version",
    "project_path",
    "record",
    "record_chunk_embedding",
    "record_project_event",
    "record_tag",
    "record_work_status",
    "retirement",
    "tag_alias",
    "verified_stamp",
)

SCHEMA: tuple[str, ...] = (
    f"""CREATE TABLE record (
        id         VARCHAR(32) {_TEXT} PRIMARY KEY,
        entry_type {enum_column(ENTRY_TYPES)} NOT NULL,
        title      VARCHAR({TITLE_CHARS}) {_TEXT} NOT NULL,
        body       LONGTEXT {_TEXT} NOT NULL,
        author     VARCHAR(120) {_TEXT} NOT NULL,
        created_at DATETIME(6) NOT NULL,
        value      JSON,
        reason     TEXT {_TEXT},
        FULLTEXT KEY ft_record_title (title),
        FULLTEXT KEY ft_record_body (body)
    )""",
    f"""CREATE TABLE record_tag (
        record_id VARCHAR(32) {_TEXT} NOT NULL,
        tag       VARCHAR({TAG_CHARS}) {_TEXT} NOT NULL,
        PRIMARY KEY (record_id, tag)
    )""",
    f"""CREATE TABLE record_work_status (
        record_id   VARCHAR(32) {_TEXT} NOT NULL,
        sequence    INT NOT NULL,
        work_status {enum_column(WORK_STATUSES)} NOT NULL,
        changed_at  DATETIME(6) NOT NULL,
        author      VARCHAR(120) {_TEXT} NOT NULL,
        reason      TEXT {_TEXT},
        PRIMARY KEY (record_id, sequence)
    )""",
    f"""CREATE TABLE link (
        source_id VARCHAR(32) {_TEXT} NOT NULL,
        relation  {enum_column(RELATIONS)} NOT NULL,
        target_id VARCHAR(32) {_TEXT} NOT NULL,
        created_at DATETIME(6) NOT NULL,
        PRIMARY KEY (source_id, relation, target_id)
    )""",
    f"""CREATE TABLE artifact (
        record_id VARCHAR(32) {_TEXT} NOT NULL,
        position  INT NOT NULL,
        location  TEXT {_TEXT} NOT NULL,
        digest    VARCHAR(80) {_TEXT},
        notes     TEXT {_TEXT} NOT NULL,
        PRIMARY KEY (record_id, position)
    )""",
    f"""CREATE TABLE code_capture (
        record_id    VARCHAR(32) {_TEXT} NOT NULL,
        repo_path    VARCHAR({PATH_CHARS}) {_TEXT} NOT NULL,
        head_sha     VARCHAR(64) {_TEXT},
        captured_sha VARCHAR(64) {_TEXT} NOT NULL,
        dirty        BOOLEAN NOT NULL,
        PRIMARY KEY (record_id, repo_path)
    )""",
    f"""CREATE TABLE verified_stamp (
        record_id   VARCHAR(32) {_TEXT} NOT NULL,
        verified_at DATE NOT NULL,
        environment VARCHAR({NAME_CHARS}) {_TEXT} NOT NULL,
        PRIMARY KEY (record_id, verified_at, environment)
    )""",
    f"""CREATE TABLE retirement (
        record_id  VARCHAR(32) {_TEXT} PRIMARY KEY,
        retired_at DATETIME(6) NOT NULL,
        reason     TEXT {_TEXT} NOT NULL
    )""",
    f"""CREATE TABLE project (
        id              VARCHAR(32) {_TEXT} PRIMARY KEY,
        name            VARCHAR({NAME_CHARS}) {_TEXT} NOT NULL UNIQUE,
        created_at      DATETIME(6) NOT NULL,
        created_by      VARCHAR(120) {_TEXT} NOT NULL,
        created_on_host VARCHAR({HOST_CHARS}) {_TEXT} NOT NULL
    )""",
    f"""CREATE TABLE project_path (
        project_id          VARCHAR(32) {_TEXT} NOT NULL,
        path                VARCHAR({PATH_CHARS}) COLLATE utf8mb4_0900_bin NOT NULL,
        label               TEXT {_TEXT} NOT NULL,
        access_instructions TEXT {_TEXT} NOT NULL,
        host                VARCHAR({HOST_CHARS}) {_TEXT} NOT NULL,
        PRIMARY KEY (project_id, path)
    )""",
    f"""CREATE TABLE project_framing_version (
        project_id VARCHAR(32) {_TEXT} NOT NULL,
        version    INT NOT NULL,
        title      VARCHAR({TITLE_CHARS}) {_TEXT} NOT NULL,
        body       LONGTEXT {_TEXT} NOT NULL,
        reason     TEXT {_TEXT},
        author     VARCHAR(120) {_TEXT} NOT NULL,
        created_at DATETIME(6) NOT NULL,
        PRIMARY KEY (project_id, version),
        FULLTEXT KEY ft_project_framing_title (title),
        FULLTEXT KEY ft_project_framing_body (body)
    )""",
    f"""CREATE TABLE record_project_event (
        record_id VARCHAR(32) {_TEXT} NOT NULL,
        project_id VARCHAR(32) {_TEXT} NOT NULL,
        sequence INT NOT NULL,
        action {enum_column(PROJECT_ACTIONS)} NOT NULL,
        reason TEXT {_TEXT},
        author VARCHAR(120) {_TEXT} NOT NULL,
        created_at DATETIME(6) NOT NULL,
        PRIMARY KEY (record_id, project_id, sequence)
    )""",
    f"""CREATE TABLE record_chunk_embedding (
        record_id   VARCHAR(32) {_TEXT} NOT NULL,
        chunk_index INT NOT NULL,
        model_id    VARCHAR(120) {_TEXT} NOT NULL,
        vec         VECTOR({EMBEDDING_DIMENSIONS}) NOT NULL,
        PRIMARY KEY (record_id, model_id, chunk_index)
    )""",
    f"""CREATE TABLE project_framing_embedding (
        project_id  VARCHAR(32) {_TEXT} NOT NULL,
        version     INT NOT NULL,
        chunk_index INT NOT NULL,
        model_id    VARCHAR(120) {_TEXT} NOT NULL,
        vec         VECTOR({EMBEDDING_DIMENSIONS}) NOT NULL,
        PRIMARY KEY (project_id, version, model_id, chunk_index)
    )""",
    f"""CREATE TABLE tag_alias (
        tag       VARCHAR({TAG_CHARS}) {_TEXT} PRIMARY KEY,
        canonical VARCHAR({TAG_CHARS}) {_TEXT} NOT NULL
    )""",
)

_READ_ONLY_FIRST_KEYWORDS = frozenset({"select", "show", "describe", "desc", "explain", "with"})
# SHOW and DESCRIBE cannot modify anything, whatever words follow — and
# `SHOW CREATE TABLE record` legitimately contains a write keyword, so the denylist
# below must not be applied to them. A second statement smuggled after one is still
# refused by the semicolon rule.
_NON_MODIFYING_FIRST_KEYWORDS = frozenset({"show", "describe", "desc"})
_WRITE_KEYWORDS = frozenset(
    {
        "insert",
        "update",
        "delete",
        "replace",
        "drop",
        "create",
        "alter",
        "truncate",
        "rename",
        "call",
        "grant",
        "revoke",
        "set",
        "commit",
        "rollback",
        "start",
        "begin",
        "load",
        "lock",
        "unlock",
        "use",
        "into",
    }
)


def open_store(db_path: Path | None = None) -> Store:
    """Open the Dolt database, creating it and its schema if absent.

    `None` resolves to `$TRAILS_DB`, then `~/.agent-trails/db`. There is no migration: a
    database is created with the current schema and otherwise opened as it is.

    Raises:
        StoreUnavailableError: the `dolt` binary is missing or the database cannot be
            opened.
        DoltError: creating the schema failed.
    """
    resolved = resolve_db_path(db_path)
    dolt.init_database(resolved)
    store = Store(db_path=resolved)
    marker = resolved / SCHEMA_MARKER
    if not marker.exists():
        # The schema is keyed on the record table, not on `.dolt`: a process that dies between
        # `dolt init` and the schema script leaves a database with no tables, which a check of
        # `.dolt` alone would open as complete forever. The marker saves the check on every
        # later open. Checked and created under the exclusive lock, so two first opens do not
        # both run the CREATE TABLEs.
        with dolt.exclusive(resolved):
            if not dolt.read_rows(resolved, "SHOW TABLES LIKE 'record'"):
                dolt.run_script(resolved, [*SCHEMA, "CALL DOLT_COMMIT('-A','-m','init schema')"])
        marker.touch()
    return store


def resolve_db_path(db_path: Path | None) -> Path:
    """The database path a call will use, without opening anything."""
    if db_path is not None:
        return db_path.expanduser()
    from_env = os.environ.get(DEFAULT_DB_ENV)
    if from_env:
        return Path(from_env).expanduser()
    return DEFAULT_DB_PATH


@dataclass(frozen=True)
class Store:
    """A handle on the database. Cheap to construct; holds no connection."""

    db_path: Path

    # ------------------------------------------------------------------ reads

    def get_records(self, record_ids: Sequence[str]) -> dict[str, Record]:
        """Assemble many records in a fixed number of dolt invocations, independent of how
        many ids are passed (one query per table, each with an IN list). This is the only
        function that builds a Record from rows; get_record delegates to it. Unknown ids are
        absent from the result."""
        ids = list(dict.fromkeys(record_ids))
        if not ids:
            return {}
        within = _placeholders(len(ids))
        statements = [
            _bind(
                "SELECT id, entry_type, title, body, author, created_at, value, reason "
                f"FROM record WHERE id IN ({within})",
                ids,
            ),
            _bind(f"SELECT record_id, tag FROM record_tag WHERE record_id IN ({within})", ids),
            _bind(
                "SELECT record_id, position, location, digest, notes FROM artifact "
                f"WHERE record_id IN ({within}) ORDER BY record_id, position",
                ids,
            ),
            _bind(
                "SELECT record_id, repo_path, head_sha, captured_sha, dirty FROM code_capture "
                f"WHERE record_id IN ({within})",
                ids,
            ),
            _bind(
                "SELECT source_id, relation, target_id, created_at FROM link "
                f"WHERE source_id IN ({within})",
                ids,
            ),
            _bind(
                "SELECT source_id, target_id, created_at FROM link "
                f"WHERE relation = 'supersedes' AND target_id IN ({within})",
                ids,
            ),
            _bind(f"SELECT record_id FROM retirement WHERE record_id IN ({within})", ids),
            _bind(
                "SELECT record_id, verified_at, environment FROM verified_stamp "
                f"WHERE record_id IN ({within})",
                ids,
            ),
            _bind(
                "SELECT record_id, sequence, work_status, changed_at, author, reason "
                f"FROM record_work_status WHERE record_id IN ({within}) "
                "ORDER BY record_id, sequence",
                ids,
            ),
            _bind(
                "SELECT e.record_id, p.id AS project_id, p.name AS project_name "
                "FROM record_project_event e JOIN project p ON p.id = e.project_id "
                f"WHERE e.record_id IN ({within}) AND e.action = 'add' AND NOT EXISTS ("
                "SELECT 1 FROM record_project_event newer WHERE newer.record_id = e.record_id "
                "AND newer.project_id = e.project_id AND newer.sequence > e.sequence) "
                "ORDER BY e.record_id, p.name, p.id",
                ids,
            ),
        ]
        (
            records,
            tags,
            artifacts,
            code,
            links,
            incoming,
            retired,
            stamps,
            work_statuses,
            memberships,
        ) = dolt.read_result_sets(self.db_path, statements)

        tags_by: dict[str, list[str]] = {}
        for row in tags:
            tags_by.setdefault(str(row["record_id"]), []).append(str(row["tag"]))
        artifacts_by: dict[str, list[ArtifactRef]] = {}
        for row in artifacts:
            artifacts_by.setdefault(str(row["record_id"]), []).append(
                ArtifactRef(
                    location=str(row["location"]),
                    digest=_optional_str(row.get("digest")),
                    notes=str(row.get("notes", "")),
                )
            )
        code_by: dict[str, list[CodeCapture]] = {}
        for row in sorted(code, key=lambda r: str(r["repo_path"])):
            code_by.setdefault(str(row["record_id"]), []).append(
                CodeCapture(
                    repo_path=str(row["repo_path"]),
                    head_sha=_optional_str(row.get("head_sha")),
                    captured_sha=str(row["captured_sha"]),
                    dirty=_as_bool(row["dirty"]),
                )
            )
        links_by: dict[str, list[tuple[datetime, Link]]] = {}
        for row in links:
            links_by.setdefault(str(row["source_id"]), []).append(
                (
                    _as_datetime(row["created_at"]),
                    Link(relation=_as_relation(row["relation"]), target_id=str(row["target_id"])),
                )
            )
        superseded_by: dict[str, tuple[datetime, str]] = {}
        for row in incoming:
            target, moment = str(row["target_id"]), _as_datetime(row["created_at"])
            candidate = (moment, str(row["source_id"]))
            if target not in superseded_by or candidate > superseded_by[target]:
                superseded_by[target] = candidate
        retired_ids = {str(row["record_id"]) for row in retired}
        newest_stamp: dict[str, tuple[date, str]] = {}
        for row in stamps:
            record_id = str(row["record_id"])
            stamp = (_as_date(row["verified_at"]), str(row["environment"]))
            if record_id not in newest_stamp or stamp > newest_stamp[record_id]:
                newest_stamp[record_id] = stamp
        status_by: dict[str, list[WorkStatusEvent]] = {}
        for row in work_statuses:
            record_id = str(row["record_id"])
            status_by.setdefault(record_id, []).append(
                WorkStatusEvent(
                    sequence=int(row["sequence"]),
                    work_status=_as_work_status(row["work_status"]),
                    changed_at=_as_datetime(row["changed_at"]),
                    author=str(row["author"]),
                    reason=_optional_str(row.get("reason")),
                )
            )

        membership_by: dict[str, list[ProjectRef]] = {}
        for row in memberships:
            membership_by.setdefault(str(row["record_id"]), []).append(
                ProjectRef(id=str(row["project_id"]), name=str(row["project_name"]))
            )

        assembled: dict[str, Record] = {}
        for row in records:
            record_id = str(row["id"])
            newest = superseded_by.get(record_id)
            history = status_by[record_id]
            assembled[record_id] = Record(
                id=record_id,
                entry_type=_as_entry_type(row["entry_type"]),
                work_status=history[-1].work_status,
                work_status_history=history,
                title=str(row["title"]),
                body=str(row.get("body", "")),
                tags=sorted(tags_by.get(record_id, [])),
                projects=membership_by.get(record_id, []),
                author=str(row["author"]),
                created_at=_as_datetime(row["created_at"]),
                value=_as_value(row.get("value")),
                reason=_optional_str(row.get("reason")),
                artifacts=artifacts_by.get(record_id, []),
                code=code_by.get(record_id, []),
                links=_ordered_links(links_by.get(record_id, [])),
                superseded_by=newest[1] if newest is not None else None,
                retired=record_id in retired_ids,
                last_verified=newest_stamp.get(record_id),
            )
        return assembled

    def get_record(self, record_id: str) -> Record:
        """Return one record with its tags, artifacts, code, outgoing links, superseded_by,
        retired and last_verified.

        Raises:
            RecordNotFoundError: no record has this id.
        """
        found = self.get_records([record_id]).get(record_id)
        if found is None:
            raise RecordNotFoundError(f"no record has id {record_id!r}")
        return found

    def get_project(self, value: str) -> Project | None:
        """The registered project with this exact id or database-equal name, or None."""
        found = self._projects(value)
        return found[0] if found else None

    def list_projects(self) -> list[Project]:
        """Every registered project with paths and current framing, by name then id."""
        return self._projects(None)

    def resolve_project(self, value: str) -> ProjectRef:
        """Resolve one stable project id or exact unique name."""
        return self.resolve_project_values([value])[0]

    def resolve_project_values(self, values: Sequence[str]) -> list[ProjectRef]:
        """Resolve ids or names in one read, preserving one result per input."""
        requested = list(values)
        if not requested:
            return []
        result_sets = dolt.read_result_sets(
            self.db_path,
            [
                _bind("SELECT id, name FROM project WHERE id = ? OR name = ?", [value, value])
                for value in requested
            ],
        )
        refs: list[ProjectRef] = []
        known_rows: list[dict[str, Any]] | None = None
        for value, rows in zip(requested, result_sets, strict=True):
            if not rows:
                if known_rows is None:
                    known_rows = self._read("SELECT id, name FROM project ORDER BY name, id")
                suggestions = _project_suggestions(value, known_rows)
                close = (
                    "; close registered projects: "
                    + ", ".join(f"{item.id} ({item.name})" for item in suggestions)
                    if suggestions
                    else ""
                )
                raise ValidationError(
                    f"no registered project has id or name {value!r}{close}; use a listed "
                    f"stable id or run register_project(name={value!r}, paths=[...])"
                )
            refs.append(ProjectRef(id=str(rows[0]["id"]), name=str(rows[0]["name"])))
        return refs

    def resolve_projects(self, values: Sequence[str]) -> list[ProjectRef]:
        """Resolve ids or names and deduplicate by stable id, preserving caller order."""
        refs: list[ProjectRef] = []
        seen: set[str] = set()
        for ref in self.resolve_project_values(values):
            if ref.id not in seen:
                seen.add(ref.id)
                refs.append(ref)
        return refs

    def _projects(self, value: str | None) -> list[Project]:
        """Projects, paths, and current framing in one Dolt invocation."""
        if value is None:
            project_where = ""
            path_where = ""
            framing_where = ""
        else:
            project_where = _bind(" WHERE id = ? OR name = ?", [value, value])
            path_where = _bind(
                " WHERE project_id IN (SELECT id FROM project WHERE id = ? OR name = ?)",
                [value, value],
            )
            framing_where = _bind(
                " AND f.project_id IN (SELECT id FROM project WHERE id = ? OR name = ?)",
                [value, value],
            )
        projects, paths, framings = dolt.read_result_sets(
            self.db_path,
            [
                "SELECT id, name, created_at, created_by, created_on_host FROM project"
                f"{project_where} ORDER BY name, id",
                "SELECT project_id, path, label, access_instructions, host FROM project_path"
                f"{path_where} ORDER BY project_id, path",
                "SELECT f.project_id, f.version, f.title, f.body, f.reason, f.author, "
                "f.created_at FROM project_framing_version f WHERE NOT EXISTS ("
                "SELECT 1 FROM project_framing_version newer WHERE newer.project_id = "
                "f.project_id AND newer.version > f.version)"
                f"{framing_where} ORDER BY f.project_id",
            ],
        )
        by_project: dict[str, list[ProjectPath]] = {}
        for row in paths:
            by_project.setdefault(str(row["project_id"]), []).append(
                ProjectPath(
                    path=str(row["path"]),
                    label=str(row.get("label", "")),
                    access_instructions=str(row.get("access_instructions", "")),
                    host=str(row.get("host", "")),
                )
            )
        framing_by = {str(row["project_id"]): _project_framing(row) for row in framings}
        return [
            Project(
                id=str(row["id"]),
                name=str(row["name"]),
                created_at=_as_datetime(row["created_at"]),
                created_by=str(row["created_by"]),
                created_on_host=str(row["created_on_host"]),
                paths=by_project.get(str(row["id"]), []),
                current_framing=framing_by.get(str(row["id"])),
            )
            for row in projects
        ]

    def project_framing_history(self, project_id: str) -> list[ProjectFramingVersion]:
        """Every framing version newest first for one exact project id."""
        rows = self._read(
            _bind(
                "SELECT project_id, version, title, body, reason, author, created_at "
                "FROM project_framing_version WHERE project_id = ? "
                "ORDER BY version DESC",
                [project_id],
            )
        )
        return [_project_framing(row) for row in rows]

    def project_membership_history(
        self, record_ids: Sequence[str]
    ) -> dict[str, list[ProjectMembershipEvent]]:
        """Membership events newest time first, then project id and sequence."""
        ids = list(dict.fromkeys(record_ids))
        if not ids:
            return {}
        within = _placeholders(len(ids))
        rows = self._read(
            _bind(
                "SELECT e.record_id, e.project_id, p.name AS project_name, e.sequence, "
                "e.action, e.reason, e.author, e.created_at FROM record_project_event e "
                "JOIN project p ON p.id = e.project_id "
                f"WHERE e.record_id IN ({within}) ORDER BY e.created_at DESC, "
                "e.project_id ASC, e.sequence DESC",
                ids,
            )
        )
        grouped: dict[str, list[ProjectMembershipEvent]] = {}
        for row in rows:
            grouped.setdefault(str(row["record_id"]), []).append(
                ProjectMembershipEvent(
                    record_id=str(row["record_id"]),
                    project=ProjectRef(id=str(row["project_id"]), name=str(row["project_name"])),
                    sequence=int(row["sequence"]),
                    action=_as_project_action(row["action"]),
                    reason=_optional_str(row.get("reason")),
                    author=str(row["author"]),
                    created_at=_as_datetime(row["created_at"]),
                )
            )
        return grouped

    def alias_map(self) -> dict[str, str]:
        """tag -> canonical for every tag_alias row; consumed by retrieval.tag_equivalence.

        A chain resolves to its end, and a CYCLE RESOLVES TO ITS LEXICOGRAPHICALLY SMALLEST
        MEMBER, so every member of a cycle resolves to the same tag rather than each to the
        next one around.
        """
        edges = {
            str(row["tag"]): str(row["canonical"])
            for row in self._read("SELECT tag, canonical FROM tag_alias")
        }

        def canonical(tag: str) -> str:
            path = [tag]
            current = tag
            while current in edges:
                nxt = edges[current]
                if nxt in path:
                    return min(path[path.index(nxt) :])
                path.append(nxt)
                current = nxt
            return current

        return {tag: canonical(tag) for tag in edges}

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[dict[str, Any]]:
        """Execute one READ-ONLY statement and return rows as dicts.

        `?` placeholders are substituted positionally. A NULL column is OMITTED from a
        row dict rather than present as None, so read nullable columns with .get.

        Raises:
            ValueError: the statement is not read-only, contains a second statement,
                contains a write keyword anywhere including inside a string literal,
                ends inside an unterminated quoted section, or the placeholder count does
                not match `params`.
            DoltError: the read failed.
        """
        _assert_read_only(sql)
        return self._read(_bind(sql, params))

    # ----------------------------------------------------------------- writes

    def new_record_id(self, entry_type: EntryType) -> str:
        """Mint an unused type-prefixed base36 id without writing anything.

        A caller can name capture refs after the record before writing it. Ten five-character
        candidates are tried before ten six-character candidates.
        """
        return self._unused_record_id(entry_type)

    def _unused_record_id(self, entry_type: EntryType) -> str:
        """Return the first database-unused candidate, or fail after all bounded attempts."""
        for candidate in _id_candidates(entry_type):
            if not self._read(_bind("SELECT id FROM record WHERE id = ?", [candidate])):
                return candidate
        raise DoltError(
            f"failed to generate an unused {entry_type} id after "
            f"{len(ID_LENGTHS) * ID_ATTEMPTS_PER_LENGTH} candidates"
        )

    def _unused_project_id(self) -> str:
        """Return the first unused bounded `prj-` candidate."""
        for candidate in _project_id_candidates():
            if not self._read(_bind("SELECT id FROM project WHERE id = ?", [candidate])):
                return candidate
        raise DoltError(
            "failed to generate an unused project id after "
            f"{len(ID_LENGTHS) * ID_ATTEMPTS_PER_LENGTH} candidates"
        )

    def validate_record_draft(
        self, draft: RecordDraft
    ) -> tuple[str, list[str], str | None, str | None]:
        """Validate fields that need no database read, without writing anything."""
        return _validated(draft)

    def write_record(self, draft: RecordDraft) -> WriteOutcome:
        """Write one record, its tags, artifacts, code captures and links in one transaction.

        When draft.supersedes is set, also writes a `supersedes` link from the new record
        to that id. Embedding is not done here; the publish operation does it.

        Uses draft.id when given. Runs scan_for_secrets and returns its findings; a finding
        does not stop the write.

        Raises:
            ValidationError: empty title; supersedes set without reason; supersedes or a
                link target names no existing record; a tag that is empty after strip;
                draft.id malformed or already used.
        """
        with dolt.exclusive(self.db_path):
            return self._write_record_locked(draft)

    def _write_record_locked(self, draft: RecordDraft) -> WriteOutcome:
        """Implementation of write_record while the caller holds the exclusive store lock."""
        title, tags, reason, value_text = self.validate_record_draft(draft)

        links = list(dict.fromkeys((item.relation, item.target_id) for item in draft.links))
        if draft.supersedes is not None:
            links.append(("supersedes", draft.supersedes))
        targets = list(dict.fromkeys(target for _relation, target in links))
        existing = self._existing_ids([*targets, *([draft.id] if draft.id else [])])
        if draft.id is not None and draft.id in existing:
            raise ValidationError(
                f"id {draft.id!r} is already used: a record is never overwritten; publish a "
                "superseding record instead"
            )
        missing = [target for target in targets if target not in existing]
        if missing:
            raise ValidationError(f"no existing record has id {', '.join(missing)}")
        if draft.supersedes is not None:
            current = self.superseding_tip(draft.supersedes)
            if current is not None:
                raise ValidationError(
                    f"{draft.supersedes} is already superseded; current version is {current}; "
                    f"supersede {current} instead"
                )

        if draft.projects is None:
            project_refs = (
                list(self.get_record(draft.supersedes).projects)
                if draft.supersedes is not None
                else []
            )
        else:
            project_refs = self.resolve_projects(draft.projects)
        project_refs = sorted(project_refs, key=lambda item: (item.name, item.id))

        findings = scan_for_secrets(
            {
                "title": title,
                "body": draft.body,
                **({"value": value_text} if value_text is not None else {}),
                **{
                    f"artifact[{n}].{part}": text
                    for n, artifact in enumerate(draft.artifacts)
                    for part, text in (("location", artifact.location), ("notes", artifact.notes))
                },
            }
        )

        # The exclusive lock held by write_record makes this absence check and the insert one
        # serialized decision. This extra read prevents an older equal-timestamp collision from
        # being mistaken for a command that landed before reporting failure.
        record_id = draft.id or self._unused_record_id(draft.entry_type)
        created_at = _now()
        author = current_author()

        def script(record_id: str) -> list[str]:
            return [
                "START TRANSACTION",
                _insert(
                    "record",
                    {
                        "id": record_id,
                        "entry_type": draft.entry_type,
                        "title": title,
                        "body": draft.body,
                        "author": author,
                        "created_at": created_at,
                        "value": value_text,
                        "reason": reason,
                    },
                ),
                _insert(
                    "record_work_status",
                    {
                        "record_id": record_id,
                        "sequence": 0,
                        "work_status": draft.work_status,
                        "changed_at": created_at,
                        "author": author,
                        "reason": None,
                    },
                ),
                *(
                    _insert(
                        "record_project_event",
                        {
                            "record_id": record_id,
                            "project_id": project.id,
                            "sequence": 0,
                            "action": "add",
                            "reason": None,
                            "author": author,
                            "created_at": created_at,
                        },
                    )
                    for project in project_refs
                ),
                *(_insert("record_tag", {"record_id": record_id, "tag": tag}) for tag in tags),
                *(
                    _insert(
                        "artifact",
                        {
                            "record_id": record_id,
                            "position": position,
                            "location": artifact.location,
                            "digest": artifact.digest,
                            "notes": artifact.notes,
                        },
                    )
                    for position, artifact in enumerate(draft.artifacts)
                ),
                *(
                    _insert(
                        "code_capture",
                        {
                            "record_id": record_id,
                            "repo_path": capture.repo_path,
                            "head_sha": capture.head_sha,
                            "captured_sha": capture.captured_sha,
                            "dirty": int(capture.dirty),
                        },
                    )
                    for capture in draft.code
                ),
                *(
                    _insert(
                        "link",
                        {
                            "source_id": record_id,
                            "relation": relation,
                            "target_id": target,
                            "created_at": created_at,
                        },
                    )
                    for relation, target in links
                ),
                f"CALL DOLT_COMMIT('-A','-m',{dolt.sql_literal(f'publish {record_id}')})",
            ]

        try:
            self._execute_script(script(record_id))
        except DoltError:
            outcome = self._classify_failed_write(record_id, draft.id, created_at, tags)
            if outcome != "landed":
                raise

        status_event = WorkStatusEvent(
            sequence=0,
            work_status=draft.work_status,
            changed_at=created_at,
            author=author,
            reason=None,
        )
        record = Record(
            id=record_id,
            entry_type=draft.entry_type,
            work_status=draft.work_status,
            work_status_history=[status_event],
            title=title,
            body=draft.body,
            tags=list(tags),
            projects=project_refs,
            author=author,
            created_at=created_at,
            value=draft.value,
            reason=reason,
            artifacts=list(draft.artifacts),
            code=_by_repo(draft.code),
            links=_ordered_links([(created_at, Link(relation=r, target_id=t)) for r, t in links]),
            superseded_by=None,
            retired=False,
            last_verified=None,
        )
        return WriteOutcome(record=record, secret_findings=findings)

    def write_project(  # noqa: PLR0912, PLR0915, PLR0917 — one atomic registration boundary
        self,
        name: str,
        host: str,
        paths: Sequence[ProjectPath],
        remove_paths: Sequence[str],
        framing: ProjectFramingDraft | None = None,
        base_version: int | None = None,
        reason: str | None = None,
        intent: Literal["upsert", "register", "update"] = "upsert",
    ) -> ProjectWriteOutcome:
        """Atomically create or update identity, paths, and optional framing.

        `intent="register"` creates or returns without mutating an existing project;
        `intent="update"` requires an existing project; internal `upsert` preserves the Store
        helper used by tests. An unchanged framing writes no version. A recognized stale-base
        retry returns before path mutations. Every read-decide-write path holds the exclusive lock.
        """
        name = _validated_project_name(name)
        _require_length("project host", host, HOST_CHARS)
        for item in paths:
            if not item.path.strip():
                raise ValidationError("a project path is empty")
            _require_length("project path", item.path, PATH_CHARS)
            _require_length("project path host", item.host, HOST_CHARS)
            _require_text_bytes(f"label of {item.path}", item.label)
            _require_text_bytes(f"access_instructions of {item.path}", item.access_instructions)
        both = sorted({item.path for item in paths} & set(remove_paths))
        if both:
            raise ValidationError(f"{', '.join(both)} is both in paths and in remove_paths")
        if framing is None and (base_version is not None or reason is not None):
            raise ValidationError("base_version and reason require framing")
        framing_title, framing_body = _validated_framing(framing)

        with dolt.exclusive(self.db_path):
            existing = self.get_project(name)
            created = existing is None
            project_id = existing.id if existing is not None else self._unused_project_id()
            current = existing.current_framing if existing is not None else None
            written_framing: ProjectFramingVersion | None = None
            framing_created = False
            normal_reason = reason.strip() if reason is not None else None

            if intent == "update" and existing is None:
                raise ValidationError(
                    f"project {name!r} is not registered; run register_project(name={name!r})"
                )
            if intent == "register" and existing is not None:
                if framing is not None and (
                    current is None or not _same_framing(current, framing_title, framing_body)
                ):
                    base = current.version if current is not None else 0
                    raise ValidationError(
                        f"project {name!r} is already registered as {project_id}; use "
                        f"update_project(project={project_id!r}, framing=..., "
                        f"base_version={base}, reason=...)"
                    )
                current_paths = {item.path: item for item in existing.paths}
                given_paths = {item.path: item for item in paths}
                if paths and given_paths != current_paths:
                    rendered_paths = [
                        {
                            key: value
                            for key, value in (
                                ("path", item.path),
                                ("label", item.label),
                                ("access_instructions", item.access_instructions),
                            )
                            if value
                        }
                        for item in given_paths.values()
                    ]
                    raise ValidationError(
                        f"project {name!r} is already registered as {project_id}; use "
                        f"update_project(project={project_id!r}, paths={rendered_paths!r})"
                    )
                return ProjectWriteOutcome(
                    project=existing,
                    created=False,
                    changed=False,
                    framing=current if framing is not None else None,
                    framing_created=False,
                    current_framing_version=(current.version if current is not None else None),
                    retry=True,
                )

            if framing is not None:
                write_framing = True
                if current is None:
                    if intent == "update":
                        if base_version != 0:
                            raise ValidationError(
                                "adding the first framing needs base_version=0 and a reason"
                            )
                        if not normal_reason:
                            raise ValidationError(
                                "adding the first framing needs base_version=0 and a nonempty reason"
                            )
                        _require_text_bytes("framing reason", normal_reason)
                    else:
                        if base_version is not None or normal_reason:
                            raise ValidationError(
                                "the first framing version needs neither base_version nor reason"
                            )
                        normal_reason = None
                    version = 1
                else:
                    same_current = _same_framing(current, framing_title, framing_body)
                    if base_version is None:
                        if same_current:
                            if intent == "upsert":
                                if existing is None:  # pragma: no cover
                                    raise DoltError("framing exists without project identity")
                                return ProjectWriteOutcome(
                                    project=existing,
                                    created=False,
                                    changed=False,
                                    framing=current,
                                    framing_created=False,
                                    current_framing_version=current.version,
                                    retry=True,
                                )
                            write_framing = False
                        else:
                            raise ValidationError(
                                f"updating framing needs base_version={current.version} and a reason"
                            )
                    elif base_version != current.version:
                        landed_rows = self._read(
                            _bind(
                                "SELECT project_id, version, title, body, reason, author, "
                                "created_at FROM project_framing_version WHERE project_id = ? "
                                "AND version = ?",
                                [project_id, base_version + 1],
                            )
                        )
                        landed = _project_framing(landed_rows[0]) if landed_rows else None
                        if landed is not None and _same_framing(
                            landed, framing_title, framing_body
                        ):
                            if existing is None:  # pragma: no cover
                                raise DoltError("framing exists without project identity")
                            return ProjectWriteOutcome(
                                project=existing,
                                created=False,
                                changed=False,
                                framing=landed,
                                framing_created=False,
                                current_framing_version=current.version,
                                retry=True,
                            )
                        raise ValidationError(
                            f"framing base_version={base_version} is stale; current version is "
                            f"{current.version}; retry with base_version={current.version} and "
                            "the current framing edited from that version"
                        )
                    elif same_current:
                        write_framing = False
                    else:
                        if not normal_reason:
                            raise ValidationError("updating framing needs a nonempty reason")
                        version = current.version + 1
                        _require_text_bytes("framing reason", normal_reason)
                if write_framing:
                    written_framing = ProjectFramingVersion(
                        project_id=project_id,
                        version=version,
                        title=framing_title,
                        body=framing_body,
                        reason=normal_reason,
                        author=current_author(),
                        created_at=_now(),
                    )
                    framing_created = True

            current_paths = {item.path: item for item in existing.paths} if existing else {}
            wanted = {item.path: item for item in paths}
            changed = [item for path, item in wanted.items() if current_paths.get(path) != item]
            removed = [path for path in dict.fromkeys(remove_paths) if path in current_paths]
            statements: list[str] = []
            if existing is None:
                statements.append(
                    _insert(
                        "project",
                        {
                            "id": project_id,
                            "name": name,
                            "created_at": _now(),
                            "created_by": current_author(),
                            "created_on_host": host,
                        },
                    )
                )
            statements += [
                _bind(
                    "DELETE FROM project_path WHERE project_id = ? AND path = ?",
                    [project_id, path],
                )
                for path in [*(item.path for item in changed), *removed]
                if path in current_paths
            ]
            statements += [
                _insert(
                    "project_path",
                    {
                        "project_id": project_id,
                        "path": item.path,
                        "label": item.label,
                        "access_instructions": item.access_instructions,
                        "host": item.host,
                    },
                )
                for item in changed
            ]
            if written_framing is not None:
                statements.append(
                    _insert(
                        "project_framing_version",
                        {
                            "project_id": project_id,
                            "version": written_framing.version,
                            "title": written_framing.title,
                            "body": written_framing.body,
                            "reason": written_framing.reason,
                            "author": written_framing.author,
                            "created_at": written_framing.created_at,
                        },
                    )
                )
            if statements:
                self._execute_script(
                    [
                        "START TRANSACTION",
                        *statements,
                        f"CALL DOLT_COMMIT('-A','-m',{dolt.sql_literal(f'project {project_id}')})",
                    ]
                )
            stored = self.get_project(project_id)
            if stored is None:  # pragma: no cover - the transaction above wrote it
                raise DoltError(f"project {project_id!r} was written but cannot be read back")
            return ProjectWriteOutcome(
                project=stored,
                created=created,
                changed=bool(statements),
                framing=written_framing,
                framing_created=framing_created,
                current_framing_version=(
                    stored.current_framing.version if stored.current_framing is not None else None
                ),
                retry=False,
            )

    def set_projects(
        self,
        record_id: str,
        add: Sequence[str],
        remove: Sequence[str],
        reason: str,
    ) -> SetProjectsOutcome:
        """Append effective membership changes in one transaction and return current state."""
        if not add and not remove:
            raise ValidationError("set_projects needs at least one project to add or remove")
        normal_reason = reason.strip()
        if not normal_reason:
            raise ValidationError("changing project membership needs a reason")
        _require_text_bytes("project membership reason", normal_reason)
        with dolt.exclusive(self.db_path):
            resolved = self.resolve_project_values([*add, *remove])
            add_refs = list({ref.id: ref for ref in resolved[: len(add)]}.values())
            remove_refs = list({ref.id: ref for ref in resolved[len(add) :]}.values())
            refs = [*add_refs, *remove_refs]
            by_id = {ref.id: ref for ref in refs}
            overlap = sorted({ref.id for ref in add_refs} & {ref.id for ref in remove_refs})
            if overlap:
                names = ", ".join(by_id[item].name for item in overlap)
                raise ValidationError(f"{names} is present in both add and remove")
            record_rows, event_rows = dolt.read_result_sets(
                self.db_path,
                [
                    _bind("SELECT id FROM record WHERE id = ?", [record_id]),
                    _bind(
                        "SELECT e.project_id, p.name AS project_name, e.sequence, e.action "
                        "FROM record_project_event e JOIN project p ON p.id = e.project_id "
                        "WHERE e.record_id = ? AND NOT EXISTS (SELECT 1 FROM "
                        "record_project_event newer WHERE newer.record_id = e.record_id AND "
                        "newer.project_id = e.project_id AND newer.sequence > e.sequence) "
                        "ORDER BY e.project_id",
                        [record_id],
                    ),
                ],
            )
            if not record_rows:
                raise RecordNotFoundError(f"no record has id {record_id!r}")
            latest = {
                str(row["project_id"]): (
                    int(row["sequence"]),
                    str(row["action"]),
                    str(row["project_name"]),
                )
                for row in event_rows
            }
            current_ids = {
                project_id
                for project_id, (_seq, action, _name) in latest.items()
                if action == "add"
            }
            effective_add = [ref for ref in add_refs if ref.id not in current_ids]
            effective_remove = [ref for ref in remove_refs if ref.id in current_ids]
            unchanged_add = [ref for ref in add_refs if ref.id in current_ids]
            unchanged_remove = [ref for ref in remove_refs if ref.id not in current_ids]
            changed_at = _now()
            author = current_author()
            events = [
                (ref, action)
                for action, selected in (("add", effective_add), ("remove", effective_remove))
                for ref in selected
            ]
            if events:
                self._execute_script(
                    [
                        "START TRANSACTION",
                        *(
                            _insert(
                                "record_project_event",
                                {
                                    "record_id": record_id,
                                    "project_id": ref.id,
                                    "sequence": latest.get(ref.id, (-1, "", ""))[0] + 1,
                                    "action": action,
                                    "reason": normal_reason,
                                    "author": author,
                                    "created_at": changed_at,
                                },
                            )
                            for ref, action in events
                        ),
                        f"CALL DOLT_COMMIT('-A','-m',{dolt.sql_literal(f'projects {record_id}')})",
                    ]
                )
            resulting = (current_ids | {ref.id for ref in effective_add}) - {
                ref.id for ref in effective_remove
            }
            known: dict[str, ProjectRef] = {
                project_id: ProjectRef(id=project_id, name=name)
                for project_id, (_seq, _action, name) in latest.items()
            }
            known.update(by_id)
            current_projects: list[ProjectRef] = [known[project_id] for project_id in resulting]
            current_projects.sort(key=_project_ref_key)
            return SetProjectsOutcome(
                record_id=record_id,
                changed=[
                    ProjectMembershipChange(
                        project=ref,
                        action=_as_project_action(action),
                        reason=normal_reason,
                        author=author,
                        created_at=changed_at,
                    )
                    for ref, action in events
                ],
                unchanged_add=unchanged_add,
                unchanged_remove=unchanged_remove,
                projects=current_projects,
            )

    def write_tag_alias(self, tag: str, canonical: str) -> None:
        """Insert a tag_alias row; an existing identical row is a no-op.

        Both tags are normalised as record tags are (strip, lowercase), so an alias names a
        tag a record can carry.

        Raises:
            ValidationError: tag equals canonical; tag already aliased to a different canonical.
        """
        alias, target = tag.strip().lower(), canonical.strip().lower()
        if not alias or not target:
            raise ValidationError("an alias needs two non-empty tags")
        if alias == target:
            raise ValidationError(f"{alias!r} cannot be an alias of itself")
        _require_length("tag", alias, TAG_CHARS)
        _require_length("canonical", target, TAG_CHARS)
        existing = self._read(_bind("SELECT canonical FROM tag_alias WHERE tag = ?", [alias]))
        if existing:
            recorded = str(existing[0]["canonical"])
            if recorded == target:
                return
            raise ValidationError(f"{alias!r} is already an alias of {recorded!r}")
        self._execute_script(
            [
                "START TRANSACTION",
                _insert("tag_alias", {"tag": alias, "canonical": target}),
                f"CALL DOLT_COMMIT('-A','-m',{dolt.sql_literal(f'alias {alias} -> {target}')})",
            ]
        )

    def add_link(self, source_id: str, relation: Relation, target_id: str) -> Link:
        """Append one link. Duplicate (source, relation, target) is a no-op returning the link.

        Raises:
            ValidationError: source equals target, or relation is supersedes.
            RecordNotFoundError: either id names no record.
        """
        if relation not in RELATIONS:
            raise ValidationError(f"relation must be one of {', '.join(RELATIONS)}")
        if relation == "supersedes":
            raise ValidationError(
                "Store.add_link does not write supersedes; write a new record with "
                "supersedes=<id> and a reason"
            )
        if source_id == target_id:
            raise ValidationError("a record cannot link to itself")
        missing = [i for i in (source_id, target_id) if i not in self._existing_ids([i])]
        if missing:
            raise RecordNotFoundError(f"no record has id {', '.join(missing)}")
        result = Link(relation=relation, target_id=target_id)
        if self._read(
            _bind(
                "SELECT source_id FROM link WHERE source_id = ? AND relation = ? AND target_id = ?",
                [source_id, relation, target_id],
            )
        ):
            return result
        self._execute_script(
            [
                "START TRANSACTION",
                _insert(
                    "link",
                    {
                        "source_id": source_id,
                        "relation": relation,
                        "target_id": target_id,
                        "created_at": _now(),
                    },
                ),
                "CALL DOLT_COMMIT('-A','-m',"
                f"{dolt.sql_literal(f'link {source_id} {relation} {target_id}')})",
            ]
        )
        return result

    def retire(self, record_id: str, reason: str) -> None:
        """Append a retirement row. Retiring a retired record is a no-op.

        Raises:
            ValidationError: empty reason.
            RecordNotFoundError: no record has this id.
        """
        reason = reason.strip()
        if not reason:
            raise ValidationError("retire needs a reason: why the record is no longer true")
        _require_text_bytes("reason", reason)
        self._require_record(record_id)
        if self._read(_bind("SELECT record_id FROM retirement WHERE record_id = ?", [record_id])):
            return
        self._execute_script(
            [
                "START TRANSACTION",
                _insert(
                    "retirement",
                    {"record_id": record_id, "retired_at": _now(), "reason": reason},
                ),
                f"CALL DOLT_COMMIT('-A','-m',{dolt.sql_literal(f'retire {record_id}')})",
            ]
        )

    def set_work_status(
        self, record_id: str, work_status: WorkStatus, reason: str
    ) -> tuple[WorkStatusEvent, bool]:
        """Append a work-status transition, or return the current event unchanged.

        A real transition requires a non-empty reason. Any entry type may move in either
        direction. The exclusive lock protects the read of the next sequence and its insert.
        """
        with dolt.exclusive(self.db_path):
            self._require_record(record_id)
            rows = self._read(
                _bind(
                    "SELECT sequence, work_status, changed_at, author, reason "
                    "FROM record_work_status WHERE record_id = ? "
                    "ORDER BY sequence DESC LIMIT 1",
                    [record_id],
                )
            )
            if not rows:
                raise DoltError(f"record {record_id!r} has no initial work status")
            current = WorkStatusEvent(
                sequence=int(rows[0]["sequence"]),
                work_status=_as_work_status(rows[0]["work_status"]),
                changed_at=_as_datetime(rows[0]["changed_at"]),
                author=str(rows[0]["author"]),
                reason=_optional_str(rows[0].get("reason")),
            )
            if current.work_status == work_status:
                return current, False
            normal_reason = reason.strip()
            if not normal_reason:
                raise ValidationError("changing work status needs a reason")
            _require_text_bytes("work status reason", normal_reason)
            changed = WorkStatusEvent(
                sequence=current.sequence + 1,
                work_status=work_status,
                changed_at=_now(),
                author=current_author(),
                reason=normal_reason,
            )
            self._execute_script(
                [
                    "START TRANSACTION",
                    _insert(
                        "record_work_status",
                        {
                            "record_id": record_id,
                            "sequence": changed.sequence,
                            "work_status": changed.work_status,
                            "changed_at": changed.changed_at,
                            "author": changed.author,
                            "reason": changed.reason,
                        },
                    ),
                    "CALL DOLT_COMMIT('-A','-m',"
                    f"{dolt.sql_literal(f'work status {record_id} {work_status}')})",
                ]
            )
            return changed, True

    def stamp_verified(self, record_id: str, environment: str, verified_at: date) -> None:
        """Append a verified_stamp row.

        Stamping the same record, date and environment twice is a no-op, because the row
        already says it.

        Raises:
            ValidationError: environment longer than its column.
            RecordNotFoundError: no record has this id.
        """
        _require_length("environment", environment, NAME_CHARS)
        self._require_record(record_id)
        day = verified_at.isoformat()
        if self._read(
            _bind(
                "SELECT record_id FROM verified_stamp WHERE record_id = ? AND verified_at = ? "
                "AND environment = ?",
                [record_id, day, environment],
            )
        ):
            return
        self._execute_script(
            [
                "START TRANSACTION",
                _insert(
                    "verified_stamp",
                    {"record_id": record_id, "verified_at": day, "environment": environment},
                ),
                f"CALL DOLT_COMMIT('-A','-m',{dolt.sql_literal(f'verified {record_id}')})",
            ]
        )

    def upsert_chunk_embeddings(self, rows: Sequence[tuple[str, int, str, Sequence[float]]]) -> int:
        """Write record chunk vectors in one transaction, returning the row count."""
        if not rows:
            return 0
        for record_id, chunk, _model_id, vector in rows:
            if len(vector) != EMBEDDING_DIMENSIONS:
                raise ValidationError(
                    f"a vector for {record_id} chunk {chunk} has {len(vector)} components; "
                    f"the schema stores {EMBEDDING_DIMENSIONS}"
                )
        values = ", ".join(
            f"({dolt.sql_literal(record_id)}, {chunk}, {dolt.sql_literal(model_id)}, "
            f"STRING_TO_VECTOR({dolt.sql_literal(dolt.vector_literal(vector))}))"
            for record_id, chunk, model_id, vector in rows
        )
        self._execute_script(
            [
                "START TRANSACTION",
                "REPLACE INTO record_chunk_embedding (record_id, chunk_index, model_id, vec) "
                f"VALUES {values}",
                f"CALL DOLT_COMMIT('-A','-m','embed {len(rows)} chunks')",
            ]
        )
        return len(rows)

    def insert_project_framing_embeddings(
        self,
        rows: Sequence[tuple[str, int, int, str, Sequence[float]]],
    ) -> int:
        """Insert framing vectors without deleting or replacing any earlier version rows."""
        if not rows:
            return 0
        for project_id, version, chunk, _model_id, values in rows:
            if len(values) != EMBEDDING_DIMENSIONS:
                raise ValidationError(
                    f"a vector for {project_id} framing {version} chunk {chunk} has "
                    f"{len(values)} components; the schema stores {EMBEDDING_DIMENSIONS}"
                )
        self._execute_script(
            [
                "START TRANSACTION",
                *(
                    _insert_vector(
                        "project_framing_embedding",
                        {
                            "project_id": project_id,
                            "version": version,
                            "chunk_index": chunk,
                            "model_id": model_id,
                        },
                        values,
                    )
                    for project_id, version, chunk, model_id, values in rows
                ),
                f"CALL DOLT_COMMIT('-A','-m','embed {len(rows)} framing chunks')",
            ]
        )
        return len(rows)

    # ---------------------------------------------------------------- internals

    def _classify_failed_write(
        self, record_id: str, pre_minted: str | None, created_at: datetime, tags: Sequence[str]
    ) -> Literal["landed", "collision", "unknown"]:
        """Why a record write raised DoltError, decided by re-reading, never by Dolt's message.

        Runs only on the failure path, so a successful write still costs one dolt process.

        Returns "landed" when the row under `record_id` carries this write's own created_at, so
        the commit went through and the failure came after it; "collision" when a self-minted
        id names an older record; "unknown" when no rule explains the failure.

        Raises:
            ValidationError: `pre_minted` is now used by another record (two writers given one
                id), or two of `tags` are equal under the column's case- and
                accent-insensitive collation, as the database itself compares them.
        """
        stored = self._read(_bind("SELECT created_at FROM record WHERE id = ?", [record_id]))
        if stored:
            if _as_datetime(stored[0]["created_at"]) == created_at.replace(tzinfo=UTC):
                return "landed"
            if pre_minted is not None:
                raise ValidationError(
                    f"id {pre_minted!r} is already used: a record is never overwritten"
                )
            return "collision"
        equal = self._collation_equal_pairs(tags)
        if equal:
            named = ", ".join(f"{a!r} and {b!r}" for a, b in equal)
            raise ValidationError(
                f"tags {named} are one tag to the store, which compares tags ignoring case "
                "and accents; keep one"
            )
        return "unknown"

    def _collation_equal_pairs(self, tags: Sequence[str]) -> list[tuple[str, str]]:
        """Pairs of distinct `tags` the record_tag collation compares as equal, from one read."""
        pairs = [(a, b) for i, a in enumerate(tags) for b in tags[i + 1 :]]
        if not pairs:
            return []
        columns = ", ".join(
            f"(? COLLATE utf8mb4_0900_ai_ci = ? COLLATE utf8mb4_0900_ai_ci) AS p{n}"
            for n in range(len(pairs))
        )
        row = self._read(_bind(f"SELECT {columns}", [value for pair in pairs for value in pair]))[0]
        return [pair for n, pair in enumerate(pairs) if _as_bool(row[f"p{n}"])]

    def _existing_ids(self, record_ids: Sequence[str]) -> set[str]:
        ids = list(dict.fromkeys(record_ids))
        if not ids:
            return set()
        rows = self._read(
            _bind(f"SELECT id FROM record WHERE id IN ({_placeholders(len(ids))})", ids)
        )
        return {str(row["id"]) for row in rows}

    def _require_record(self, record_id: str) -> None:
        if not self._existing_ids([record_id]):
            raise RecordNotFoundError(f"no record has id {record_id!r}")

    def superseding_tip(self, record_id: str) -> str | None:
        """Newest current descendant reached through incoming supersedes links.

        None means nothing supersedes `record_id`. Existing historical forks are followed by
        choosing the newest successor at each step, with id as the equal-time tie break. New
        writes refuse before creating another fork.
        """
        successors: dict[str, list[tuple[datetime, str]]] = {}
        for row in self._read(
            "SELECT source_id, target_id, created_at FROM link "
            "WHERE relation = 'supersedes' ORDER BY created_at, source_id"
        ):
            successors.setdefault(str(row["target_id"]), []).append(
                (_as_datetime(row["created_at"]), str(row["source_id"]))
            )
        current = record_id
        seen = {record_id}
        while choices := successors.get(current):
            _created_at, nxt = max(choices)
            if nxt in seen:
                return current
            seen.add(nxt)
            current = nxt
        return None if current == record_id else current

    def _read(self, statement: str) -> list[dict[str, Any]]:
        return dolt.read_rows(self.db_path, statement)

    def _execute_script(self, statements: Sequence[str]) -> None:
        """Run a write as ONE Dolt transaction, under the write lock `dolt.run_script` holds."""
        dolt.run_script(self.db_path, statements)


def _validated(  # noqa: PLR0912 — one boundary validates the complete record draft
    draft: RecordDraft,
) -> tuple[str, list[str], str | None, str | None]:
    """The checks on a draft that need no read: title, tags, reason, id shape, links,
    artifact digests, duplicate repositories and the value. Returns the normalised title,
    tags and reason, and the value as JSON text.

    Raises:
        ValidationError: the draft is malformed.
    """
    title = draft.title.strip()
    if not title:
        raise ValidationError("title is empty: a record needs a one-line title")
    _require_length("title", title, TITLE_CHARS)
    _require_text_bytes("reason", draft.reason)
    for capture in draft.code:
        _require_length("code repo_path", capture.repo_path, PATH_CHARS)
    for n, artifact in enumerate(draft.artifacts):
        _require_text_bytes(f"artifact[{n}].location", artifact.location)
        _require_text_bytes(f"artifact[{n}].notes", artifact.notes)
    tags = _normalise_tags(draft.tags)
    duplicate_types = sorted(set(tags).intersection(ENTRY_TYPES))
    if duplicate_types:
        raise ValidationError(
            f"{', '.join(duplicate_types)} is an entry type, not a descriptive tag; "
            "pass it as entry_type and remove it from tags"
        )
    reason = draft.reason.strip() if draft.reason is not None else None
    if draft.supersedes is not None and not reason:
        raise ValidationError(
            f"supersedes={draft.supersedes!r} needs a reason: one sentence saying what "
            "changed relative to the superseded record"
        )
    if draft.id is not None:
        if ID_PATTERN.fullmatch(draft.id) is None:
            raise ValidationError(
                f"id {draft.id!r} is malformed: use a recognized entry-type prefix with "
                "5 or 6 lowercase base36 characters, or a recognized 16-hex compatibility id"
            )
        expected = ENTRY_TYPE_PREFIXES[draft.entry_type]
        if not draft.id.startswith(f"{expected}-"):
            raise ValidationError(
                f"id {draft.id!r} does not match entry_type={draft.entry_type!r}; "
                f"Store.new_record_id({draft.entry_type!r}) mints ids starting {expected!r}"
            )
    for item in draft.links:
        if item.relation == "supersedes":
            raise ValidationError(
                "a supersedes link is written through supersedes= with a reason, not through links"
            )
    for position, artifact in enumerate(draft.artifacts):
        if artifact.digest is not None and _DIGEST.fullmatch(artifact.digest) is None:
            raise ValidationError(
                f"artifact[{position}].digest {artifact.digest!r} is malformed: use "
                "'sha256:<64 lowercase hex>' or null"
            )
    repos = [capture.repo_path for capture in draft.code]
    if len(set(repos)) != len(repos):
        raise ValidationError("code lists the same repo_path twice")
    value_text = _value_text(draft.value)
    return title, tags, reason, value_text


def _ordered_links(links: Sequence[tuple[datetime, Link]]) -> list[Link]:
    """Outgoing links in write order, ties broken by relation then target.

    One order for the write path and the read path, so a written Record equals its read-back.
    """
    return [
        link
        for _moment, link in sorted(
            links, key=lambda item: (item[0], item[1].relation, item[1].target_id)
        )
    ]


def _by_repo(captures: Sequence[CodeCapture]) -> list[CodeCapture]:
    """Code captures in repo_path order, the order `get_records` returns them in."""
    return sorted(captures, key=_repo_path)


def _repo_path(capture: CodeCapture) -> str:
    return capture.repo_path


def _normalise_tags(tags: Sequence[str]) -> list[str]:
    """strip().lower(), deduplicated and sorted. A tag empty after strip, or wider than its
    column, is refused.

    Two tags the case- and accent-insensitive collation treats as one (`cafe`, `café`) are
    NOT detected here: any Python folding approximates the collation and misses both ways.
    The database's own comparison decides, in `Store._classify_failed_write`, after the
    primary key refuses the second tag.
    """
    normalised: set[str] = set()
    for raw in tags:
        tag = raw.strip().lower()
        if not tag:
            raise ValidationError("a tag is empty after stripping whitespace")
        _require_length("tag", tag, TAG_CHARS)
        normalised.add(tag)
    return sorted(normalised)


def _require_length(field: str, value: str | None, limit: int) -> None:
    """Refuse an unencodable value or one longer than its VARCHAR column."""
    _require_utf8(field, value)
    if value is not None and len(value) > limit:
        raise ValidationError(f"{field} is {len(value)} characters; the limit is {limit}")


def _require_text_bytes(field: str, value: str | None) -> None:
    """Refuse unencodable text or text over the column's 65,535 UTF-8 bytes."""
    encoded = _require_utf8(field, value)
    if encoded is not None and len(encoded) > TEXT_BYTES:
        raise ValidationError(
            f"{field} is {len(encoded)} bytes of UTF-8; the limit is {TEXT_BYTES}"
        )


def _require_utf8(field: str, value: str | None) -> bytes | None:
    """Return UTF-8 bytes or translate unpaired surrogates to malformed input."""
    if value is None:
        return None
    try:
        return value.encode()
    except UnicodeEncodeError as exc:
        raise ValidationError(f"{field} contains an unpaired Unicode surrogate") from exc


def _value_text(value: dict[str, Any] | None) -> str | None:
    if value is None:
        return None
    try:
        return json.dumps(value, sort_keys=True, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValidationError(f"value is not a JSON object: {exc}") from exc


def _assert_read_only(sql: str) -> None:
    stripped = sql.strip().rstrip(";")
    if ";" in stripped:
        raise ValueError("Store.query accepts one statement; a second statement is not read-only")
    tokens = _words(stripped)
    if not tokens or tokens[0] not in _READ_ONLY_FIRST_KEYWORDS:
        raise ValueError(f"Store.query is read-only; refusing {sql.strip()[:60]!r}")
    if tokens[0] in _NON_MODIFYING_FIRST_KEYWORDS:
        return
    # A leading keyword is not enough: SQL permits a CTE in front of a modifying statement,
    # so any standalone write keyword anywhere is refused. This rejects a SELECT whose string
    # literal contains the word `delete`, a false positive in the safe direction; search terms
    # are bound after this check, so they never reach it.
    offending = sorted(set(tokens) & _WRITE_KEYWORDS)
    if offending:
        raise ValueError(
            f"Store.query is read-only; statement contains {', '.join(offending)}. "
            "If a literal string tripped this, rephrase it."
        )


def _words(sql: str) -> list[str]:
    """Split into alphanumeric words so keywords are matched on boundaries."""
    spaced = "".join(c if c.isalnum() or c == "_" else " " for c in sql)
    return spaced.lower().split()


def _bind(sql: str, params: Sequence[Any]) -> str:
    """Substitute each `?` placeholder in `sql` with the SQL literal of the matching param.

    Scans `sql` once, left to right, and never rescans substituted text, so a param that
    itself contains `?` is not spliced into by the next param. A `?` inside a quoted string
    ('...' or "...", honoring backslash escapes and doubled quotes) or a backtick-quoted
    identifier is literal text, not a placeholder. Comments are not recognized; the store
    writes no SQL comments.

    Raises:
        ValueError: the number of placeholders differs from len(params), or `sql` ends
            inside a quoted string or identifier.
    """
    positions = _placeholder_positions(sql)
    if len(positions) != len(params):
        raise ValueError(
            f"statement has {len(positions)} placeholders but {len(params)} params given"
        )
    pieces: list[str] = []
    start = 0
    for position, value in zip(positions, params, strict=True):
        pieces.append(sql[start:position])
        pieces.append(dolt.sql_literal(value))
        start = position + 1
    pieces.append(sql[start:])
    return "".join(pieces)


def _placeholder_positions(sql: str) -> list[int]:
    positions: list[int] = []
    quote: str | None = None
    index = 0
    while index < len(sql):
        char = sql[index]
        if quote is not None:
            if char == "\\" and quote != "`":
                index += 2
                continue
            if char == quote:
                if sql[index + 1 : index + 2] == quote:
                    index += 2
                    continue
                quote = None
        elif char in "'\"`":
            quote = char
        elif char == "?":
            positions.append(index)
        index += 1
    if quote is not None:
        raise ValueError(f"statement ends inside a {quote} quoted section")
    return positions


def _placeholders(count: int) -> str:
    return ", ".join("?" * count)


def _insert(table: str, columns: dict[str, Any]) -> str:
    names = ", ".join(f"`{column}`" for column in columns)
    values = ", ".join(dolt.sql_literal(v) for v in columns.values())
    return f"INSERT INTO {table} ({names}) VALUES ({values})"


def _insert_vector(table: str, columns: dict[str, Any], vector: Sequence[float]) -> str:
    names = ", ".join([*(f"`{column}`" for column in columns), "`vec`"])
    values = ", ".join(
        [
            *(dolt.sql_literal(value) for value in columns.values()),
            f"STRING_TO_VECTOR({dolt.sql_literal(dolt.vector_literal(vector))})",
        ]
    )
    return f"INSERT INTO {table} ({names}) VALUES ({values})"


def _id_candidates(entry_type: EntryType) -> Iterator[str]:
    """Ten five-character candidates, then ten six-character candidates."""
    for length in ID_LENGTHS:
        for _attempt in range(ID_ATTEMPTS_PER_LENGTH):
            yield _mint_id(entry_type, length)


def _mint_id(entry_type: EntryType, length: int) -> str:
    """Type prefix plus `length` independent lowercase base36 characters."""
    suffix = "".join(secrets.choice(ID_ALPHABET) for _ in range(length))
    return f"{ENTRY_TYPE_PREFIXES[entry_type]}-{suffix}"


def _now() -> datetime:
    """The current time in UTC, at the microsecond precision the schema holds."""
    return datetime.now(UTC)


def _optional_str(value: Any) -> str | None:
    return None if value is None else str(value)


def _as_bool(value: Any) -> bool:
    """A BOOLEAN column, which Dolt stores as TINYINT and returns as 0 or 1."""
    return bool(int(value)) if not isinstance(value, bool) else value


def _as_datetime(value: Any) -> datetime:
    """A `DATETIME(6)` column as an aware UTC datetime. Dolt emits six fractional digits."""
    return datetime.strptime(str(value), dolt.DOLT_DATETIME).replace(tzinfo=UTC)


def _as_date(value: Any) -> date:
    """A DATE column, which Dolt may return with or without a time part."""
    return date.fromisoformat(str(value)[:10])


def _as_entry_type(value: Any) -> EntryType:
    text = str(value)
    if text not in ENTRY_TYPES:
        raise DoltError(f"record has unknown entry_type {text!r}")
    return text


def _as_work_status(value: Any) -> WorkStatus:
    text = str(value)
    if text not in WORK_STATUSES:
        raise DoltError(f"record has unknown work_status {text!r}")
    return text


def _as_value(value: Any) -> dict[str, Any] | None:
    """The JSON value column, which `-r json` returns already decoded."""
    if value is None:
        return None
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, dict):
        raise ValueError(f"record.value does not hold a JSON object: {value!r}")
    return value


def _as_relation(value: Any) -> Relation:
    text = str(value)
    for relation in RELATIONS:
        if relation == text:
            return relation
    raise ValueError(f"link.relation {text!r} is not one of {RELATIONS}")


def _project_id_candidates() -> Iterator[str]:
    """Ten five-character project ids, then ten six-character candidates."""
    for length in ID_LENGTHS:
        for _attempt in range(ID_ATTEMPTS_PER_LENGTH):
            suffix = "".join(secrets.choice(ID_ALPHABET) for _ in range(length))
            yield f"{PROJECT_ID_PREFIX}-{suffix}"


def _validated_project_name(name: str) -> str:
    """A trimmed one-line human name that cannot be mistaken for a project id."""
    normal = name.strip()
    if not normal:
        raise ValidationError("project name is empty")
    if PROJECT_ID_PATTERN.fullmatch(normal) is not None:
        raise ValidationError(f"project name {normal!r} matches the reserved project id shape")
    if any(_forbidden_one_line_character(character) for character in normal):
        raise ValidationError("project name must be one line with no control characters")
    _require_length("project name", normal, NAME_CHARS)
    return normal


def _validated_framing(framing: ProjectFramingDraft | None) -> tuple[str, str]:
    """Validate framing without changing bytes other than requiring a clean one-line title."""
    if framing is None:
        return "", ""
    title = framing.title
    if not title.strip():
        raise ValidationError("framing title is empty")
    if title != title.strip() or any(
        _forbidden_one_line_character(character) for character in title
    ):
        raise ValidationError("framing title must be one trimmed line with no control characters")
    if "\x00" in framing.body:
        raise ValidationError("framing body contains a NUL byte")
    _require_utf8("framing body", framing.body)
    _require_length("framing title", title, TITLE_CHARS)
    return title, framing.body


def _normalise_newlines(text: str) -> str:
    """The only equivalence allowed for lost-response framing retry recognition."""
    return text.replace("\r\n", "\n").replace("\r", "\n")


def _same_framing(existing: ProjectFramingVersion, title: str, body: str) -> bool:
    return _normalise_newlines(existing.title) == _normalise_newlines(
        title
    ) and _normalise_newlines(existing.body) == _normalise_newlines(body)


def _project_framing(row: dict[str, Any]) -> ProjectFramingVersion:
    return ProjectFramingVersion(
        project_id=str(row["project_id"]),
        version=int(row["version"]),
        title=str(row["title"]),
        body=str(row.get("body", "")),
        reason=_optional_str(row.get("reason")),
        author=str(row["author"]),
        created_at=_as_datetime(row["created_at"]),
    )


def _as_project_action(value: Any) -> ProjectAction:
    text = str(value)
    if text not in PROJECT_ACTIONS:
        raise DoltError(f"membership has unknown action {text!r}")
    return text


def _project_ref_key(item: ProjectRef) -> tuple[str, str]:
    return item.name, item.id


def _project_suggestions(value: str, rows: Sequence[dict[str, Any]]) -> list[ProjectRef]:
    """Up to three close human names, ordered by similarity then name and id."""
    target = value.casefold().strip()
    candidates = []
    for row in rows:
        name = str(row["name"])
        ratio = difflib.SequenceMatcher(a=target, b=name.casefold()).ratio()
        if ratio >= 0.6:
            candidates.append((-ratio, name.casefold(), str(row["id"]), name))
    return [
        ProjectRef(id=project_id, name=name)
        for _ratio, _normal, project_id, name in sorted(candidates)[:3]
    ]


def _forbidden_one_line_character(character: str) -> bool:
    """True for Unicode controls, surrogates, formatting controls, and line separators."""
    return unicodedata.category(character) in {"Cc", "Cf", "Cs", "Zl", "Zp"}
