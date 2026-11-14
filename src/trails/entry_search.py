"""Search over current entry versions, and the entry embedding that feeds its vector arm.

Three rankers run in ONE dolt invocation and are fused by reciprocal rank, as before:

- keyword: FULLTEXT MATCH over the current version's title and body, after common English
  function words are removed;
- exact: each identifier-shaped query token (an id, a path, a sha, a file name) matched
  case-insensitively over the id, title, body, value, tags, artifacts and captured code of the
  current version; an exact hit leads the order, because RRF flattens a near-certainty toward a
  fuzzy match;
- vector: the nearest entries by their best chunk of the current version, cosine at least
  `retrieval.VECTOR_MIN_COSINE`.

A note's fused score is halved before sorting (D5): notes are scratch, and a claim, task or
skill about the same thing should come first. The penalty is fixed because "about the same
thing" needs judgment the store does not have.

Only current versions are searched. An earlier version is read with `show(id, versions=True)`.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from typing import Any

from pydantic import BaseModel

from trails import dolt, retrieval
from trails.embeddings import EmbeddingEngine
from trails.entry_store import EntryStore
from trails.errors import ValidationError
from trails.vector import KNOWN_MODEL_IDS, VectorMatch, chunk_texts
from trails.vocab import OUTCOMES, WORK_KINDS, Kind

NOTE_PENALTY = 0.5
_CURRENT = (
    "NOT EXISTS (SELECT 1 FROM entry_version n WHERE n.entry_id = x.entry_id "
    "AND n.version > x.version)"
)
_FROM = f"FROM entry e JOIN entry_version x ON x.entry_id = e.id AND {_CURRENT}"


class EntryFilters(BaseModel):
    """Hard constraints on which entries a search may return."""

    kinds: list[Kind] = []
    projects: list[str] = []
    tags: list[str] = []
    status: str | None = None
    open_comments: bool = False
    include_retired: bool = False


class EntryRanked(BaseModel):
    """Every entry a search matched, in final order, with the rankers that returned each."""

    order: list[str]
    rankers: dict[str, dict[str, int]]
    vector: dict[str, VectorMatch]


def run_entry_search(  # noqa: PLR0912 — three rankers and their fusion
    store: EntryStore,
    filters: EntryFilters,
    *,
    query: str,
    query_vector: Sequence[float] | None,
    model_id: str | None,
    equivalence: dict[str, tuple[str, ...]],
) -> EntryRanked:
    """Run every ranker in one dolt invocation and fuse them. With no query, the filters
    alone select entries, newest version first."""
    where = _where(filters, equivalence)
    exact_terms = [
        token for token in retrieval.tokenize(query) if retrieval.looks_like_identifier(token)
    ]
    keyword_query = " ".join(retrieval.keyword_tokens(query))
    names: list[str] = []
    statements: list[str] = []
    if keyword_query:
        names.append("keyword")
        statements.append(_keyword_statement(keyword_query, where))
    if exact_terms:
        names.append("exact")
        statements.append(_exact_statement(exact_terms, where))
    if query_vector is not None and model_id is not None:
        names.append("vector")
        statements.append(_nearest_statement(query_vector, model_id, where))
    if not names:
        if query.strip():
            return EntryRanked(order=[], rankers={}, vector={})
        rows = store.query(
            f"SELECT e.id AS id, e.kind AS kind {_FROM} WHERE {where} "
            "ORDER BY x.created_at DESC, e.id ASC"
        )
        return EntryRanked(order=[str(row["id"]) for row in rows], rankers={}, vector={})
    results = dict(zip(names, dolt.read_result_sets(store.db_path, statements), strict=True))
    kinds: dict[str, str] = {}
    created: dict[str, str] = {}
    rankings: dict[str, list[str]] = {}
    for name in ("keyword", "exact"):
        if name in results:
            rankings[name] = list(dict.fromkeys(str(row["id"]) for row in results[name]))
            for row in results[name]:
                kinds[str(row["id"])] = str(row["kind"])
                created[str(row["id"])] = str(row["created_at"])
    nearest: list[VectorMatch] = []
    seen: set[str] = set()
    for row in results.get("vector", []):
        entry_id = str(row["entry_id"])
        kinds.setdefault(entry_id, str(row["kind"]))
        created.setdefault(entry_id, str(row["created_at"]))
        if entry_id in seen or len(nearest) >= retrieval.VECTOR_DEPTH:
            continue
        seen.add(entry_id)
        cosine = 1.0 - float(row["distance"])
        if cosine >= retrieval.VECTOR_MIN_COSINE:
            nearest.append(
                VectorMatch(
                    record_id=entry_id,
                    cosine=cosine,
                    chunk_index=int(row["chunk_index"]),
                    chunk_count=int(row["chunks"]),
                )
            )
    if nearest:
        rankings["vector"] = [match.record_id for match in nearest]
    scores = retrieval.rrf_scores(list(rankings.values()))
    for entry_id in scores:
        if kinds.get(entry_id) == "note":
            scores[entry_id] *= NOTE_PENALTY
    fused = sorted(scores)
    fused.sort(key=lambda item: created.get(item, ""), reverse=True)
    fused.sort(key=lambda item: scores[item], reverse=True)
    exact = set(rankings.get("exact", []))
    order = [item for item in fused if item in exact] + [
        item for item in fused if item not in exact
    ]
    rankers = {
        item: {
            name: ranking.index(item) + 1 for name, ranking in rankings.items() if item in ranking
        }
        for item in order
    }
    return EntryRanked(
        order=order, rankers=rankers, vector={match.record_id: match for match in nearest}
    )


def excerpt(body: str, query: str, width: int = 240) -> str:
    """A window of at most `width` characters centered on the first query word found in the
    body, or the body's start when none is."""
    text = body.strip()
    if len(text) <= width:
        return text
    lowered = text.lower()
    for token in retrieval.keyword_tokens(query):
        match = re.search(rf"\b{re.escape(token)}\b", lowered)
        if match is not None:
            start = max(0, min(match.start() - width // 2, len(text) - width))
            prefix = "..." if start > 0 else ""
            suffix = "..." if start + width < len(text) else ""
            return prefix + text[start : start + width] + suffix
    return text[:width] + "..."


# ------------------------------------------------------------------ embedding


def embed_entries(
    store: EntryStore,
    engine: EmbeddingEngine,
    entry_ids: Sequence[str],
    *,
    model_id: str | None = None,
) -> int:
    """Embed the current version of each entry, one row per chunk, keyed by entry, version and
    chunk. A current version already embedded under this model is skipped, so a second run
    writes nothing and never reaches `CALL DOLT_COMMIT`. Returns the rows written."""
    if isinstance(entry_ids, str):
        raise ValidationError(f"entry_ids must be a sequence of ids, not the string {entry_ids!r}")
    if not entry_ids or not engine.is_available():
        return 0
    selected = model_id or engine.model_id()
    if selected not in KNOWN_MODEL_IDS:
        known = ", ".join(sorted(KNOWN_MODEL_IDS))
        raise ValidationError(
            f"unknown embedding model {selected!r}; this engine can produce: {known}"
        )
    ids = tuple(dict.fromkeys(entry_ids))
    found = store.get_entries(ids)
    placeholders = ", ".join("?" * len(ids))
    done = {
        (str(row["entry_id"]), int(row["version"]))
        for row in store.query(
            "SELECT DISTINCT entry_id, version FROM entry_chunk_embedding WHERE model_id = ? "
            f"AND entry_id IN ({placeholders})",
            [selected, *ids],
        )
    }
    rows: list[str] = []
    for entry_id in ids:
        entry = found.get(entry_id)
        if entry is None or (entry_id, entry.current.version) in done:
            continue
        for index, text in enumerate(chunk_texts(engine, entry.current.title, entry.current.body)):
            vector = engine.embed(text)
            if len(vector) != 384:
                raise ValidationError(f"a vector for {entry_id} has {len(vector)} components")
            rows.append(
                f"({dolt.sql_literal(entry_id)}, {entry.current.version}, {index}, "
                f"{dolt.sql_literal(selected)}, "
                f"STRING_TO_VECTOR({dolt.sql_literal(dolt.vector_literal(vector))}))"
            )
    if not rows:
        return 0
    dolt.run_script(
        store.db_path,
        [
            "START TRANSACTION",
            "INSERT INTO entry_chunk_embedding (entry_id, version, chunk_index, model_id, vec) "
            f"VALUES {', '.join(rows)}",
            f"CALL DOLT_COMMIT('-A','-m','embed {len(rows)} chunks')",
        ],
    )
    return len(rows)


def unembedded_ids(store: EntryStore, model_id: str) -> list[str]:
    """Entries whose current version has no chunk row for `model_id`."""
    rows = store.query(
        f"SELECT e.id AS id {_FROM} WHERE NOT EXISTS (SELECT 1 FROM entry_chunk_embedding c "
        "WHERE c.entry_id = e.id AND c.version = x.version AND c.model_id = ?) ORDER BY e.id",
        [model_id],
    )
    return [str(row["id"]) for row in rows]


# ------------------------------------------------------------------ statements


def status_condition(status: str) -> str:
    """A goal or task's state or close outcome, from its latest task event, over alias `e`."""
    latest = (
        "EXISTS (SELECT 1 FROM task_event t WHERE t.entry_id = e.id AND {condition} AND NOT "
        "EXISTS (SELECT 1 FROM task_event later WHERE later.entry_id = t.entry_id AND "
        "later.sequence > t.sequence))"
    )
    work = "e.kind IN (" + ", ".join(_lit(kind) for kind in WORK_KINDS) + ")"
    if status == "open":
        held = latest.format(condition="t.event IN ('claim', 'close')")
        return f"({work} AND NOT {held})"
    if status == "claimed":
        return latest.format(condition="t.event = 'claim'")
    if status == "closed":
        return latest.format(condition="t.event = 'close'")
    if status in OUTCOMES:
        return latest.format(condition=f"t.event = 'close' AND t.outcome = {_lit(status)}")
    raise ValidationError(
        f"status must be open, claimed, closed or an outcome ({', '.join(OUTCOMES)}), not "
        f"{status!r}"
    )


# An entry with at least one comment that is not a response and that nothing has addressed.
OPEN_COMMENT_CONDITION = (
    "EXISTS (SELECT 1 FROM comment c WHERE c.entry_id = e.id AND NOT EXISTS (SELECT 1 FROM "
    "comment_addresses a WHERE a.addressed_id = c.id) AND NOT EXISTS (SELECT 1 FROM "
    "comment_addresses r WHERE r.comment_id = c.id))"
)


def membership_condition(project_ids: Sequence[str]) -> str:
    ids = ", ".join(_lit(item) for item in project_ids)
    return (
        "EXISTS (SELECT 1 FROM entry_project_event m WHERE m.entry_id = e.id AND m.project_id IN "
        f"({ids}) AND m.action = 'add' AND NOT EXISTS (SELECT 1 FROM entry_project_event later "
        "WHERE later.entry_id = m.entry_id AND later.project_id = m.project_id AND "
        "later.sequence > m.sequence))"
    )


def _where(filters: EntryFilters, equivalence: dict[str, tuple[str, ...]]) -> str:
    clauses = ["1 = 1"]
    if not filters.include_retired:
        clauses.append("NOT EXISTS (SELECT 1 FROM retirement r WHERE r.entry_id = e.id)")
    if filters.kinds:
        clauses.append("e.kind IN (" + ", ".join(_lit(kind) for kind in filters.kinds) + ")")
    for tag in filters.tags:
        forms = ", ".join(
            _lit(form) for form in retrieval.expand_tags(equivalence, [tag.strip().lower()])
        )
        clauses.append(
            "EXISTS (SELECT 1 FROM version_tag g WHERE g.entry_id = e.id AND g.version = "
            f"x.version AND g.tag IN ({forms}))"
        )
    if filters.projects:
        clauses.append(membership_condition(filters.projects))
    if filters.status is not None:
        clauses.append(status_condition(filters.status))
    if filters.open_comments:
        clauses.append(OPEN_COMMENT_CONDITION)
    return " AND ".join(clauses)


def _keyword_statement(query: str, where: str) -> str:
    q = _lit(query)
    title = f"MATCH(x.title) AGAINST ({q} IN NATURAL LANGUAGE MODE)"
    body = f"MATCH(x.body) AGAINST ({q} IN NATURAL LANGUAGE MODE)"
    return (
        f"SELECT e.id AS id, e.kind AS kind, x.created_at AS created_at, {title} + {body} AS "
        f"score {_FROM} WHERE ({title} OR {body}) AND {where} "
        "ORDER BY score DESC, x.created_at DESC, e.id ASC"
    )


def _exact_statement(terms: Sequence[str], where: str) -> str:
    conditions = [_term_condition(term) for term in terms]
    hits = " + ".join(f"(CASE WHEN {condition} THEN 1 ELSE 0 END)" for condition in conditions)
    return (
        f"SELECT e.id AS id, e.kind AS kind, x.created_at AS created_at, {hits} AS hits {_FROM} "
        f"WHERE ({' OR '.join(conditions)}) AND {where} "
        "ORDER BY hits DESC, x.created_at DESC, e.id ASC"
    )


def _term_condition(term: str) -> str:
    if len(term) >= 3:
        pattern = _lit(retrieval.like_pattern(term))

        def match(column: str) -> str:
            return f"{column} LIKE {pattern}"
    else:
        regex = _lit(rf"\b{re.escape(term.lower())}\b")

        def match(column: str) -> str:
            return f"REGEXP_LIKE({column}, {regex}, 'i')"

    same = "{alias}.entry_id = e.id AND {alias}.version = x.version"
    return (
        f"({match('e.id')} OR {match('x.title')} OR {match('x.body')} OR "
        f"{match('LOWER(CAST(x.value AS CHAR))')}"
        f" OR EXISTS (SELECT 1 FROM version_tag g WHERE {same.format(alias='g')} AND "
        f"{match('g.tag')})"
        f" OR EXISTS (SELECT 1 FROM version_artifact a WHERE {same.format(alias='a')} AND "
        f"({match('a.location')} OR {match('a.notes')}))"
        f" OR EXISTS (SELECT 1 FROM version_code c WHERE {same.format(alias='c')} AND "
        f"({match('c.repo_path')} OR {match('c.head_sha')} OR {match('c.captured_sha')})))"
    )


def _nearest_statement(query_vector: Sequence[float], model_id: str, where: str) -> str:
    vector = _lit(dolt.vector_literal(query_vector))
    return (
        "SELECT c.entry_id AS entry_id, e.kind AS kind, c.chunk_index AS chunk_index, "
        "x.created_at AS created_at, "
        f"DISTANCE(c.vec, STRING_TO_VECTOR({vector}), 'COSINE') AS distance, "
        "(SELECT COUNT(*) FROM entry_chunk_embedding k WHERE k.entry_id = c.entry_id AND "
        "k.version = c.version AND k.model_id = c.model_id) AS chunks "
        f"FROM entry_chunk_embedding c JOIN entry e ON e.id = c.entry_id JOIN entry_version x "
        f"ON x.entry_id = e.id AND x.version = c.version AND {_CURRENT} "
        f"WHERE c.model_id = {_lit(model_id)} AND {where} "
        "ORDER BY distance ASC, c.entry_id ASC, c.chunk_index ASC"
    )


def _lit(value: Any) -> str:
    return dolt.sql_literal(value)
