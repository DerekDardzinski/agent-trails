"""The entry store: six kinds, stable ids with append-only versions, links, tasks, comments.

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

from trails import dolt, sessions
from trails.author import current_author
from trails.errors import DoltError, RecordNotFoundError, StoreUnavailableError, ValidationError
from trails.models import (
    ArtifactRef,
    CodeCapture,
    Entry,
    EntryDraft,
    EntryLink,
    EntryVersion,
    EntryWriteOutcome,
    FollowUp,
    LinkEvent,
    LinkOutcome,
    MovedLink,
    ProjectRef,
    Provenance,
    RetireOutcome,
    SessionRow,
    TaskEvent,
    TaskOutcome,
)
from trails.secret_scan import scan_for_secrets
from trails.store import (
    _DIGEST,
    _TEXT,
    EMBEDDING_DIMENSIONS,
    HOST_CHARS,
    NAME_CHARS,
    PATH_CHARS,
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
    _value_text,
    resolve_db_path,
)
from trails.taskgraph import Node, TaskGraph
from trails.vocab import (
    ID_ALPHABET,
    ID_ATTEMPTS_PER_LENGTH,
    ID_LENGTHS,
    KIND_PREFIXES,
    KINDS,
    LINK_ACTIONS,
    LINK_RELATIONS,
    OUTCOMES,
    PRIORITY_RANGE,
    PROJECT_ACTIONS,
    STATED_BY_VALUES,
    STATED_KINDS,
    TASK_EVENTS,
    VERDICTS,
    WORK_KINDS,
    Kind,
    LinkAction,
    LinkRelation,
    Outcome,
    StatedBy,
    TaskEventKind,
    TaskState,
    enum_column,
)

# Written in the database directory once the schema is known to exist. A different name from
# the old store's marker, so an old database is always checked and refused (D13).
SCHEMA_MARKER = ".agent-trails-schema"
AGENT_NAME_CHARS = 200
SESSION_CHARS = 200

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
        stated_by  {enum_column(STATED_BY_VALUES)},
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
        PRIMARY KEY (entry_id, sequence)
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
        tag       VARCHAR({TAG_CHARS}) {_TEXT} PRIMARY KEY,
        canonical VARCHAR({TAG_CHARS}) {_TEXT} NOT NULL
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
                "x.title, x.body, x.value, x.priority, x.stated_by, x.reason, x.author, "
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

    def write_entry(self, draft: EntryDraft, new_id: str | None = None) -> EntryWriteOutcome:
        """Create an entry (version 1) or write its next version, in one transaction.

        `new_id` is a pre-minted id for a creation (see `new_entry_id`).

        Raises:
            ValidationError: malformed input, or a stale or missing `base_version`.
            RecordNotFoundError: `draft.id` names no entry.
        """
        with dolt.exclusive(self.db_path):
            if draft.id is None:
                return self._create_locked(draft, new_id)
            if new_id is not None:
                raise ValidationError("new_id is for a new entry; a new version keeps its id")
            return self._version_locked(draft)

    def _create_locked(self, draft: EntryDraft, new_id: str | None) -> EntryWriteOutcome:
        if draft.kind is None:
            raise ValidationError(
                "a new entry needs kind: goal, task, note, claim, preference or skill"
            )
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
        stated_by = _validated_stated_by(kind, draft.stated_by, inherited=None)
        _validate_content(body, artifacts, code)
        targets = list(dict.fromkeys([*([draft.parent] if draft.parent else []), *draft.waits_on]))
        found = self.get_entries(targets)
        missing = [target for target in targets if target not in found]
        if missing:
            raise ValidationError(f"no entry has id {', '.join(missing)}")
        if draft.parent is not None and draft.parent in draft.waits_on:
            raise ValidationError(f"{draft.parent} is both the parent and in waits_on")
        project_refs = sorted(
            self.resolve_projects(draft.projects or []), key=lambda ref: (ref.name, ref.id)
        )
        if new_id is not None:
            if not new_id.startswith(f"{KIND_PREFIXES[kind]}-"):
                raise ValidationError(f"id {new_id!r} does not match kind {kind!r}")
            if self._read(_bind("SELECT id FROM entry WHERE id = ?", [new_id])):
                raise ValidationError(
                    f"id {new_id!r} is already used: an entry is never overwritten"
                )
        entry_id = new_id or self.new_entry_id(kind)
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
            stated_by=stated_by,
            artifacts=artifacts,
            code=code,
            reason=None,
            author=author,
            harness=provenance.harness,
            session_id=provenance.session_id,
            created_at=created_at,
        )
        relations: list[tuple[LinkRelation, str]] = []
        if draft.parent:
            relations.append(("part_of", draft.parent))
        relations += [("waits_on", target) for target in dict.fromkeys(draft.waits_on)]
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
        warnings = _target_warnings(entry_id, relations, found)
        if relations and kind in WORK_KINDS:
            graph = self.task_graph()
            graph.nodes[entry_id] = Node(
                id=entry_id,
                kind=kind,
                title=title,
                version=1,
                priority=priority,
                created_at=created_at,
                state="open",
                outcome=None,
                claim=None,
                retired=False,
            )
            for relation, target in relations:
                table = graph.parents if relation == "part_of" else graph.waits
                table.setdefault(entry_id, []).append(target)
            warnings += cycle_warnings(graph, [entry_id])
        self._commit(
            statements,
            message=f"create {entry_id}",
            entry_id=entry_id,
            version=1,
            created_at=created_at,
            tags=tags,
        )
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
            entry=entry, created=True, retry=False, warnings=warnings, secret_findings=findings
        )

    def _version_locked(self, draft: EntryDraft) -> EntryWriteOutcome:
        entry_id = str(draft.id)
        existing = self.get_entries([entry_id]).get(entry_id)
        if existing is None:
            raise RecordNotFoundError(f"no entry has id {entry_id!r}")
        if draft.kind is not None and draft.kind != existing.kind:
            raise ValidationError(
                f"{entry_id} is a {existing.kind}; the kind is fixed when an entry is created"
            )
        if draft.parent is not None or draft.waits_on:
            raise ValidationError(
                "parent and waits_on are set when an entry is created; add a later edge with "
                f'link("{entry_id}", "part_of" or "waits_on", "<target id>")'
            )
        current = existing.current
        merged = _merged(existing.kind, current, draft)
        if draft.base_version is None or draft.base_version != current.version:
            if draft.base_version is not None and draft.base_version < current.version:
                landed = self._landed_retry(entry_id, draft.base_version + 1, merged)
                if landed is not None:
                    return EntryWriteOutcome(
                        entry=existing,
                        created=False,
                        retry=True,
                        warnings=[
                            f"version {landed.version} of {entry_id} with this content had "
                            "already landed; nothing new was written"
                        ],
                        secret_findings=[],
                    )
            raise ValidationError(_stale_message(entry_id, draft.base_version, current))
        reason = (draft.reason or "").strip()
        if not reason:
            raise ValidationError(
                f"a new version of {entry_id} needs a reason: one sentence saying what changed"
            )
        _require_text_bytes("reason", reason)
        project_refs: list[ProjectRef] | None = None
        if draft.projects is not None:
            project_refs = sorted(
                self.resolve_projects(draft.projects), key=lambda ref: (ref.name, ref.id)
            )
        same_content = _content(merged) == _content(current)
        same_projects = project_refs is None or [ref.id for ref in project_refs] == [
            ref.id for ref in sorted(existing.projects, key=lambda ref: (ref.name, ref.id))
        ]
        if same_content and same_projects:
            return EntryWriteOutcome(
                entry=existing,
                created=False,
                retry=False,
                warnings=[
                    f"nothing changed: the content is the same as version {current.version}, so "
                    "no version was written"
                ],
                secret_findings=[],
            )
        provenance = sessions.current_provenance()
        author = current_author()
        created_at = _now()
        statements: list[str] = []
        version = current
        if not same_content:
            version = merged.model_copy(
                update={
                    "version": current.version + 1,
                    "reason": reason,
                    "author": author,
                    "harness": provenance.harness,
                    "session_id": provenance.session_id,
                    "created_at": created_at,
                }
            )
            statements += _version_inserts(version)
        projects = existing.projects
        if project_refs is not None and not same_projects:
            statements += self._membership_changes(
                entry_id,
                wanted=[ref.id for ref in project_refs],
                reason=reason,
                author=author,
                provenance=provenance,
                created_at=created_at,
            )
            projects = project_refs
        statements += _session_insert(provenance, created_at)
        findings = scan_for_secrets(_scanned_fields(version)) if not same_content else []
        self._commit(
            statements,
            message=f"version {entry_id} v{version.version}",
            entry_id=entry_id,
            version=version.version if not same_content else None,
            created_at=created_at,
            tags=version.tags,
        )
        return EntryWriteOutcome(
            entry=existing.model_copy(update={"current": version, "projects": projects}),
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
        """Append an `add` event, recording the target's current version. A link that is
        already current is left as it is.

        Raises:
            ValidationError: a self-link, an unknown relation, or an id naming no entry.
        """
        if relation not in LINK_RELATIONS:
            raise ValidationError(f"relation must be one of {', '.join(LINK_RELATIONS)}")
        if source_id == target_id:
            raise ValidationError("an entry cannot link to itself")
        normal_reason = (reason or "").strip() or None
        _require_text_bytes("reason", normal_reason)
        with dolt.exclusive(self.db_path):
            found = self._require_entries([source_id, target_id])
            latest = self._latest_link(source_id, relation, target_id)
            target_version = found[target_id].current.version
            if latest is not None and latest.action == "add":
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
            warnings = _target_warnings(source_id, [(relation, target_id)], found)
            if relation in {"part_of", "waits_on"}:
                graph = self.task_graph()
                table = graph.parents if relation == "part_of" else graph.waits
                table.setdefault(source_id, []).append(target_id)
                warnings += cycle_warnings(graph, [source_id, target_id])
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
            latest = self._latest_link(source_id, relation, target_id)
            if latest is None or latest.action == "remove":
                return LinkOutcome(
                    source_id=source_id,
                    relation=relation,
                    target_id=target_id,
                    action="remove",
                    changed=False,
                    target_version=None,
                    warnings=[f"{source_id} has no current {relation} link to {target_id}"],
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
            warnings=[],
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
        """Every goal and task, every entry a part_of or waits_on link names, and the current
        links between them, from ONE dolt invocation (D15)."""
        linked = "SELECT target_id FROM link_event WHERE relation IN ('part_of', 'waits_on')"
        versions, links, events, retired, memberships = dolt.read_result_sets(
            self.db_path,
            [
                "SELECT e.id, e.kind, e.created_at AS entry_created_at, x.title, x.version, "
                "x.priority FROM entry e JOIN entry_version x ON x.entry_id = e.id WHERE "
                f"(e.kind IN ('goal', 'task') OR e.id IN ({linked})) AND NOT EXISTS (SELECT 1 "
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
        self, entry_id: str, base_version: int | None, take_over_reason: str | None = None
    ) -> TaskOutcome:
        """Claim a task for this session, atomically under the write lock.

        Refused: a goal or any other kind (malformed), a closed task, a stale or missing
        `base_version` (an attempted overwrite), and a task another session holds unless
        `take_over_reason` says why (D3). Claiming a task this session already holds changes
        nothing.
        """
        with dolt.exclusive(self.db_path):
            entry = self.get_entry(entry_id)
            if entry.kind != "task":
                raise ValidationError(
                    f"{entry_id} is a {entry.kind}; only a task is claimed. A goal is closed "
                    "when its tasks are done, never claimed"
                )
            if entry.state == "closed":
                raise ValidationError(
                    f"{entry_id} is closed {entry.outcome}; reopen it first with "
                    f'reopen("{entry_id}", reason="...")'
                )
            if base_version != entry.current.version:
                raise ValidationError(_stale_message(entry_id, base_version, entry.current))
            provenance = sessions.current_provenance()
            held = entry.claim
            take_over = (take_over_reason or "").strip()
            if held is not None:
                if _same_session(held, provenance):
                    return TaskOutcome(
                        entry=entry, event=held, changed=False, follow_ups=[], moved=[],
                        warnings=[],
                    )  # fmt: skip
                if not take_over:
                    raise ValidationError(
                        f"{entry_id} is claimed by {_claimant(held)} since "
                        f"{held.created_at.isoformat()}; to take it over, say why: "
                        f'claim("{entry_id}", base_version={entry.current.version}, '
                        'take_over_reason="...")'
                    )
            _require_text_bytes("take_over_reason", take_over or None)
            event = self._task_event(
                entry, "claim", outcome=None, reason=take_over or None, provenance=provenance
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
                    entry=entry, event=None, changed=False, follow_ups=[], moved=[],
                    warnings=[f"{entry_id} is not claimed; nothing to release"],
                )  # fmt: skip
            provenance = sessions.current_provenance()
            event = self._task_event(
                entry, "release", outcome=None, reason=normal_reason, provenance=provenance
            )
            self._commit_events([_task_insert(event)], f"release {entry_id}", event, provenance)
        return self._task_outcome(entry, event)

    def reopen(self, entry_id: str, reason: str) -> TaskOutcome:
        """Move a closed goal or task back to open (D11). Links a supersede moved stay moved.
        Reopening an entry that is not closed changes nothing."""
        normal_reason = _required_reason("reopen", reason)
        with dolt.exclusive(self.db_path):
            entry = self.get_entry(entry_id)
            if entry.kind not in WORK_KINDS:
                raise ValidationError(f"{entry_id} is a {entry.kind}; only goals and tasks close")
            if entry.state != "closed":
                return TaskOutcome(
                    entry=entry, event=None, changed=False, follow_ups=[], moved=[],
                    warnings=[f"{entry_id} is not closed; nothing to reopen"],
                )  # fmt: skip
            provenance = sessions.current_provenance()
            event = self._task_event(
                entry, "reopen", outcome=None, reason=normal_reason, provenance=provenance
            )
            self._commit_events([_task_insert(event)], f"reopen {entry_id}", event, provenance)
        return self._task_outcome(entry, event)

    def close(  # noqa: PLR0912 — one transaction holds the close and all it implies
        self,
        entry_id: str,
        outcome: Outcome,
        summary: str,
        by: Sequence[str] = (),
        follow_ups: Sequence[FollowUp] = (),
    ) -> TaskOutcome:
        """Close a goal or task with an outcome, in one transaction with everything it implies.

        `superseded` needs a nonempty `by`, every id an existing goal or task; every current
        incoming waits_on link is moved onto every replacement, and each replacement gets a
        `supersedes` link to the closed entry, which is where `by` is recorded. Each follow-up
        becomes a new task linked `discovered_from` the closed entry and `part_of` its parent,
        if it has one (D18). Closing a parent with children not closed is stored with a warning
        (D22). Closing again with the same outcome changes nothing.
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
                'outcome="superseded" needs by=[ids]: the goals or tasks that replace this one'
            )
        if outcome != "superseded" and replacements:
            raise ValidationError('by names replacements and goes with outcome="superseded" only')
        if entry_id in replacements:
            raise ValidationError(f"{entry_id} cannot supersede itself")
        for item in follow_ups:
            _validated_title(item.title)
            _validated_priority("task", item.priority)
        with dolt.exclusive(self.db_path):
            found = self._require_entries([entry_id, *replacements])
            entry = found[entry_id]
            if entry.kind not in WORK_KINDS:
                raise ValidationError(f"{entry_id} is a {entry.kind}; only goals and tasks close")
            not_work = [item for item in replacements if found[item].kind not in WORK_KINDS]
            if not_work:
                raise ValidationError(
                    f"{', '.join(not_work)} is not a goal or task; only a goal or task can "
                    "replace one"
                )
            if entry.state == "closed":
                if entry.outcome == outcome:
                    return TaskOutcome(
                        entry=entry, event=None, changed=False, follow_ups=[], moved=[],
                        warnings=[f"{entry_id} was already closed {outcome}; nothing changed"],
                    )  # fmt: skip
                raise ValidationError(
                    f"{entry_id} is already closed {entry.outcome}; to change the outcome, "
                    f'reopen("{entry_id}", reason="...") and close it again'
                )
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
            created: list[str] = []
            parents = graph.parents.get(entry_id, [])
            for item in follow_ups:
                follow_id = self.new_entry_id("task")
                created.append(follow_id)
                statements += self._follow_up_statements(
                    follow_id,
                    item,
                    closed=entry,
                    parents=parents,
                    graph=graph,
                    moment=event.created_at,
                    provenance=provenance,
                )
            self._commit_events(statements, f"close {entry_id} {outcome}", event, provenance)
        warnings = []
        still_open = graph.open_children(entry_id)
        if still_open:
            warnings.append(
                f"{entry_id} was closed with children not closed: {', '.join(still_open)}; their "
                "state is unchanged"
            )
        return self._task_outcome(entry, event, follow_ups=created, moved=moved, warnings=warnings)

    def retire_entry(self, entry_id: str, reason: str) -> RetireOutcome:
        """Mark an entry no longer true without replacing it. Retiring twice keeps the first
        reason and changes nothing."""
        normal_reason = (reason or "").strip()
        if not normal_reason:
            raise ValidationError("retire needs a reason: why the entry is no longer true")
        _require_text_bytes("reason", normal_reason)
        with dolt.exclusive(self.db_path):
            entry = self.get_entry(entry_id)
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
        return RetireOutcome(entry_id=entry_id, reason=normal_reason, already_retired=False)

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

    def _follow_up_statements(
        self,
        follow_id: str,
        item: FollowUp,
        *,
        closed: Entry,
        parents: Sequence[str],
        graph: TaskGraph,
        moment: datetime,
        provenance: Provenance,
    ) -> list[str]:
        author = current_author()
        version = EntryVersion(
            entry_id=follow_id,
            version=1,
            title=_validated_title(item.title),
            body=item.body,
            tags=_normalise_tags(item.tags),
            value=None,
            priority=item.priority,
            stated_by=None,
            artifacts=[],
            code=[],
            reason=None,
            author=author,
            harness=provenance.harness,
            session_id=provenance.session_id,
            created_at=moment,
        )
        links: list[tuple[LinkRelation, str, int | None]] = [
            ("discovered_from", closed.id, closed.current.version),
            *(
                ("part_of", parent, graph.nodes[parent].version if parent in graph.nodes else None)
                for parent in parents
            ),
        ]
        project_ids = sorted(ref.id for ref in closed.projects)
        return [
            _insert("entry", {"id": follow_id, "kind": "task", "created_at": moment}),
            *_version_inserts(version),
            *(
                _link_insert(
                    self._link_event(
                        follow_id, relation, target, action="add", sequence=0,
                        target_version=target_version, reason=None, provenance=provenance,
                        moment=moment,
                    )
                )
                for relation, target, target_version in links
            ),
            *(
                _membership_insert(
                    follow_id, project_id, sequence=0, action="add", reason=None,
                    author=author, provenance=provenance, created_at=moment,
                )
                for project_id in project_ids
            ),
        ]  # fmt: skip

    def _task_outcome(
        self,
        before: Entry,
        event: TaskEvent,
        follow_ups: Sequence[str] = (),
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
            follow_ups=list(follow_ups),
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
    """State, outcome and held claim from a goal's or task's events, in sequence order."""
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
        raise ValidationError(f"priority is a goal and task field; a {kind} has none")
    low, high = PRIORITY_RANGE
    if isinstance(priority, bool) or not low <= priority <= high:
        raise ValidationError(f"priority must be an integer from {low} to {high}; 0 is highest")
    return priority


def _validated_stated_by(
    kind: Kind, stated_by: StatedBy | None, inherited: StatedBy | None
) -> StatedBy | None:
    if kind not in STATED_KINDS:
        if stated_by is not None:
            raise ValidationError(
                f"stated_by is recorded on claims and preferences; a {kind} has none"
            )
        return None
    if stated_by is None:
        return inherited or "agent"
    if stated_by not in STATED_BY_VALUES:
        raise ValidationError(f"stated_by must be one of {', '.join(STATED_BY_VALUES)}")
    return stated_by


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
    for capture in code:
        _require_length("code repo_path", capture.repo_path, PATH_CHARS)
    repos = [capture.repo_path for capture in code]
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
        current.priority if draft.priority is None else _validated_priority(kind, draft.priority)
    )
    stated_by = _validated_stated_by(kind, draft.stated_by, inherited=current.stated_by)
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
            "stated_by": stated_by,
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
        version.stated_by,
    )


def _stale_message(entry_id: str, given: int | None, current: EntryVersion) -> str:
    session = f", session {current.session_id}" if current.session_id else ""
    who = (
        f"{entry_id} is at version {current.version}, written by {current.author} "
        f"({current.harness}{session}) at {current.created_at.isoformat()}"
    )
    retry = (
        f'read it with show("{entry_id}"), then retry with write(id="{entry_id}", '
        f'base_version={current.version}, reason="...", <the fields you change>)'
    )
    if given is None:
        return f"a new version needs base_version: {who}; {retry}"
    return f"base_version={given} is stale and would overwrite a newer version: {who}; {retry}"


def _target_warnings(
    entry_id: str, relations: Sequence[tuple[LinkRelation, str]], found: dict[str, Entry]
) -> list[str]:
    """Warnings about links whose target cannot play the role the relation gives it (D10)."""
    warnings = []
    for relation, target in relations:
        entry = found.get(target)
        if entry is None or relation not in {"part_of", "waits_on"}:
            continue
        if entry.kind not in WORK_KINDS:
            role = "parent" if relation == "part_of" else "dependency"
            warnings.append(
                f"{entry_id} {relation} {target}, which is a {entry.kind}: only a goal or task "
                f"counts as a {role}, so this link is stored and ignored for readiness"
            )
        if entry.retired:
            warnings.append(f"{entry_id} {relation} {target}, which is retired")
    return warnings


def _scanned_fields(version: EntryVersion) -> dict[str, str]:
    value_text = _value_text(version.value)
    return {
        "title": version.title,
        "body": version.body,
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
                "stated_by": version.stated_by,
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


def _session_insert(provenance: Provenance, seen_at: datetime) -> list[str]:
    """Record a session the first time a write names it. `INSERT IGNORE` keeps the first row,
    so a session's row is written once however many writes it makes (probed on dolt 2.4.0)."""
    if provenance.session_id is None:
        return []
    _require_length("session id", provenance.session_id, SESSION_CHARS)
    agent = provenance.agent[:AGENT_NAME_CHARS] if provenance.agent else None
    return [
        "INSERT IGNORE"
        + _insert(
            "session",
            {
                "id": provenance.session_id,
                "harness": provenance.harness[:NAME_CHARS],
                "transcript": provenance.transcript,
                "agent": agent,
                "first_seen": seen_at,
            },
        ).removeprefix("INSERT")
    ]


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
        stated_by=_as_stated_by(row.get("stated_by")),
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


def _as_stated_by(value: Any) -> StatedBy | None:
    if value is None:
        return None
    for stated in STATED_BY_VALUES:
        if stated == str(value):
            return stated
    raise DoltError(f"version has unknown stated_by {value!r}")


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


def _claimant(event: TaskEvent) -> str:
    session = f"session {event.session_id}" if event.session_id else "no session id"
    return f"{event.author} ({event.harness}, {session})"


def cycle_warnings(graph: TaskGraph, entry_ids: Sequence[str]) -> list[str]:
    """One warning per must-finish-first cycle through any of `entry_ids` (D8)."""
    return [
        "dependency cycle: "
        + " -> ".join(cycle)
        + "; none of these can become ready until one link in the cycle is removed with unlink"
        for cycle in graph.cycles_among(entry_ids)
    ]
