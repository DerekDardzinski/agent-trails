"""The base store: projects, framing, tag aliases, read-only queries, and the SQL helpers
every write uses. `entry_store.EntryStore` extends it with entries, links, tasks and comments,
and defines the schema.

The store stores and decides nothing. It refuses exactly two things, both as ValidationError:
malformed input, and an attempt to overwrite. Every history row is an INSERT; current project
paths alone are mutable metadata, with older states retained in Dolt history.

Three facts about Dolt shape almost every function here, all probed:

* One `dolt sql -q` invocation is ONE transaction, and `CALL DOLT_COMMIT` must be its single
  finisher. It raises "nothing to commit" on a transaction that changed nothing, so every
  idempotent path returns BEFORE reaching it.
* `dolt sql -r json` OMITS a NULL column from the row object rather than emitting null, so
  nullable columns are read with `.get`.
* Every read and write is a subprocess, and invocations are the cost, so reads are batched into
  one invocation wherever they can be.

Every text column but project_path.path is declared `COLLATE utf8mb4_0900_ai_ci`. Dolt's
default, utf8mb4_0900_bin, makes FULLTEXT MATCH and LIKE case-sensitive (measured on dolt
2.4.0: MATCH 'lift' did not find 'Lift'). `SHOW FULL COLUMNS` reports the declared collation
inside its `Type` field while its `Collation` field reads utf8mb4_0900_bin regardless;
`information_schema.columns` reports it in COLLATION_NAME.
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
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from trails import dolt
from trails.author import current_author
from trails.errors import DoltError, ValidationError
from trails.models import (
    Project,
    ProjectFramingDraft,
    ProjectFramingVersion,
    ProjectPath,
    ProjectRef,
    ProjectWriteOutcome,
)
from trails.vocab import (
    ID_ALPHABET,
    ID_ATTEMPTS_PER_LENGTH,
    ID_LENGTHS,
    PROJECT_ID_PATTERN,
    PROJECT_ID_PREFIX,
)

DEFAULT_DB_ENV = "TRAILS_DB"
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


_READ_ONLY_FIRST_KEYWORDS = frozenset({"select", "show", "describe", "desc", "explain", "with"})
# SHOW and DESCRIBE cannot modify anything, whatever words follow — and
# `SHOW CREATE TABLE entry` legitimately contains a write keyword, so the denylist
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

    def _unused_project_id(self) -> str:
        """Return the first unused bounded `prj-` candidate."""
        for candidate in _project_id_candidates():
            if not self._read(_bind("SELECT id FROM project WHERE id = ?", [candidate])):
                return candidate
        raise DoltError(
            "failed to generate an unused project id after "
            f"{len(ID_LENGTHS) * ID_ATTEMPTS_PER_LENGTH} candidates"
        )

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

    def write_tag_alias(self, tag: str, canonical: str) -> None:
        """Insert a tag_alias row; an existing identical row is a no-op.

        Both tags are normalised as entry tags are (strip, lowercase), so an alias names a
        tag an entry can carry.

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

    def _collation_equal_pairs(self, tags: Sequence[str]) -> list[tuple[str, str]]:
        """Pairs of distinct `tags` the tag column collation compares as equal, from one read."""
        pairs = [(a, b) for i, a in enumerate(tags) for b in tags[i + 1 :]]
        if not pairs:
            return []
        columns = ", ".join(
            f"(? COLLATE utf8mb4_0900_ai_ci = ? COLLATE utf8mb4_0900_ai_ci) AS p{n}"
            for n in range(len(pairs))
        )
        row = self._read(_bind(f"SELECT {columns}", [value for pair in pairs for value in pair]))[0]
        return [pair for n, pair in enumerate(pairs) if _as_bool(row[f"p{n}"])]

    def _read(self, statement: str) -> list[dict[str, Any]]:
        return dolt.read_rows(self.db_path, statement)

    def _execute_script(self, statements: Sequence[str]) -> None:
        """Run a write as ONE Dolt transaction, under the write lock `dolt.run_script` holds."""
        dolt.run_script(self.db_path, statements)


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


def _as_value(value: Any) -> dict[str, Any] | None:
    """The JSON value column, which `-r json` returns already decoded."""
    if value is None:
        return None
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, dict):
        raise ValueError(f"value does not hold a JSON object: {value!r}")
    return value


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
