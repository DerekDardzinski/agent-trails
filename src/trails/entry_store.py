"""The entry store: four kinds, stable ids with append-only versions, links, tasks, comments.

The store stores, links and tracks entries and decides nothing about their meaning. It refuses
exactly two things, both as ValidationError: malformed input, and an attempt to overwrite, which
here means writing a version from a stale `base_version` or taking over a claim without saying
why. Every row is an INSERT; nothing updates or deletes a version, a link event, a task event or
a comment. Current project paths alone are mutable metadata, inherited from `Store`.

Derived state (current links, task state, readiness) comes only from stored links, events and
version fields, never from what an entry says.

`EntryStore` extends `Store` for the project, alias and framing methods, whose tables are
unchanged. The record methods `Store` also carries read tables this schema does not have; they
are removed when the old operations are.

Every write records who made it twice over: `author` is the OS user, and `harness` and
`session_id` come from `sessions.current_provenance()`, on every row, so a harness that gives no
session is still recorded. A session's transcript and agent are written once, in `session`, the
first time a write names it.
"""

from __future__ import annotations

import json
import secrets
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from trails import capture, dolt, sessions
from trails.author import current_author
from trails.errors import DoltError, RecordNotFoundError, StoreUnavailableError, ValidationError
from trails.models import (
    ArtifactRef,
    CodeCapture,
    Comment,
    CommentOutcome,
    CommentPage,
    Entry,
    EntryDraft,
    EntryLink,
    EntryVersion,
    EntryWriteOutcome,
    LinkEvent,
    LinkOutcome,
    MovedLink,
    ProjectMembershipChange,
    ProjectRef,
    Provenance,
    RetireOutcome,
    SessionRow,
    SetProjectsOutcome,
    SinceComment,
    TaskEvent,
    TaskOutcome,
    VersionChange,
)
from trails.secret_scan import scan_for_secrets
from trails.store import (
    _DIGEST,
    _TEXT,
    AGENT_NAME_CHARS,
    EMBEDDING_DIMENSIONS,
    HOST_CHARS,
    NAME_CHARS,
    PATH_CHARS,
    SESSION_CHARS,
    TAG_CHARS,
    TITLE_CHARS,
    Store,
    _as_bool,
    _as_datetime,
    _as_value,
    _bind,
    _insert,
    _normalise_tags,
    _now,
    _optional_str,
    _placeholders,
    _require_length,
    _require_text_bytes,
    _session_insert,
    _value_text,
    resolve_db_path,
)
from trails.taskgraph import Node, TaskGraph
from trails.vocab import (
    COMMENT_ID_PREFIX,
    EVIDENCE_RELATIONS,
    ID_ALPHABET,
    ID_ATTEMPTS_PER_LENGTH,
    ID_LENGTHS,
    KIND_PREFIXES,
    KINDS,
    KNOWLEDGE_KINDS,
    LINK_ACTIONS,
    LINK_RELATIONS,
    OUTCOMES,
    PRIORITY_RANGE,
    PROJECT_ACTIONS,
    TASK_EVENTS,
    VERDICTS,
    WORK_KINDS,
    WORK_RELATIONS,
    Kind,
    LinkAction,
    LinkRelation,
    Outcome,
    TaskEventKind,
    TaskState,
    Verdict,
    enum_column,
)

# Written in the database directory once the schema is known to exist. A different name from
# the old store's marker, so an old database is always checked and refused (D13).
SCHEMA_MARKER = ".agent-trails-schema"

SCHEMA: tuple[str, ...] = (
    f"""CREATE TABLE entry (
        id         VARCHAR(32) {_TEXT} PRIMARY KEY,
        kind       {enum_column(KINDS)} NOT NULL,
        created_at DATETIME(6) NOT NULL
    )""",
    f"""CREATE TABLE entry_version (
        entry_id   VARCHAR(32) {_TEXT} NOT NULL,
        version    INT NOT NULL,
        title      VARCHAR({TITLE_CHARS}) {_TEXT} NOT NULL,
        body       LONGTEXT {_TEXT} NOT NULL,
        value      JSON,
        priority   INT,
        reason     TEXT {_TEXT},
        author     VARCHAR(120) {_TEXT} NOT NULL,
        harness    VARCHAR({NAME_CHARS}) {_TEXT} NOT NULL,
        session_id VARCHAR({SESSION_CHARS}) {_TEXT},
        created_at DATETIME(6) NOT NULL,
        PRIMARY KEY (entry_id, version),
        FULLTEXT KEY ft_entry_version_title (title),
        FULLTEXT KEY ft_entry_version_body (body)
    )""",
    f"""CREATE TABLE version_tag (
        entry_id VARCHAR(32) {_TEXT} NOT NULL,
        version  INT NOT NULL,
        tag      VARCHAR({TAG_CHARS}) {_TEXT} NOT NULL,
        PRIMARY KEY (entry_id, version, tag)
    )""",
    f"""CREATE TABLE version_artifact (
        entry_id VARCHAR(32) {_TEXT} NOT NULL,
        version  INT NOT NULL,
        position INT NOT NULL,
        location TEXT {_TEXT} NOT NULL,
        digest   VARCHAR(80) {_TEXT},
        notes    TEXT {_TEXT} NOT NULL,
        PRIMARY KEY (entry_id, version, position)
    )""",
    f"""CREATE TABLE version_code (
        entry_id     VARCHAR(32) {_TEXT} NOT NULL,
        version      INT NOT NULL,
        repo_path    VARCHAR({PATH_CHARS}) {_TEXT} NOT NULL,
        head_sha     VARCHAR(64) {_TEXT},
        captured_sha VARCHAR(64) {_TEXT} NOT NULL,
        dirty        BOOLEAN NOT NULL,
        PRIMARY KEY (entry_id, version, repo_path)
    )""",
    f"""CREATE TABLE link_event (
        source_id      VARCHAR(32) {_TEXT} NOT NULL,
        relation       {enum_column(LINK_RELATIONS)} NOT NULL,
        target_id      VARCHAR(32) {_TEXT} NOT NULL,
        sequence       INT NOT NULL,
        action         {enum_column(LINK_ACTIONS)} NOT NULL,
        target_version INT,
        reason         TEXT {_TEXT},
        author         VARCHAR(120) {_TEXT} NOT NULL,
        harness        VARCHAR({NAME_CHARS}) {_TEXT} NOT NULL,
        session_id     VARCHAR({SESSION_CHARS}) {_TEXT},
        created_at     DATETIME(6) NOT NULL,
        PRIMARY KEY (source_id, relation, target_id, sequence)
    )""",
    f"""CREATE TABLE task_event (
        entry_id   VARCHAR(32) {_TEXT} NOT NULL,
        sequence   INT NOT NULL,
        event      {enum_column(TASK_EVENTS)} NOT NULL,
        outcome    {enum_column(OUTCOMES)},
        reason     TEXT {_TEXT},
        author     VARCHAR(120) {_TEXT} NOT NULL,
        harness    VARCHAR({NAME_CHARS}) {_TEXT} NOT NULL,
        session_id VARCHAR({SESSION_CHARS}) {_TEXT},
        created_at DATETIME(6) NOT NULL,
        PRIMARY KEY (entry_id, sequence),
        FULLTEXT KEY ft_task_event_reason (reason)
    )""",
    f"""CREATE TABLE comment (
        id            VARCHAR(32) {_TEXT} PRIMARY KEY,
        entry_id      VARCHAR(32) {_TEXT} NOT NULL,
        entry_version INT NOT NULL,
        body          LONGTEXT {_TEXT} NOT NULL,
        verdict       {enum_column(VERDICTS)},
        author        VARCHAR(120) {_TEXT} NOT NULL,
        harness       VARCHAR({NAME_CHARS}) {_TEXT} NOT NULL,
        session_id    VARCHAR({SESSION_CHARS}) {_TEXT},
        created_at    DATETIME(6) NOT NULL,
        FULLTEXT KEY ft_comment_body (body)
    )""",
    f"""CREATE TABLE comment_addresses (
        comment_id   VARCHAR(32) {_TEXT} NOT NULL,
        addressed_id VARCHAR(32) {_TEXT} NOT NULL,
        PRIMARY KEY (comment_id, addressed_id)
    )""",
    f"""CREATE TABLE comment_pin (
        comment_id VARCHAR(32) {_TEXT} NOT NULL,
        entry_id   VARCHAR(32) {_TEXT} NOT NULL,
        version    INT NOT NULL,
        PRIMARY KEY (comment_id, entry_id)
    )""",
    f"""CREATE TABLE session (
        id         VARCHAR({SESSION_CHARS}) {_TEXT} PRIMARY KEY,
        harness    VARCHAR({NAME_CHARS}) {_TEXT} NOT NULL,
        transcript TEXT {_TEXT},
        agent      VARCHAR({AGENT_NAME_CHARS}) {_TEXT},
        first_seen DATETIME(6) NOT NULL
    )""",
    f"""CREATE TABLE retirement (
        entry_id   VARCHAR(32) {_TEXT} PRIMARY KEY,
        retired_at DATETIME(6) NOT NULL,
        reason     TEXT {_TEXT} NOT NULL,
        author     VARCHAR(120) {_TEXT} NOT NULL,
        harness    VARCHAR({NAME_CHARS}) {_TEXT} NOT NULL,
        session_id VARCHAR({SESSION_CHARS}) {_TEXT}
    )""",
    f"""CREATE TABLE project (
        id              VARCHAR(32) {_TEXT} PRIMARY KEY,
        name            VARCHAR({NAME_CHARS}) {_TEXT} NOT NULL UNIQUE,
        created_at      DATETIME(6) NOT NULL,
        created_by      VARCHAR(120) {_TEXT} NOT NULL,
        created_on_host VARCHAR({HOST_CHARS}) {_TEXT} NOT NULL,
        harness         VARCHAR({NAME_CHARS}) {_TEXT} NOT NULL,
        session_id      VARCHAR({SESSION_CHARS}) {_TEXT}
    )""",
    f"""CREATE TABLE project_path (
        project_id          VARCHAR(32) {_TEXT} NOT NULL,
        path                VARCHAR({PATH_CHARS}) COLLATE utf8mb4_0900_bin NOT NULL,
        label               TEXT {_TEXT} NOT NULL,
        access_instructions TEXT {_TEXT} NOT NULL,
        host                VARCHAR({HOST_CHARS}) {_TEXT} NOT NULL,
        author              VARCHAR(120) {_TEXT} NOT NULL,
        harness             VARCHAR({NAME_CHARS}) {_TEXT} NOT NULL,
        session_id          VARCHAR({SESSION_CHARS}) {_TEXT},
        set_at              DATETIME(6) NOT NULL,
        PRIMARY KEY (project_id, path)
    )""",
    f"""CREATE TABLE project_framing_version (
        project_id VARCHAR(32) {_TEXT} NOT NULL,
        version    INT NOT NULL,
        title      VARCHAR({TITLE_CHARS}) {_TEXT} NOT NULL,
        body       LONGTEXT {_TEXT} NOT NULL,
        reason     TEXT {_TEXT},
        author     VARCHAR(120) {_TEXT} NOT NULL,
        harness    VARCHAR({NAME_CHARS}) {_TEXT} NOT NULL,
        session_id VARCHAR({SESSION_CHARS}) {_TEXT},
        created_at DATETIME(6) NOT NULL,
        PRIMARY KEY (project_id, version),
        FULLTEXT KEY ft_project_framing_title (title),
        FULLTEXT KEY ft_project_framing_body (body)
    )""",
    f"""CREATE TABLE entry_project_event (
        entry_id   VARCHAR(32) {_TEXT} NOT NULL,
        project_id VARCHAR(32) {_TEXT} NOT NULL,
        sequence   INT NOT NULL,
        action     {enum_column(PROJECT_ACTIONS)} NOT NULL,
        reason     TEXT {_TEXT},
        author     VARCHAR(120) {_TEXT} NOT NULL,
        harness    VARCHAR({NAME_CHARS}) {_TEXT} NOT NULL,
        session_id VARCHAR({SESSION_CHARS}) {_TEXT},
        created_at DATETIME(6) NOT NULL,
        PRIMARY KEY (entry_id, project_id, sequence)
    )""",
    f"""CREATE TABLE entry_chunk_embedding (
        entry_id    VARCHAR(32) {_TEXT} NOT NULL,
        version     INT NOT NULL,
        chunk_index INT NOT NULL,
        model_id    VARCHAR(120) {_TEXT} NOT NULL,
        vec         VECTOR({EMBEDDING_DIMENSIONS}) NOT NULL,
        PRIMARY KEY (entry_id, version, model_id, chunk_index)
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
        tag        VARCHAR({TAG_CHARS}) {_TEXT} PRIMARY KEY,
        canonical  VARCHAR({TAG_CHARS}) {_TEXT} NOT NULL,
        author     VARCHAR(120) {_TEXT} NOT NULL,
        harness    VARCHAR({NAME_CHARS}) {_TEXT} NOT NULL,
        session_id VARCHAR({SESSION_CHARS}) {_TEXT},
        created_at DATETIME(6) NOT NULL
    )""",
)


def open_entry_store(db_path: Path | None = None) -> EntryStore:
    """Open the database, creating it and its schema if absent.

    `None` resolves to `$TRAILS_DB`, then the default path. There is no migration.

    Raises:
        StoreUnavailableError: the `dolt` binary is missing, the database cannot be opened, or
            it holds the old record schema (D13), which this store does not read.
        DoltError: creating the schema failed.
    """
    resolved = resolve_db_path(db_path)
    dolt.init_database(resolved)
    store = EntryStore(db_path=resolved)
    marker = resolved / SCHEMA_MARKER
    if not marker.exists():
        # Keyed on the entry table rather than on `.dolt`, so a process that died between
        # `dolt init` and the schema script cannot leave a tableless database marked complete.
        with dolt.exclusive(resolved):
            entry_rows, record_rows = dolt.read_result_sets(
                resolved, ["SHOW TABLES LIKE 'entry'", "SHOW TABLES LIKE 'record'"]
            )
            if not entry_rows:
                if record_rows:
                    raise StoreUnavailableError(
                        f"{resolved} holds the old trails record schema, which this version "
                        "does not read and does not migrate. Point TRAILS_DB at a new path, "
                        "or leave it unset to use the default database; the old database stays "
                        "as it is"
                    )
                dolt.run_script(resolved, [*SCHEMA, "CALL DOLT_COMMIT('-A','-m','init schema')"])
        marker.touch()
    return store


@dataclass(frozen=True)
class EntryStore(Store):
    """A handle on the database. Cheap to construct; holds no connection."""

    # ------------------------------------------------------------------ reads

    def get_entries(self, entry_ids: Sequence[str]) -> dict[str, Entry]:
        """Assemble entries with their current version and state in ONE dolt invocation.

        Unknown ids are absent from the result.
        """
        ids = list(dict.fromkeys(entry_ids))
        if not ids:
            return {}
        within = _placeholders(len(ids))
        current = (
            "NOT EXISTS (SELECT 1 FROM entry_version n WHERE n.entry_id = x.entry_id "
            "AND n.version > x.version)"
        )
        statements = [
            _bind(
                "SELECT e.id, e.kind, e.created_at AS entry_created_at, x.entry_id, x.version, "
                "x.title, x.body, x.value, x.priority, x.reason, x.author, "
                "x.harness, x.session_id, x.created_at FROM entry e JOIN entry_version x ON "
                f"x.entry_id = e.id WHERE e.id IN ({within}) AND {current}",
                ids,
            ),
            _bind(
                f"SELECT x.entry_id, x.version, x.tag FROM version_tag x WHERE x.entry_id IN "
                f"({within}) AND {current}",
                ids,
            ),
            _bind(
                "SELECT x.entry_id, x.version, x.position, x.location, x.digest, x.notes FROM "
                f"version_artifact x WHERE x.entry_id IN ({within}) AND {current} "
                "ORDER BY x.entry_id, x.position",
                ids,
            ),
            _bind(
                "SELECT x.entry_id, x.version, x.repo_path, x.head_sha, x.captured_sha, x.dirty "
                f"FROM version_code x WHERE x.entry_id IN ({within}) AND {current} "
                "ORDER BY x.entry_id, x.repo_path",
                ids,
            ),
            _bind(
                f"SELECT * FROM link_event WHERE source_id IN ({within}) "
                "ORDER BY source_id, relation, target_id, sequence",
                ids,
            ),
            _bind(
                f"SELECT * FROM task_event WHERE entry_id IN ({within}) "
                "ORDER BY entry_id, sequence",
                ids,
            ),
            _bind(f"SELECT entry_id, reason FROM retirement WHERE entry_id IN ({within})", ids),
            _bind(
                "SELECT e.entry_id, p.id AS project_id, p.name AS project_name "
                "FROM entry_project_event e JOIN project p ON p.id = e.project_id "
                f"WHERE e.entry_id IN ({within}) AND e.action = 'add' AND NOT EXISTS ("
                "SELECT 1 FROM entry_project_event newer WHERE newer.entry_id = e.entry_id "
                "AND newer.project_id = e.project_id AND newer.sequence > e.sequence) "
                "ORDER BY e.entry_id, p.name, p.id",
                ids,
            ),
        ]
        (versions, tags, artifacts, code, link_rows, task_rows, retired, memberships) = (
            dolt.read_result_sets(self.db_path, statements)
        )
        tags_by = _group(tags, lambda row: str(row["tag"]))
        artifacts_by = _group(artifacts, _artifact)
        code_by = _group(code, _code)
        links_by: dict[str, list[EntryLink]] = {}
        for event in current_links(_link_event(row) for row in link_rows):
            links_by.setdefault(event.source_id, []).append(_entry_link(event))
        events_by: dict[str, list[TaskEvent]] = {}
        for row in task_rows:
            events_by.setdefault(str(row["entry_id"]), []).append(_task_event(row))
        retired_by = {str(row["entry_id"]): str(row["reason"]) for row in retired}
        projects_by: dict[str, list[ProjectRef]] = {}
        for row in memberships:
            projects_by.setdefault(str(row["entry_id"]), []).append(
                ProjectRef(id=str(row["project_id"]), name=str(row["project_name"]))
            )
        assembled: dict[str, Entry] = {}
        for row in versions:
            entry_id = str(row["id"])
            kind = _as_kind(row["kind"])
            events = events_by.get(entry_id, [])
            state, outcome, claim = task_state(kind, events)
            assembled[entry_id] = Entry(
                id=entry_id,
                kind=kind,
                created_at=_as_datetime(row["entry_created_at"]),
                current=_version(
                    row,
                    tags_by.get(entry_id, []),
                    artifacts_by.get(entry_id, []),
                    code_by.get(entry_id, []),
                ),
                projects=projects_by.get(entry_id, []),
                links=links_by.get(entry_id, []),
                state=state,
                outcome=outcome,
                claim=claim,
                task_events=events,
                retired=entry_id in retired_by,
                retired_reason=retired_by.get(entry_id),
            )
        return {entry_id: assembled[entry_id] for entry_id in ids if entry_id in assembled}

    def get_entry(self, entry_id: str) -> Entry:
        """One entry.

        Raises:
            RecordNotFoundError: no entry has this id.
        """
        found = self.get_entries([entry_id]).get(entry_id)
        if found is None:
            raise RecordNotFoundError(f"no entry has id {entry_id!r}")
        return found

    def get_versions(self, entry_id: str) -> list[EntryVersion]:
        """Every version of one entry, oldest first, in one dolt invocation."""
        versions, tags, artifacts, code = dolt.read_result_sets(
            self.db_path,
            [
                _bind(
                    "SELECT * FROM entry_version WHERE entry_id = ? ORDER BY version", [entry_id]
                ),
                _bind("SELECT * FROM version_tag WHERE entry_id = ?", [entry_id]),
                _bind(
                    "SELECT * FROM version_artifact WHERE entry_id = ? ORDER BY version, position",
                    [entry_id],
                ),
                _bind(
                    "SELECT * FROM version_code WHERE entry_id = ? ORDER BY version, repo_path",
                    [entry_id],
                ),
            ],
        )
        tags_by = _group(tags, lambda row: str(row["tag"]), key=_version_key)
        artifacts_by = _group(artifacts, _artifact, key=_version_key)
        code_by = _group(code, _code, key=_version_key)
        return [
            _version(
                row,
                tags_by.get(_version_key(row), []),
                artifacts_by.get(_version_key(row), []),
                code_by.get(_version_key(row), []),
            )
            for row in versions
        ]

    def get_session(self, session_id: str) -> SessionRow | None:
        """The session row for one session id, or None."""
        rows = self._read(_bind("SELECT * FROM session WHERE id = ?", [session_id]))
        if not rows:
            return None
        row = rows[0]
        return SessionRow(
            id=str(row["id"]),
            harness=str(row["harness"]),
            transcript=_optional_str(row.get("transcript")),
            agent=_optional_str(row.get("agent")),
            first_seen=_as_datetime(row["first_seen"]),
        )

    def get_sessions(self, session_ids: Sequence[str]) -> dict[str, SessionRow]:
        """Session rows for many ids in one read."""
        ids = [item for item in dict.fromkeys(session_ids) if item]
        if not ids:
            return {}
        rows = self._read(
            _bind(f"SELECT * FROM session WHERE id IN ({_placeholders(len(ids))})", ids)
        )
        return {
            str(row["id"]): SessionRow(
                id=str(row["id"]),
                harness=str(row["harness"]),
                transcript=_optional_str(row.get("transcript")),
                agent=_optional_str(row.get("agent")),
                first_seen=_as_datetime(row["first_seen"]),
            )
            for row in rows
        }

    def claims_held_at_creation(self, entry_ids: Sequence[str]) -> dict[str, list[str] | None]:
        """For each entry, the tasks its creating session held a claim on when version 1 was
        written, from stored task events. None when version 1 recorded no session id, since
        then nothing says which claims were its own. One read."""
        ids = list(dict.fromkeys(entry_ids))
        if not ids:
            return {}
        firsts, events = dolt.read_result_sets(
            self.db_path,
            [
                _bind(
                    "SELECT entry_id, harness, session_id, created_at FROM entry_version WHERE "
                    f"version = 1 AND entry_id IN ({_placeholders(len(ids))})",
                    ids,
                ),
                _bind(
                    "SELECT * FROM task_event WHERE entry_id IN (SELECT c.entry_id FROM "
                    "task_event c JOIN entry_version v ON v.session_id = c.session_id AND "
                    f"v.version = 1 WHERE c.event = 'claim' AND v.entry_id IN "
                    f"({_placeholders(len(ids))})) ORDER BY entry_id, sequence",
                    ids,
                ),
            ],
        )
        by_task: dict[str, list[TaskEvent]] = {}
        for row in events:
            by_task.setdefault(str(row["entry_id"]), []).append(_task_event(row))
        held: dict[str, list[str] | None] = {}
        for row in firsts:
            session = _optional_str(row.get("session_id"))
            if session is None:
                held[str(row["entry_id"])] = None
                continue
            moment = _as_datetime(row["created_at"])
            claimant = Provenance(harness=str(row["harness"]), session_id=session)
            found = []
            for task_id, task_events in sorted(by_task.items()):
                _state, _outcome, claim = task_state(
                    "task", [event for event in task_events if event.created_at <= moment]
                )
                if claim is not None and _same_session(claim, claimant):
                    found.append(task_id)
            held[str(row["entry_id"])] = [item for item in found if item != str(row["entry_id"])]
        return held

    # ----------------------------------------------------------------- writes

    def new_entry_id(self, kind: Kind) -> str:
        """Mint an unused kind-prefixed id without writing anything, so a caller can name
        capture refs after the entry before writing it."""
        for candidate in _id_candidates(KIND_PREFIXES[kind]):
            if not self._read(_bind("SELECT id FROM entry WHERE id = ?", [candidate])):
                return candidate
        raise DoltError(
            f"failed to generate an unused {kind} id after "
            f"{len(ID_LENGTHS) * ID_ATTEMPTS_PER_LENGTH} candidates"
        )

    def check_entry_draft(self, draft: EntryDraft) -> None:
        """Run every refusal `write_entry` would raise for this draft, without writing, so a
        caller can refuse before doing expensive work such as code capture.

        Raises:
            ValidationError: as `write_entry`.
            RecordNotFoundError: as `write_entry`.
        """
        with dolt.exclusive(self.db_path):
            if draft.id is None:
                self._create_locked(draft, None, check_only=True)
            else:
                self._version_locked(draft, check_only=True)

    def write_entry(
        self, draft: EntryDraft, new_id: str | None = None, capture_ref: str | None = None
    ) -> EntryWriteOutcome:
        """Create an entry (version 1) or write its next version, in one transaction.

        `new_id` is an id minted before the lock (see `new_entry_id`) so that code could be
        captured under it, in refs `refs/research/<capture_ref>/...`. If another write took
        that id meanwhile, a new id is minted here, under the lock, and the capture refs are
        moved to it, because an id collision is neither malformed input nor an overwrite.

        Raises:
            ValidationError: malformed input, or a stale or missing `base_version`.
            RecordNotFoundError: `draft.id` names no entry.
        """
        with dolt.exclusive(self.db_path):
            if draft.id is None:
                created = self._create_locked(draft, new_id, capture_ref=capture_ref)
            else:
                if new_id is not None:
                    raise ValidationError("new_id is for a new entry; a new version keeps its id")
                created = self._version_locked(draft)
            if created is None:  # pragma: no cover - only check_only returns None
                raise DoltError("a write returned no outcome")
            return created

    def _create_locked(  # noqa: PLR0912, PLR0915 — one creation and everything it checks
        self,
        draft: EntryDraft,
        new_id: str | None,
        *,
        capture_ref: str | None = None,
        check_only: bool = False,
    ) -> EntryWriteOutcome | None:
        if draft.kind is None:
            raise ValidationError("a new entry needs kind: task, note, preference or skill")
        kind = draft.kind
        if draft.base_version is not None or draft.reason is not None:
            raise ValidationError(
                "base_version and reason are for a new version of an existing entry; pass id too"
            )
        title = _validated_title(draft.title)
        body = draft.body or ""
        tags = _normalise_tags(draft.tags or [])
        artifacts = list(draft.artifacts or [])
        code = sorted(draft.code or [], key=lambda item: item.repo_path)
        value = draft.value or None
        priority = _validated_priority(kind, draft.priority)
        _validate_content(body, artifacts, code)
        targets = list(dict.fromkeys([*([draft.parent] if draft.parent else []), *draft.waits_on]))
        found = self.get_entries(targets)
        missing = [target for target in targets if target not in found]
        if missing:
            raise ValidationError(f"no entry has id {', '.join(missing)}")
        if draft.parent is not None and draft.parent in draft.waits_on:
            raise ValidationError(f"{draft.parent} is both the parent and in waits_on")
        if targets and kind not in WORK_KINDS:
            raise ValidationError(f"parent and waits_on belong to a task; a {kind} has neither")
        for target in targets:
            relation: LinkRelation = "part_of" if target == draft.parent else "waits_on"
            _require_relation_kinds(relation, kind, found[target].kind, "new entry", target)
        project_refs = sorted(
            self.resolve_projects(draft.projects or []), key=lambda ref: (ref.name, ref.id)
        )
        relations: list[tuple[LinkRelation, str]] = []
        if draft.parent:
            relations.append(("part_of", draft.parent))
        relations += [("waits_on", target) for target in dict.fromkeys(draft.waits_on)]
        if relations:
            _refuse_cycles(self.task_graph(), relations)
        if new_id is not None and not new_id.startswith(f"{KIND_PREFIXES[kind]}-"):
            raise ValidationError(f"id {new_id!r} does not match kind {kind!r}")
        if check_only:
            return None
        entry_id = new_id or self.new_entry_id(kind)
        if new_id is not None and self._read(_bind("SELECT id FROM entry WHERE id = ?", [new_id])):
            entry_id = self.new_entry_id(kind)
            if capture_ref is not None:
                renamed = entry_id + capture_ref.removeprefix(new_id)
                capture.rename_refs(capture.store_path(self.db_path), capture_ref, renamed)
                capture_ref = renamed
        provenance = sessions.current_provenance()
        author = current_author()
        created_at = _now()
        version = EntryVersion(
            entry_id=entry_id,
            version=1,
            title=title,
            body=body,
            tags=tags,
            value=value,
            priority=priority,
            artifacts=artifacts,
            code=code,
            reason=None,
            author=author,
            harness=provenance.harness,
            session_id=provenance.session_id,
            created_at=created_at,
        )
        links = [
            LinkEvent(
                source_id=entry_id,
                relation=relation,
                target_id=target,
                sequence=0,
                action="add",
                target_version=found[target].current.version,
                reason=None,
                author=author,
                harness=provenance.harness,
                session_id=provenance.session_id,
                created_at=created_at,
            )
            for relation, target in relations
        ]
        statements = [
            _insert("entry", {"id": entry_id, "kind": kind, "created_at": created_at}),
            *_version_inserts(version),
            *(_link_insert(event) for event in links),
            *(
                _membership_insert(
                    entry_id,
                    ref.id,
                    sequence=0,
                    action="add",
                    reason=None,
                    author=author,
                    provenance=provenance,
                    created_at=created_at,
                )
                for ref in project_refs
            ),
            *_session_insert(provenance, created_at),
        ]
        findings = scan_for_secrets(_scanned_fields(version))
        try:
            self._commit(
                statements,
                message=f"create {entry_id}",
                entry_id=entry_id,
                version=1,
                created_at=created_at,
                tags=tags,
            )
        except Exception:
            if capture_ref is not None and new_id is not None and entry_id != new_id:
                # The refs were moved to the id minted here and nothing landed under it; the
                # caller discards refs under the id it minted, which no longer has any.
                capture.discard_unwritten_refs(capture.store_path(self.db_path), capture_ref)
            raise
        entry = Entry(
            id=entry_id,
            kind=kind,
            created_at=created_at,
            current=version,
            projects=project_refs,
            links=[_entry_link(event) for event in links],
            state="open" if kind in WORK_KINDS else None,
            outcome=None,
            claim=None,
            task_events=[],
            retired=False,
            retired_reason=None,
        )
        return EntryWriteOutcome(
            entry=entry, created=True, retry=False, warnings=[], secret_findings=findings
        )

    def _version_locked(
        self, draft: EntryDraft, *, check_only: bool = False
    ) -> EntryWriteOutcome | None:
        entry_id = str(draft.id)
        existing = self.get_entries([entry_id]).get(entry_id)
        if existing is None:
            raise RecordNotFoundError(f"no entry has id {entry_id!r}")
        if draft.kind is not None and draft.kind != existing.kind:
            raise ValidationError(f"{entry_id} is a {existing.kind}, not a {draft.kind}")
        if draft.parent is not None or draft.waits_on or draft.projects is not None:
            raise ValidationError("a new version carries content only, never links or projects")
        current = existing.current
        merged = _merged(existing.kind, current, draft)
        retry = _retry_call(existing, draft)
        if draft.base_version is None or draft.base_version != current.version:
            if draft.base_version is not None and draft.base_version < current.version:
                landed = self._landed_retry(entry_id, draft.base_version + 1, merged)
                if landed is not None:
                    return EntryWriteOutcome(
                        entry=existing,
                        created=False,
                        retry=True,
                        unchanged_from=landed.version,
                        warnings=[],
                        secret_findings=[],
                    )
            raise ValidationError(_stale_message(entry_id, draft.base_version, current, retry))
        reason = (draft.reason or "").strip()
        if not reason:
            raise ValidationError(
                f"a new version of {entry_id} needs a reason: one sentence saying what changed"
            )
        _require_text_bytes("reason", reason)
        if check_only:
            return None
        if _content(merged) == _content(current):
            return EntryWriteOutcome(
                entry=existing,
                created=False,
                retry=False,
                unchanged_from=current.version,
                warnings=[],
                secret_findings=[],
            )
        provenance = sessions.current_provenance()
        created_at = _now()
        version = merged.model_copy(
            update={
                "version": current.version + 1,
                "reason": reason,
                "author": current_author(),
                "harness": provenance.harness,
                "session_id": provenance.session_id,
                "created_at": created_at,
            }
        )
        statements = [*_version_inserts(version), *_session_insert(provenance, created_at)]
        findings = scan_for_secrets(_scanned_fields(version))
        self._commit(
            statements,
            message=f"version {entry_id} v{version.version}",
            entry_id=entry_id,
            version=version.version,
            created_at=created_at,
            tags=version.tags,
        )
        return EntryWriteOutcome(
            entry=existing.model_copy(update={"current": version}),
            created=False,
            retry=False,
            warnings=[],
            secret_findings=findings,
        )

    def _landed_retry(
        self, entry_id: str, version: int, merged: EntryVersion
    ) -> EntryVersion | None:
        """The version a lost-response retry would have written, when it already landed.

        A retry of a write whose response was lost carries the old base. When version base+1
        exists with exactly this content from this session, the write had landed.
        """
        for candidate in self.get_versions(entry_id):
            if candidate.version != version:
                continue
            provenance = sessions.current_provenance()
            if _content(candidate) == _content(merged) and (
                candidate.session_id == provenance.session_id
                and candidate.harness == provenance.harness
            ):
                return candidate
        return None

    def _membership_changes(
        self,
        entry_id: str,
        *,
        wanted: Sequence[str],
        reason: str,
        author: str,
        provenance: Provenance,
        created_at: datetime,
    ) -> list[str]:
        """Membership event inserts that make the current membership equal `wanted`."""
        latest = self._latest_memberships(entry_id)
        current = {pid for pid, (_seq, action) in latest.items() if action == "add"}
        target = set(wanted)
        statements = []
        for project_id in sorted(target - current):
            sequence = latest.get(project_id, (-1, ""))[0] + 1
            statements.append(
                _membership_insert(
                    entry_id,
                    project_id,
                    sequence=sequence,
                    action="add",
                    reason=reason,
                    author=author,
                    provenance=provenance,
                    created_at=created_at,
                )
            )
        for project_id in sorted(current - target):
            sequence = latest[project_id][0] + 1
            statements.append(
                _membership_insert(
                    entry_id,
                    project_id,
                    sequence=sequence,
                    action="remove",
                    reason=reason,
                    author=author,
                    provenance=provenance,
                    created_at=created_at,
                )
            )
        return statements

    def _latest_memberships(self, entry_id: str) -> dict[str, tuple[int, str]]:
        rows = self._read(
            _bind(
                "SELECT e.project_id, e.sequence, e.action FROM entry_project_event e "
                "WHERE e.entry_id = ? AND NOT EXISTS (SELECT 1 FROM entry_project_event newer "
                "WHERE newer.entry_id = e.entry_id AND newer.project_id = e.project_id AND "
                "newer.sequence > e.sequence)",
                [entry_id],
            )
        )
        return {str(row["project_id"]): (int(row["sequence"]), str(row["action"])) for row in rows}

    # ------------------------------------------------------------------ links

    def add_entry_link(
        self, source_id: str, relation: LinkRelation, target_id: str, reason: str | None = None
    ) -> LinkOutcome:
        """Append an `add` event, recording the target's current version.

        A work link (`waits_on`, `part_of`) that is already current is left as it is, and one
        that would close a dependency cycle is refused. An evidence link (`supports`,
        `contradicts`) that is already current is recorded again when the target has moved
        on since, so it now names the target's current version; at that version already, it
        is left as it is.

        Raises:
            ValidationError: a self-link, an unknown relation, an id naming no entry, a link
                between the wrong kinds, or a work link closing a cycle.
        """
        if relation not in LINK_RELATIONS:
            raise ValidationError(f"relation must be one of {', '.join(LINK_RELATIONS)}")
        if source_id == target_id:
            raise ValidationError("an entry cannot link to itself")
        normal_reason = (reason or "").strip() or None
        _require_text_bytes("reason", normal_reason)
        if relation == "supersedes":
            raise ValidationError(
                "supersedes is not a link relation: it records a task closed superseded"
            )
        with dolt.exclusive(self.db_path):
            found = self._require_entries([source_id, target_id])
            _require_relation_kinds(
                relation, found[source_id].kind, found[target_id].kind, source_id, target_id
            )
            latest = self._latest_link(source_id, relation, target_id)
            target_version = found[target_id].current.version
            refresh = relation in EVIDENCE_RELATIONS and (
                latest is not None
                and latest.action == "add"
                and latest.target_version != target_version
            )
            if latest is not None and latest.action == "add" and not refresh:
                return LinkOutcome(
                    source_id=source_id,
                    relation=relation,
                    target_id=target_id,
                    action="add",
                    changed=False,
                    target_version=latest.target_version,
                    warnings=[],
                )
            provenance = sessions.current_provenance()
            event = self._link_event(
                source_id,
                relation,
                target_id,
                action="add",
                sequence=latest.sequence + 1 if latest is not None else 0,
                target_version=target_version,
                reason=normal_reason,
                provenance=provenance,
            )
            warnings = _secret_warnings({"reason": normal_reason or ""})
            if relation in WORK_RELATIONS:
                graph = self.task_graph()
                table = graph.parents if relation == "part_of" else graph.waits
                table.setdefault(source_id, []).append(target_id)
                _refuse_cycles_from(graph, [source_id, target_id])
            self._commit_events(
                [_link_insert(event)], f"link {source_id} {relation} {target_id}", event, provenance
            )
        return LinkOutcome(
            source_id=source_id,
            relation=relation,
            target_id=target_id,
            action="add",
            changed=True,
            target_version=target_version,
            warnings=warnings,
            refreshed_from=latest.target_version if refresh and latest is not None else None,
        )

    def remove_entry_link(
        self, source_id: str, relation: LinkRelation, target_id: str, reason: str
    ) -> LinkOutcome:
        """Append a `remove` event for a current link (D23: a reason is required). Removing a
        link that is not current changes nothing.

        Raises:
            ValidationError: an empty reason or an unknown relation.
        """
        if relation not in LINK_RELATIONS:
            raise ValidationError(f"relation must be one of {', '.join(LINK_RELATIONS)}")
        normal_reason = reason.strip()
        if not normal_reason:
            raise ValidationError(
                "unlink needs a reason: removing a link changes what is ready, and a later "
                "reader needs to know why"
            )
        _require_text_bytes("reason", normal_reason)
        with dolt.exclusive(self.db_path):
            self._require_entries([source_id, target_id])
            latest = self._latest_link(source_id, relation, target_id)
            if latest is None or latest.action == "remove":
                return LinkOutcome(
                    source_id=source_id,
                    relation=relation,
                    target_id=target_id,
                    action="remove",
                    changed=False,
                    target_version=None,
                    warnings=[],
                )
            provenance = sessions.current_provenance()
            event = self._link_event(
                source_id,
                relation,
                target_id,
                action="remove",
                sequence=latest.sequence + 1,
                target_version=None,
                reason=normal_reason,
                provenance=provenance,
            )
            self._commit_events(
                [_link_insert(event)],
                f"unlink {source_id} {relation} {target_id}",
                event,
                provenance,
            )
        return LinkOutcome(
            source_id=source_id,
            relation=relation,
            target_id=target_id,
            action="remove",
            changed=True,
            target_version=None,
            warnings=_secret_warnings({"reason": normal_reason}),
        )

    def link_events(self, entry_ids: Sequence[str]) -> list[LinkEvent]:
        """Every link event from or to any of `entry_ids`, in time order."""
        ids = list(dict.fromkeys(entry_ids))
        if not ids:
            return []
        within = _placeholders(len(ids))
        rows = self._read(
            _bind(
                f"SELECT * FROM link_event WHERE source_id IN ({within}) OR target_id IN "
                f"({within}) ORDER BY created_at, source_id, relation, target_id, sequence",
                [*ids, *ids],
            )
        )
        return [_link_event(row) for row in rows]

    def incoming_links(
        self, entry_ids: Sequence[str], relations: Sequence[LinkRelation] = LINK_RELATIONS
    ) -> dict[str, list[EntryLink]]:
        """Current links INTO each of `entry_ids`, from entries that are not retired."""
        ids = list(dict.fromkeys(entry_ids))
        if not ids or not relations:
            return {}
        rows = self._read(
            _bind(
                f"SELECT l.* FROM link_event l WHERE l.target_id IN ({_placeholders(len(ids))}) "
                f"AND l.relation IN ({_placeholders(len(relations))}) AND NOT EXISTS (SELECT 1 "
                "FROM retirement r WHERE r.entry_id = l.source_id)",
                [*ids, *relations],
            )
        )
        grouped: dict[str, list[EntryLink]] = {}
        for event in current_links([_link_event(row) for row in rows]):
            grouped.setdefault(event.target_id, []).append(_entry_link(event))
        return grouped

    # ------------------------------------------------------------------ task state

    def task_graph(self) -> TaskGraph:
        """Every task, every entry a part_of or waits_on link names, and the current
        links between them, from ONE dolt invocation (D15)."""
        linked = "SELECT target_id FROM link_event WHERE relation IN ('part_of', 'waits_on')"
        versions, links, events, retired, memberships = dolt.read_result_sets(
            self.db_path,
            [
                "SELECT e.id, e.kind, e.created_at AS entry_created_at, x.title, x.version, "
                "x.priority FROM entry e JOIN entry_version x ON x.entry_id = e.id WHERE "
                f"(e.kind = 'task' OR e.id IN ({linked})) AND NOT EXISTS (SELECT 1 "
                "FROM entry_version n WHERE n.entry_id = x.entry_id AND n.version > x.version)",
                "SELECT * FROM link_event WHERE relation IN ('part_of', 'waits_on')",
                "SELECT t.* FROM task_event t ORDER BY t.entry_id, t.sequence",
                "SELECT entry_id FROM retirement",
                "SELECT e.entry_id, e.project_id FROM entry_project_event e WHERE e.action = "
                "'add' AND NOT EXISTS (SELECT 1 FROM entry_project_event newer WHERE "
                "newer.entry_id = e.entry_id AND newer.project_id = e.project_id AND "
                "newer.sequence > e.sequence)",
            ],
        )
        events_by: dict[str, list[TaskEvent]] = {}
        for row in events:
            events_by.setdefault(str(row["entry_id"]), []).append(_task_event(row))
        retired_ids = {str(row["entry_id"]) for row in retired}
        projects_by: dict[str, set[str]] = {}
        for row in memberships:
            projects_by.setdefault(str(row["entry_id"]), set()).add(str(row["project_id"]))
        nodes: dict[str, Node] = {}
        for row in versions:
            entry_id = str(row["id"])
            kind = _as_kind(row["kind"])
            state, outcome, claim = task_state(kind, events_by.get(entry_id, []))
            priority = row.get("priority")
            nodes[entry_id] = Node(
                id=entry_id,
                kind=kind,
                title=str(row["title"]),
                version=int(row["version"]),
                priority=int(priority) if priority is not None else None,
                created_at=_as_datetime(row["entry_created_at"]),
                state=state,
                outcome=outcome,
                claim=claim,
                retired=entry_id in retired_ids,
                projects=frozenset(projects_by.get(entry_id, set())),
            )
        graph = TaskGraph(nodes=nodes)
        for event in current_links([_link_event(row) for row in links]):
            if event.source_id in retired_ids:
                continue
            table = graph.parents if event.relation == "part_of" else graph.waits
            table.setdefault(event.source_id, []).append(event.target_id)
        return graph

    def claim(
        self, entry_id: str, base_version: int | None, holder: str | None = None
    ) -> TaskOutcome:
        """Claim a task for this session, atomically under the write lock.

        `holder` is who held the task when the caller read it (`claim_holder` of the claim
        event: its session id, or its harness when it recorded none), or None when it was
        unclaimed. Like `base_version`, it names the state the claim replaces: a task another
        session holds is taken over only when `holder` names that session, and refused as an
        overwrite otherwise. A closed task is left as it is, with a warning naming its state
        (dec-d1krx item 4). Claiming a task this session already holds changes nothing.

        Refused: an entry of another kind (malformed), a stale or missing `base_version`, and
        a held task whose holder the caller did not name (both attempted overwrites).
        """
        with dolt.exclusive(self.db_path):
            entry = self.get_entry(entry_id)
            if entry.kind != "task":
                raise ValidationError(f"{entry_id} is a {entry.kind}; only a task is claimed")
            if entry.state == "closed":
                return TaskOutcome(
                    entry=entry, event=None, changed=False, moved=[],
                    warnings=[f"{entry_id} is closed {entry.outcome}; nothing changed"],
                )  # fmt: skip
            retry = f'claim("{entry_id}", base_version={entry.current.version}'
            held = entry.claim
            if held is not None:
                retry += f", holder={json.dumps(claim_holder(held))}"
            retry += ")"
            if base_version != entry.current.version:
                raise ValidationError(
                    _stale_message(entry_id, base_version, entry.current, retry=retry)
                )
            provenance = sessions.current_provenance()
            if held is not None:
                if _same_session(held, provenance):
                    return TaskOutcome(
                        entry=entry, event=held, changed=False, moved=[], warnings=[]
                    )
                if holder != claim_holder(held):
                    named = f"holder={json.dumps(holder)}" if holder else "no holder"
                    raise ValidationError(
                        f"{entry_id} is claimed by {_claimant(held)} since "
                        f"{held.created_at.isoformat()}, and this call named {named}, so it "
                        f"would take over a claim it did not read; read it with "
                        f'show("{entry_id}"), then retry with {retry}'
                    )
            event = self._task_event(
                entry, "claim", outcome=None, reason=None, provenance=provenance
            )
            self._commit_events([_task_insert(event)], f"claim {entry_id}", event, provenance)
        return self._task_outcome(entry, event)

    def release(self, entry_id: str, reason: str) -> TaskOutcome:
        """Drop the current claim (D11); the task stays not closed. Releasing an unclaimed
        task changes nothing."""
        normal_reason = _required_reason("release", reason)
        with dolt.exclusive(self.db_path):
            entry = self.get_entry(entry_id)
            if entry.claim is None:
                return TaskOutcome(
                    entry=entry, event=None, changed=False, moved=[],
                    warnings=[],
                )  # fmt: skip
            provenance = sessions.current_provenance()
            event = self._task_event(
                entry, "release", outcome=None, reason=normal_reason, provenance=provenance
            )
            self._commit_events([_task_insert(event)], f"release {entry_id}", event, provenance)
        return self._task_outcome(
            entry, event, warnings=_secret_warnings({"reason": normal_reason})
        )

    def reopen(self, entry_id: str, reason: str) -> TaskOutcome:
        """Move a closed task back to open (D11). Links a supersede moved stay moved.
        Reopening an entry that is not closed changes nothing."""
        normal_reason = _required_reason("reopen", reason)
        with dolt.exclusive(self.db_path):
            entry = self.get_entry(entry_id)
            if entry.kind not in WORK_KINDS:
                raise ValidationError(f"{entry_id} is a {entry.kind}; only tasks close")
            if entry.state != "closed":
                return TaskOutcome(
                    entry=entry, event=None, changed=False, moved=[],
                    warnings=[],
                )  # fmt: skip
            provenance = sessions.current_provenance()
            event = self._task_event(
                entry, "reopen", outcome=None, reason=normal_reason, provenance=provenance
            )
            self._commit_events([_task_insert(event)], f"reopen {entry_id}", event, provenance)
        return self._task_outcome(
            entry, event, warnings=_secret_warnings({"reason": normal_reason})
        )

    def close(  # noqa: PLR0912 — one transaction holds the close and all it implies
        self, entry_id: str, outcome: Outcome, summary: str, by: Sequence[str] = ()
    ) -> TaskOutcome:
        """Close a task with an outcome, in one transaction with everything it implies.

        `superseded` needs a nonempty `by`, every id an existing task; every current incoming
        waits_on link is moved onto every replacement, and each replacement gets a
        `supersedes` link to the closed task, which is where `by` is recorded. Closing again
        with the same outcome changes nothing.
        """
        if outcome not in OUTCOMES:
            raise ValidationError(f"outcome must be one of {', '.join(OUTCOMES)}")
        normal_summary = summary.strip()
        if not normal_summary:
            raise ValidationError("close needs a summary: what was done, or why not")
        _require_text_bytes("summary", normal_summary)
        replacements = list(dict.fromkeys(by))
        if outcome == "superseded" and not replacements:
            raise ValidationError(
                'outcome="superseded" needs by=[ids]: the tasks that replace this one'
            )
        if outcome != "superseded" and replacements:
            raise ValidationError('by names replacements and goes with outcome="superseded" only')
        if entry_id in replacements:
            raise ValidationError(f"{entry_id} cannot supersede itself")
        with dolt.exclusive(self.db_path):
            found = self._require_entries([entry_id, *replacements])
            entry = found[entry_id]
            if entry.kind not in WORK_KINDS:
                raise ValidationError(f"{entry_id} is a {entry.kind}; only tasks close")
            not_work = [item for item in replacements if found[item].kind not in WORK_KINDS]
            if not_work:
                raise ValidationError(
                    f"{', '.join(not_work)} is not a task; only a task can replace one"
                )
            if entry.state == "closed":
                return TaskOutcome(
                    entry=entry, event=None, changed=False, moved=[],
                    warnings=[f"{entry_id} is closed {entry.outcome}; nothing changed"],
                )  # fmt: skip
            provenance = sessions.current_provenance()
            graph = self.task_graph()
            event = self._task_event(
                entry, "close", outcome=outcome, reason=normal_summary, provenance=provenance
            )
            statements = [_task_insert(event)]
            moved: list[MovedLink] = []
            if outcome == "superseded":
                statements += self._supersede_statements(
                    entry_id,
                    replacements=replacements,
                    found=found,
                    summary=normal_summary,
                    moment=event.created_at,
                    provenance=provenance,
                )
                for source in graph.dependents(entry_id):
                    moved += [
                        MovedLink(source_id=source, from_id=entry_id, to_id=item)
                        for item in replacements
                        if item != source
                    ]
            if moved:
                for item in moved:
                    targets = graph.waits.setdefault(item.source_id, [])
                    if entry_id in targets:
                        targets.remove(entry_id)
                    if item.to_id not in targets:
                        targets.append(item.to_id)
                _refuse_cycles_from(graph, sorted({item.source_id for item in moved}))
            warnings = _secret_warnings({"summary": normal_summary})
            self._commit_events(statements, f"close {entry_id} {outcome}", event, provenance)
        return self._task_outcome(entry, event, moved=moved, warnings=warnings)

    def retire_entry(self, entry_id: str, reason: str) -> RetireOutcome:
        """Mark an entry no longer true without replacing it. Retiring twice keeps the first
        reason and changes nothing."""
        normal_reason = (reason or "").strip()
        if not normal_reason:
            raise ValidationError("retire needs a reason: why the entry is no longer true")
        _require_text_bytes("reason", normal_reason)
        with dolt.exclusive(self.db_path):
            entry = self.get_entry(entry_id)
            if entry.kind not in KNOWLEDGE_KINDS:
                raise ValidationError(
                    f"retire takes a note, preference or skill; {entry_id} is a {entry.kind}"
                )
            if entry.retired:
                return RetireOutcome(
                    entry_id=entry_id, reason=str(entry.retired_reason), already_retired=True
                )
            provenance = sessions.current_provenance()
            moment = _now()
            self._execute_script(
                [
                    "START TRANSACTION",
                    _insert(
                        "retirement",
                        {
                            "entry_id": entry_id,
                            "retired_at": moment,
                            "reason": normal_reason,
                            "author": current_author(),
                            "harness": provenance.harness,
                            "session_id": provenance.session_id,
                        },
                    ),
                    *_session_insert(provenance, moment),
                    f"CALL DOLT_COMMIT('-A','-m',{dolt.sql_literal(f'retire {entry_id}')})",
                ]
            )
        return RetireOutcome(
            entry_id=entry_id,
            reason=normal_reason,
            already_retired=False,
            warnings=_secret_warnings({"reason": normal_reason}),
        )

    def _supersede_statements(
        self,
        entry_id: str,
        *,
        replacements: Sequence[str],
        found: dict[str, Entry],
        summary: str,
        moment: datetime,
        provenance: Provenance,
    ) -> list[str]:
        """Move every current incoming waits_on onto every replacement; record `by` as
        supersedes links from each replacement."""
        statements: list[str] = []
        incoming = self.incoming_links([entry_id], ["waits_on"]).get(entry_id, [])
        reason = f"{entry_id} was closed superseded by {', '.join(replacements)}"
        for link in incoming:
            latest = self._latest_link(link.source_id, "waits_on", entry_id)
            statements.append(
                _link_insert(
                    self._link_event(
                        link.source_id, "waits_on", entry_id, action="remove",
                        sequence=(latest.sequence + 1) if latest else 0, target_version=None,
                        reason=reason, provenance=provenance, moment=moment,
                    )
                )
            )  # fmt: skip
            for item in replacements:
                if item == link.source_id:
                    continue
                previous = self._latest_link(link.source_id, "waits_on", item)
                if previous is not None and previous.action == "add":
                    continue
                statements.append(
                    _link_insert(
                        self._link_event(
                            link.source_id, "waits_on", item, action="add",
                            sequence=(previous.sequence + 1) if previous else 0,
                            target_version=found[item].current.version, reason=reason,
                            provenance=provenance, moment=moment,
                        )
                    )
                )  # fmt: skip
        for item in replacements:
            previous = self._latest_link(item, "supersedes", entry_id)
            if previous is not None and previous.action == "add":
                continue
            statements.append(
                _link_insert(
                    self._link_event(
                        item, "supersedes", entry_id, action="add",
                        sequence=(previous.sequence + 1) if previous else 0,
                        target_version=found[entry_id].current.version, reason=summary,
                        provenance=provenance, moment=moment,
                    )
                )
            )  # fmt: skip
        return statements

    def _task_outcome(
        self,
        before: Entry,
        event: TaskEvent,
        moved: Sequence[MovedLink] = (),
        warnings: Sequence[str] = (),
    ) -> TaskOutcome:
        """The entry as it stands after `event`, computed without a read after the commit."""
        events = [*before.task_events, event]
        state, outcome, claim = task_state(before.kind, events)
        after = before.model_copy(
            update={"task_events": events, "state": state, "outcome": outcome, "claim": claim}
        )
        return TaskOutcome(
            entry=after,
            event=event,
            changed=True,
            moved=list(moved),
            warnings=list(warnings),
        )

    def _task_event(
        self,
        entry: Entry,
        kind: TaskEventKind,
        *,
        outcome: Outcome | None,
        reason: str | None,
        provenance: Provenance | None = None,
    ) -> TaskEvent:
        provenance = provenance or sessions.current_provenance()
        sequence = max((event.sequence for event in entry.task_events), default=-1) + 1
        return TaskEvent(
            entry_id=entry.id,
            sequence=sequence,
            event=kind,
            outcome=outcome,
            reason=reason,
            author=current_author(),
            harness=provenance.harness,
            session_id=provenance.session_id,
            created_at=_now(),
        )

    def _link_event(
        self,
        source_id: str,
        relation: LinkRelation,
        target_id: str,
        *,
        action: LinkAction,
        sequence: int,
        target_version: int | None,
        reason: str | None,
        provenance: Provenance | None = None,
        moment: datetime | None = None,
    ) -> LinkEvent:
        provenance = provenance or sessions.current_provenance()
        return LinkEvent(
            source_id=source_id,
            relation=relation,
            target_id=target_id,
            sequence=sequence,
            action=action,
            target_version=target_version,
            reason=reason,
            author=current_author(),
            harness=provenance.harness,
            session_id=provenance.session_id,
            created_at=moment or _now(),
        )

    def _latest_link(
        self, source_id: str, relation: LinkRelation, target_id: str
    ) -> LinkEvent | None:
        rows = self._read(
            _bind(
                "SELECT * FROM link_event WHERE source_id = ? AND relation = ? AND target_id = ? "
                "ORDER BY sequence DESC LIMIT 1",
                [source_id, relation, target_id],
            )
        )
        return _link_event(rows[0]) if rows else None

    def _require_entries(self, entry_ids: Sequence[str]) -> dict[str, Entry]:
        found = self.get_entries(entry_ids)
        missing = [item for item in dict.fromkeys(entry_ids) if item not in found]
        if missing:
            raise ValidationError(f"no entry has id {', '.join(missing)}")
        return found

    def _commit_events(
        self,
        statements: Sequence[str],
        message: str,
        event: LinkEvent | TaskEvent,
        provenance: Provenance,
    ) -> None:
        """Commit an event write with its session row; on failure, re-read whether the event
        row landed."""
        try:
            self._execute_script(
                [
                    "START TRANSACTION",
                    *statements,
                    *_session_insert(provenance, event.created_at),
                    f"CALL DOLT_COMMIT('-A','-m',{dolt.sql_literal(message)})",
                ]
            )
        except DoltError:
            if isinstance(event, TaskEvent):
                check = _bind(
                    "SELECT created_at FROM task_event WHERE entry_id = ? AND sequence = ?",
                    [event.entry_id, event.sequence],
                )
            else:
                check = _bind(
                    "SELECT created_at FROM link_event WHERE source_id = ? AND relation = ? AND "
                    "target_id = ? AND sequence = ?",
                    [event.source_id, event.relation, event.target_id, event.sequence],
                )
            stored = self._read(check)
            if stored and _as_datetime(stored[0]["created_at"]) == event.created_at:
                return
            raise

    # ------------------------------------------------------------------ projects

    def set_entry_projects(
        self, entry_id: str, add: Sequence[str], remove: Sequence[str], reason: str
    ) -> SetProjectsOutcome:
        """Append effective membership changes in one transaction; return current state."""
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
            overlap = sorted({ref.id for ref in add_refs} & {ref.id for ref in remove_refs})
            if overlap:
                names = ", ".join(ref.name for ref in add_refs if ref.id in overlap)
                raise ValidationError(f"{names} is present in both add and remove")
            entry = self.get_entry(entry_id)
            current = {ref.id: ref for ref in entry.projects}
            effective_add = [ref for ref in add_refs if ref.id not in current]
            effective_remove = [ref for ref in remove_refs if ref.id in current]
            provenance = sessions.current_provenance()
            author = current_author()
            moment = _now()
            wanted = [
                *(pid for pid in current if pid not in {ref.id for ref in effective_remove}),
                *(ref.id for ref in effective_add),
            ]
            statements = self._membership_changes(
                entry_id,
                wanted=wanted,
                reason=normal_reason,
                author=author,
                provenance=provenance,
                created_at=moment,
            )
            if statements:
                self._execute_script(
                    [
                        "START TRANSACTION",
                        *statements,
                        *_session_insert(provenance, moment),
                        f"CALL DOLT_COMMIT('-A','-m',{dolt.sql_literal(f'projects {entry_id}')})",
                    ]
                )
            known = {**current, **{ref.id: ref for ref in add_refs}}
            resulting = sorted((known[pid] for pid in wanted), key=lambda ref: (ref.name, ref.id))
            changes = [
                ProjectMembershipChange(
                    project=ref,
                    action=action,
                    reason=normal_reason,
                    author=author,
                    created_at=moment,
                )
                for action, group in (("add", effective_add), ("remove", effective_remove))
                for ref in group
            ]
            return SetProjectsOutcome(
                entry_id=entry_id,
                changed=changes,
                unchanged_add=[ref for ref in add_refs if ref.id in current],
                unchanged_remove=[ref for ref in remove_refs if ref.id not in current],
                projects=resulting,
                warnings=_secret_warnings({"reason": normal_reason}) if statements else [],
            )

    # ------------------------------------------------------------------ comments

    def add_comment(
        self,
        entry_id: str,
        body: str,
        addresses: Sequence[str] = (),
        verdict: Verdict | None = None,
    ) -> CommentOutcome:
        """Append a comment beside an entry. It writes no version, so it never makes an
        editor's base_version stale.

        A comment with `addresses` is a response: it closes those comments. Each must be an
        open comment on the same entry. On a task the comment pins the current version
        of every descendant, so a later `changes_since_comment` can list exactly what moved.

        Raises:
            ValidationError: empty text, an unknown verdict, or an `addresses` id that is not
                an open comment on this entry.
            RecordNotFoundError: no entry has this id.
        """
        text = body.strip()
        if not text and verdict is None:
            raise ValidationError("a comment needs text")
        if verdict is not None and verdict not in VERDICTS:
            raise ValidationError(f"verdict must be one of {', '.join(VERDICTS)}")
        _require_text_bytes("comment", text)
        targets = list(dict.fromkeys(addresses))
        with dolt.exclusive(self.db_path):
            entry = self.get_entry(entry_id)
            if targets:
                comment_rows, addressed_rows = dolt.read_result_sets(
                    self.db_path,
                    [
                        _bind(
                            "SELECT id, entry_id FROM comment WHERE id IN "
                            f"({_placeholders(len(targets))})",
                            targets,
                        ),
                        _bind(
                            "SELECT comment_id, addressed_id FROM comment_addresses WHERE "
                            f"addressed_id IN ({_placeholders(len(targets))}) OR comment_id IN "
                            f"({_placeholders(len(targets))})",
                            [*targets, *targets],
                        ),
                    ],
                )
                on_entry = {
                    str(row["id"]) for row in comment_rows if str(row["entry_id"]) == entry_id
                }
                closed = {
                    str(row["addressed_id"]): str(row["comment_id"])
                    for row in addressed_rows
                    if str(row["addressed_id"]) in targets
                }
                responses = {
                    str(row["comment_id"])
                    for row in addressed_rows
                    if str(row["comment_id"]) in targets
                }
                for target in targets:
                    if target not in on_entry:
                        raise ValidationError(
                            f"{target} is not a comment on {entry_id}; addresses names open "
                            "comments on the same entry"
                        )
                    if target in responses:
                        raise ValidationError(
                            f"{target} is a response and is never open; address the comment it "
                            "responds to only if that is still open"
                        )
                    if target in closed:
                        raise ValidationError(
                            f"{target} was already addressed by {closed[target]}; addresses "
                            "names open comments only"
                        )
            pins: dict[str, int] = {}
            if entry.kind in WORK_KINDS:
                graph = self.task_graph()
                pins = {item: graph.nodes[item].version for item in graph.descendants(entry_id)}
            provenance = sessions.current_provenance()
            moment = _now()
            comment_id = self._unused_comment_id()
            comment = Comment(
                id=comment_id,
                entry_id=entry_id,
                entry_version=entry.current.version,
                body=text,
                verdict=verdict,
                addresses=targets,
                addressed_by=[],
                author=current_author(),
                harness=provenance.harness,
                session_id=provenance.session_id,
                created_at=moment,
            )
            statements = [
                _insert(
                    "comment",
                    {
                        "id": comment_id,
                        "entry_id": entry_id,
                        "entry_version": comment.entry_version,
                        "body": text,
                        "verdict": verdict,
                        "author": comment.author,
                        "harness": comment.harness,
                        "session_id": comment.session_id,
                        "created_at": moment,
                    },
                ),
                *(
                    _insert("comment_addresses", {"comment_id": comment_id, "addressed_id": item})
                    for item in targets
                ),
                *(
                    _insert(
                        "comment_pin",
                        {"comment_id": comment_id, "entry_id": item, "version": version},
                    )
                    for item, version in pins.items()
                ),
                *_session_insert(provenance, moment),
                f"CALL DOLT_COMMIT('-A','-m',{dolt.sql_literal(f'comment {comment_id}')})",
            ]
            try:
                self._execute_script(["START TRANSACTION", *statements])
            except DoltError:
                stored = self._read(
                    _bind("SELECT created_at FROM comment WHERE id = ?", [comment_id])
                )
                if not (stored and _as_datetime(stored[0]["created_at"]) == moment):
                    raise
        return CommentOutcome(
            comment=comment, pinned=pins, warnings=_secret_warnings({"comment": text})
        )

    def get_comments(self, entry_id: str, limit: int = 5, offset: int = 0) -> CommentPage:
        """The newest `limit` comments on an entry after skipping `offset`, newest first, with
        the entry's total and open comment counts, in one read."""
        rows, addresses = dolt.read_result_sets(
            self.db_path,
            [
                _bind(
                    "SELECT * FROM comment WHERE entry_id = ? ORDER BY created_at DESC, id DESC",
                    [entry_id],
                ),
                _bind(
                    "SELECT a.comment_id, a.addressed_id FROM comment_addresses a JOIN comment c "
                    "ON c.id = a.comment_id WHERE c.entry_id = ? ORDER BY a.comment_id",
                    [entry_id],
                ),
            ],
        )
        comments = _comments(rows, addresses)
        return CommentPage(
            entry_id=entry_id,
            comments=comments[offset : offset + max(limit, 0)],
            total=len(comments),
            open=sum(1 for item in comments if _is_open(item)),
            offset=offset,
        )

    def get_comment(self, comment_id: str) -> Comment:
        """One comment.

        Raises:
            RecordNotFoundError: no comment has this id.
        """
        rows, addresses = dolt.read_result_sets(
            self.db_path,
            [
                _bind("SELECT * FROM comment WHERE id = ?", [comment_id]),
                _bind(
                    "SELECT comment_id, addressed_id FROM comment_addresses WHERE comment_id = ? "
                    "OR addressed_id = ?",
                    [comment_id, comment_id],
                ),
            ],
        )
        found = _comments(rows, addresses)
        if not found:
            raise RecordNotFoundError(f"no comment has id {comment_id!r}")
        return found[0]

    def open_comment_counts(self, entry_ids: Sequence[str]) -> dict[str, int]:
        """How many open comments each entry has, in one read. Absent means none."""
        ids = list(dict.fromkeys(entry_ids))
        if not ids:
            return {}
        rows = self._read(
            _bind(
                "SELECT c.entry_id, COUNT(*) AS open_count FROM comment c WHERE c.entry_id IN "
                f"({_placeholders(len(ids))}) AND NOT EXISTS (SELECT 1 FROM comment_addresses a "
                "WHERE a.addressed_id = c.id) AND NOT EXISTS (SELECT 1 FROM comment_addresses r "
                "WHERE r.comment_id = c.id) GROUP BY c.entry_id",
                ids,
            )
        )
        return {str(row["entry_id"]): int(row["open_count"]) for row in rows}

    def changes_since_comment(self, comment_id: str) -> SinceComment:
        """The entries whose version moved, and the link events, under a commented entry
        after the comment: the entry itself, every descendant it pinned, and every current
        descendant it did not pin (new since)."""
        comment = self.get_comment(comment_id)
        pin_rows = self._read(
            _bind("SELECT entry_id, version FROM comment_pin WHERE comment_id = ?", [comment_id])
        )
        pins = {str(row["entry_id"]): int(row["version"]) for row in pin_rows}
        pins[comment.entry_id] = comment.entry_version
        entry = self.get_entry(comment.entry_id)
        current_descendants: list[str] = []
        if entry.kind in WORK_KINDS:
            current_descendants = self.task_graph().descendants(comment.entry_id)
        scope = list(dict.fromkeys([comment.entry_id, *pins, *current_descendants]))
        found = self.get_entries(scope)
        changed: list[VersionChange] = []
        unchanged: list[str] = []
        for item in scope:
            now = found.get(item)
            if now is None:
                continue
            pinned = pins.get(item)
            if pinned is None or pinned != now.current.version:
                changed.append(
                    VersionChange(
                        entry_id=item,
                        title=now.current.title,
                        pinned=pinned,
                        current=now.current.version,
                    )
                )
            else:
                unchanged.append(item)
        links = [
            event for event in self.link_events(scope) if event.created_at > comment.created_at
        ]
        return SinceComment(comment=comment, changed=changed, unchanged=unchanged, links=links)

    def _unused_comment_id(self) -> str:
        for candidate in _id_candidates(COMMENT_ID_PREFIX):
            if not self._read(_bind("SELECT id FROM comment WHERE id = ?", [candidate])):
                return candidate
        raise DoltError("failed to generate an unused comment id")

    # ---------------------------------------------------------------- internals

    def _commit(
        self,
        statements: Sequence[str],
        *,
        message: str,
        entry_id: str,
        version: int | None,
        created_at: datetime,
        tags: Sequence[str],
    ) -> None:
        """Run one write transaction; classify a failure by re-reading, never by Dolt's text.

        `version` names the version row this write inserts, if any, which is what a re-read
        looks for to tell a write that landed before reporting failure.
        """
        try:
            self._execute_script(
                [
                    "START TRANSACTION",
                    *statements,
                    f"CALL DOLT_COMMIT('-A','-m',{dolt.sql_literal(message)})",
                ]
            )
        except DoltError:
            if version is not None:
                stored = self._read(
                    _bind(
                        "SELECT created_at FROM entry_version WHERE entry_id = ? AND version = ?",
                        [entry_id, version],
                    )
                )
                if stored and _as_datetime(stored[0]["created_at"]) == created_at.replace(
                    tzinfo=UTC
                ):
                    return
            equal = self._collation_equal_pairs(tags)
            if equal:
                named = ", ".join(f"{a!r} and {b!r}" for a, b in equal)
                raise ValidationError(
                    f"tags {named} are one tag to the store, which compares tags ignoring case "
                    "and accents; keep one"
                ) from None
            raise


# ------------------------------------------------------------------ derived state


def current_links(events: Iterator[LinkEvent] | Sequence[LinkEvent]) -> list[LinkEvent]:
    """The add events whose (source, relation, target) has no later event, in write order."""
    latest: dict[tuple[str, str, str], LinkEvent] = {}
    for event in events:
        key = (event.source_id, event.relation, event.target_id)
        if key not in latest or event.sequence > latest[key].sequence:
            latest[key] = event
    return sorted(
        (event for event in latest.values() if event.action == "add"),
        key=lambda event: (event.created_at, event.relation, event.target_id),
    )


def task_state(
    kind: Kind, events: Sequence[TaskEvent]
) -> tuple[TaskState | None, Outcome | None, TaskEvent | None]:
    """State, outcome and held claim from a task's events, in sequence order."""
    if kind not in WORK_KINDS:
        return None, None, None
    state: TaskState = "open"
    outcome: Outcome | None = None
    claim: TaskEvent | None = None
    for event in sorted(events, key=lambda item: item.sequence):
        if event.event == "claim":
            state, claim = "claimed", event
        elif event.event == "release":
            state, claim = "open", None
        elif event.event == "close":
            state, outcome, claim = "closed", event.outcome, None
        elif event.event == "reopen":
            state, outcome, claim = "open", None, None
    return state, outcome, claim


# ------------------------------------------------------------------ helpers


def _validated_title(title: str | None) -> str:
    normal = (title or "").strip()
    if not normal:
        raise ValidationError("title is empty: an entry needs a one-line title")
    _require_length("title", normal, TITLE_CHARS)
    return normal


def _validated_priority(kind: Kind, priority: int | None) -> int | None:
    if priority is None:
        return None
    if kind not in WORK_KINDS:
        raise ValidationError(f"priority is a task field; a {kind} has none")
    low, high = PRIORITY_RANGE
    if isinstance(priority, bool) or not low <= priority <= high:
        raise ValidationError(f"priority must be an integer from {low} to {high}; 0 is highest")
    return priority


def _validate_content(
    body: str, artifacts: Sequence[ArtifactRef], code: Sequence[CodeCapture]
) -> None:
    if "\x00" in body:
        raise ValidationError("body contains a NUL byte")
    for n, artifact in enumerate(artifacts):
        _require_text_bytes(f"artifact[{n}].location", artifact.location)
        _require_text_bytes(f"artifact[{n}].notes", artifact.notes)
        if artifact.digest is not None and _DIGEST.fullmatch(artifact.digest) is None:
            raise ValidationError(
                f"artifact[{n}].digest {artifact.digest!r} is malformed: use "
                "'sha256:<64 lowercase hex>' or null"
            )
    for item in code:
        _require_length("code repo_path", item.repo_path, PATH_CHARS)
    repos = [item.repo_path for item in code]
    if len(set(repos)) != len(repos):
        raise ValidationError("code lists the same repo_path twice")


def _merged(kind: Kind, current: EntryVersion, draft: EntryDraft) -> EntryVersion:
    """The next version's content: each omitted field inherits, an empty list clears (D14)."""
    title = current.title if draft.title is None else _validated_title(draft.title)
    body = current.body if draft.body is None else draft.body
    tags = current.tags if draft.tags is None else _normalise_tags(draft.tags)
    if draft.value is None:
        value = current.value
    else:
        value = draft.value or None
    artifacts = current.artifacts if draft.artifacts is None else list(draft.artifacts)
    code = (
        current.code if draft.code is None else sorted(draft.code, key=lambda item: item.repo_path)
    )
    priority = (
        _validated_priority(kind, draft.priority)
        if draft.priority is not None or draft.priority_given
        else current.priority
    )
    _validate_content(body, artifacts, code)
    _value_text(value)
    return current.model_copy(
        update={
            "title": title,
            "body": body,
            "tags": tags,
            "value": value,
            "artifacts": artifacts,
            "code": code,
            "priority": priority,
        }
    )


def _content(version: EntryVersion) -> tuple[Any, ...]:
    return (
        version.title,
        version.body,
        tuple(version.tags),
        json.dumps(version.value, sort_keys=True) if version.value is not None else None,
        tuple((a.location, a.digest, a.notes) for a in version.artifacts),
        tuple((c.repo_path, c.head_sha, c.captured_sha, c.dirty) for c in version.code),
        version.priority,
    )


def _stale_message(entry_id: str, given: int | None, current: EntryVersion, retry: str) -> str:
    """The refusal of a stale or missing base_version, with the call to retry."""
    session = f", session {current.session_id}" if current.session_id else ""
    who = (
        f"{entry_id} is at version {current.version}, written by {current.author} "
        f"({current.harness}{session}) at {current.created_at.isoformat()}"
    )
    advice = f'read it with show("{entry_id}"), then retry with {retry}'
    if given is None:
        return f"this call needs base_version: {who}; {advice}"
    return f"base_version={given} is stale and would overwrite a newer version: {who}; {advice}"


def _retry_call(entry: Entry, draft: EntryDraft) -> str:
    """The same call against the current version."""
    version = entry.current.version
    if draft.priority_given:
        return (
            f'set_priority("{entry.id}", base_version={version}, priority=<priority>, reason="...")'
        )
    return (
        f'revise_{entry.kind}("{entry.id}", base_version={version}, reason="...", '
        "<the fields you change>)"
    )


NEW_TASK = "the new task"


def _refuse_cycles(graph: TaskGraph, relations: Sequence[tuple[LinkRelation, str]]) -> None:
    """Refuse a new task whose parent and waits_on links would close a must-finish-first
    cycle (D8): no task on it could ever become ready, so the request is malformed. A new
    task has no incoming links, but its parent's ancestors can wait on its own dependencies'
    descendants, so creation can close one."""
    graph.nodes[NEW_TASK] = Node(
        id=NEW_TASK,
        kind="task",
        title=NEW_TASK,
        version=1,
        priority=None,
        created_at=_now(),
        state="open",
        outcome=None,
        claim=None,
        retired=False,
    )
    for relation, target in relations:
        table = graph.parents if relation == "part_of" else graph.waits
        table.setdefault(NEW_TASK, []).append(target)
    _refuse_cycle_through(graph, NEW_TASK)


def _refuse_cycles_from(graph: TaskGraph, changed: Sequence[str]) -> None:
    """Refuse when a changed link closed a cycle anywhere it reaches. A task inherits its
    ancestors' waits_on targets, so a link on a task also changes what each of its
    descendants needs, and a new cycle can pass through any of them rather than the task."""
    reached = list(
        dict.fromkeys(item for root in changed for item in [root, *graph.descendants(root)])
    )
    for item in reached:
        _refuse_cycle_through(graph, item)


def _refuse_cycle_through(graph: TaskGraph, entry_id: str) -> None:
    cycle = graph.cycle_through(entry_id)
    if cycle is not None:
        raise ValidationError(
            "these links would make a dependency cycle, so none of its tasks could become "
            "ready: " + " -> ".join(cycle)
        )


def _require_relation_kinds(
    relation: LinkRelation, source_kind: Kind, target_kind: Kind, source: str, target: str
) -> None:
    """Work relations join two tasks; evidence relations join two notes, preferences or
    skills. Anything else is malformed."""
    allowed = WORK_KINDS if relation in WORK_RELATIONS else KNOWLEDGE_KINDS
    for which, entry, kind in (("source", source, source_kind), ("target", target, target_kind)):
        if kind not in allowed:
            raise ValidationError(
                f"{relation} joins {' or '.join(allowed)} entries; the {which} {entry} is a {kind}"
            )


def _scanned_fields(version: EntryVersion) -> dict[str, str]:
    value_text = _value_text(version.value)
    return {
        "title": version.title,
        "body": version.body,
        **({"reason": version.reason} if version.reason else {}),
        **({"value": value_text} if value_text is not None else {}),
        **{
            f"artifact[{n}].{part}": text
            for n, artifact in enumerate(version.artifacts)
            for part, text in (("location", artifact.location), ("notes", artifact.notes))
        },
    }


def _version_inserts(version: EntryVersion) -> list[str]:
    entry_id, number = version.entry_id, version.version
    return [
        _insert(
            "entry_version",
            {
                "entry_id": entry_id,
                "version": number,
                "title": version.title,
                "body": version.body,
                "value": _value_text(version.value),
                "priority": version.priority,
                "reason": version.reason,
                "author": version.author,
                "harness": version.harness,
                "session_id": version.session_id,
                "created_at": version.created_at,
            },
        ),
        *(
            _insert("version_tag", {"entry_id": entry_id, "version": number, "tag": tag})
            for tag in version.tags
        ),
        *(
            _insert(
                "version_artifact",
                {
                    "entry_id": entry_id,
                    "version": number,
                    "position": position,
                    "location": artifact.location,
                    "digest": artifact.digest,
                    "notes": artifact.notes,
                },
            )
            for position, artifact in enumerate(version.artifacts)
        ),
        *(
            _insert(
                "version_code",
                {
                    "entry_id": entry_id,
                    "version": number,
                    "repo_path": capture.repo_path,
                    "head_sha": capture.head_sha,
                    "captured_sha": capture.captured_sha,
                    "dirty": int(capture.dirty),
                },
            )
            for capture in version.code
        ),
    ]


def _link_insert(event: LinkEvent) -> str:
    return _insert(
        "link_event",
        {
            "source_id": event.source_id,
            "relation": event.relation,
            "target_id": event.target_id,
            "sequence": event.sequence,
            "action": event.action,
            "target_version": event.target_version,
            "reason": event.reason,
            "author": event.author,
            "harness": event.harness,
            "session_id": event.session_id,
            "created_at": event.created_at,
        },
    )


def _membership_insert(
    entry_id: str,
    project_id: str,
    *,
    sequence: int,
    action: str,
    reason: str | None,
    author: str,
    provenance: Provenance,
    created_at: datetime,
) -> str:
    return _insert(
        "entry_project_event",
        {
            "entry_id": entry_id,
            "project_id": project_id,
            "sequence": sequence,
            "action": action,
            "reason": reason,
            "author": author,
            "harness": provenance.harness,
            "session_id": provenance.session_id,
            "created_at": created_at,
        },
    )


def _id_candidates(prefix: str) -> Iterator[str]:
    """Ten five-character candidates, then ten six-character candidates."""
    for length in ID_LENGTHS:
        for _attempt in range(ID_ATTEMPTS_PER_LENGTH):
            yield f"{prefix}-" + "".join(secrets.choice(ID_ALPHABET) for _ in range(length))


def _group[T](
    rows: Sequence[dict[str, Any]],
    build: Any,
    key: Any = None,
) -> dict[Any, list[T]]:
    grouped: dict[Any, list[T]] = {}
    for row in rows:
        grouped.setdefault(key(row) if key is not None else str(row["entry_id"]), []).append(
            build(row)
        )
    return grouped


def _version_key(row: dict[str, Any]) -> tuple[str, int]:
    return str(row["entry_id"]), int(row["version"])


def _artifact(row: dict[str, Any]) -> ArtifactRef:
    return ArtifactRef(
        location=str(row["location"]),
        digest=_optional_str(row.get("digest")),
        notes=str(row.get("notes", "")),
    )


def _code(row: dict[str, Any]) -> CodeCapture:
    return CodeCapture(
        repo_path=str(row["repo_path"]),
        head_sha=_optional_str(row.get("head_sha")),
        captured_sha=str(row["captured_sha"]),
        dirty=_as_bool(row["dirty"]),
    )


def _version(
    row: dict[str, Any],
    tags: list[str],
    artifacts: list[ArtifactRef],
    code: list[CodeCapture],
) -> EntryVersion:
    priority = row.get("priority")
    return EntryVersion(
        entry_id=str(row["entry_id"]),
        version=int(row["version"]),
        title=str(row["title"]),
        body=str(row.get("body", "")),
        tags=sorted(tags),
        value=_as_value(row.get("value")),
        priority=int(priority) if priority is not None else None,
        artifacts=artifacts,
        code=code,
        reason=_optional_str(row.get("reason")),
        author=str(row["author"]),
        harness=str(row["harness"]),
        session_id=_optional_str(row.get("session_id")),
        created_at=_as_datetime(row["created_at"]),
    )


def _link_event(row: dict[str, Any]) -> LinkEvent:
    target_version = row.get("target_version")
    return LinkEvent(
        source_id=str(row["source_id"]),
        relation=_as_relation(row["relation"]),
        target_id=str(row["target_id"]),
        sequence=int(row["sequence"]),
        action=_as_link_action(row["action"]),
        target_version=int(target_version) if target_version is not None else None,
        reason=_optional_str(row.get("reason")),
        author=str(row["author"]),
        harness=str(row["harness"]),
        session_id=_optional_str(row.get("session_id")),
        created_at=_as_datetime(row["created_at"]),
    )


def _entry_link(event: LinkEvent) -> EntryLink:
    return EntryLink(
        source_id=event.source_id,
        relation=event.relation,
        target_id=event.target_id,
        target_version=event.target_version,
        reason=event.reason,
        author=event.author,
        harness=event.harness,
        session_id=event.session_id,
        created_at=event.created_at,
    )


def _task_event(row: dict[str, Any]) -> TaskEvent:
    return TaskEvent(
        entry_id=str(row["entry_id"]),
        sequence=int(row["sequence"]),
        event=_as_task_event(row["event"]),
        outcome=_as_outcome(row.get("outcome")),
        reason=_optional_str(row.get("reason")),
        author=str(row["author"]),
        harness=str(row["harness"]),
        session_id=_optional_str(row.get("session_id")),
        created_at=_as_datetime(row["created_at"]),
    )


def _as_kind(value: Any) -> Kind:
    for kind in KINDS:
        if kind == str(value):
            return kind
    raise DoltError(f"entry has unknown kind {value!r}")


def _as_relation(value: Any) -> LinkRelation:
    for relation in LINK_RELATIONS:
        if relation == str(value):
            return relation
    raise DoltError(f"link has unknown relation {value!r}")


def _as_link_action(value: Any) -> LinkAction:
    for action in LINK_ACTIONS:
        if action == str(value):
            return action
    raise DoltError(f"link event has unknown action {value!r}")


def _as_task_event(value: Any) -> TaskEventKind:
    for event in TASK_EVENTS:
        if event == str(value):
            return event
    raise DoltError(f"task event has unknown kind {value!r}")


def _as_outcome(value: Any) -> Outcome | None:
    if value is None:
        return None
    for outcome in OUTCOMES:
        if outcome == str(value):
            return outcome
    raise DoltError(f"task event has unknown outcome {value!r}")


def _task_insert(event: TaskEvent) -> str:
    return _insert(
        "task_event",
        {
            "entry_id": event.entry_id,
            "sequence": event.sequence,
            "event": event.event,
            "outcome": event.outcome,
            "reason": event.reason,
            "author": event.author,
            "harness": event.harness,
            "session_id": event.session_id,
            "created_at": event.created_at,
        },
    )


def _required_reason(operation: str, reason: str) -> str:
    normal = (reason or "").strip()
    if not normal:
        raise ValidationError(f"{operation} needs a reason")
    _require_text_bytes("reason", normal)
    return normal


def _same_session(event: TaskEvent, provenance: Provenance) -> bool:
    """Whether a claim was made by the session now writing. Two writes with no session id
    from the same harness count as one claimant, because nothing tells them apart."""
    return (event.harness, event.session_id) == (provenance.harness, provenance.session_id)


def claim_holder(event: TaskEvent) -> str:
    """How a claim's holder is named to `claim`: its session id, or its harness when the
    claim recorded no session."""
    return event.session_id or event.harness


def _claimant(event: TaskEvent) -> str:
    session = f"session {event.session_id}" if event.session_id else "no session id"
    return f"{event.author} ({event.harness}, {session})"


def _comments(rows: Sequence[dict[str, Any]], addresses: Sequence[dict[str, Any]]) -> list[Comment]:
    """Comments in row order with what each addresses and what addressed it."""
    addresses_by: dict[str, list[str]] = {}
    addressed_by: dict[str, list[str]] = {}
    for row in addresses:
        source, target = str(row["comment_id"]), str(row["addressed_id"])
        addresses_by.setdefault(source, []).append(target)
        addressed_by.setdefault(target, []).append(source)
    return [
        Comment(
            id=str(row["id"]),
            entry_id=str(row["entry_id"]),
            entry_version=int(row["entry_version"]),
            body=str(row.get("body", "")),
            verdict=_as_verdict(row.get("verdict")),
            addresses=sorted(addresses_by.get(str(row["id"]), [])),
            addressed_by=sorted(addressed_by.get(str(row["id"]), [])),
            author=str(row["author"]),
            harness=str(row["harness"]),
            session_id=_optional_str(row.get("session_id")),
            created_at=_as_datetime(row["created_at"]),
        )
        for row in rows
    ]


def _is_open(comment: Comment) -> bool:
    """Open: not a response, and no comment has addressed it."""
    return not comment.addresses and not comment.addressed_by


def is_open_comment(comment: Comment) -> bool:
    """Whether a comment still needs a response."""
    return _is_open(comment)


def _as_verdict(value: Any) -> Verdict | None:
    if value is None:
        return None
    for verdict in VERDICTS:
        if verdict == str(value):
            return verdict
    raise DoltError(f"comment has unknown verdict {value!r}")


def unchanged_responses(comments: Sequence[Comment]) -> set[str]:
    """Responses written against the same entry version as a comment they address: the
    feedback was answered without a new version (shown as "addressed with no new version")."""
    versions = {item.id: item.entry_version for item in comments}
    return {
        item.id
        for item in comments
        if any(versions.get(target) == item.entry_version for target in item.addresses)
    }


def _secret_warnings(fields: dict[str, str]) -> list[str]:
    """Possible secrets in free-text fields an event or comment carries, as warnings. A
    finding never stops the write."""
    return [
        f"possible secret: {finding.detector} in {finding.field} line {finding.line}; tell the "
        "person, because removing it needs a manual database-history rewrite"
        for finding in scan_for_secrets({k: v for k, v in fields.items() if v})
    ]
