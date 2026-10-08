"""Retrieval: the search rankers and the primitives they are built on.

Reciprocal-rank fusion, identifier detection, the one word-boundary rule, tag alias
expansion, and `run_search`, which runs the keyword, exact and vector rankers.

SEARCH TERMS ALWAYS REACH SQL AS `dolt.sql_literal` LITERALS, in statements this module
builds and reads through `dolt.read_result_sets`. Not through `Store.query`: its write-keyword
denylist would refuse a search for "how to delete a record" written inline, and its `_bind`
splices a later parameter into an earlier one that contains `?` (labbook-79y.7).

Dolt's minimum indexed FULLTEXT token length is 3, so a one or two character token matches
nothing through MATCH (measured on dolt 1.86.5 and 2.4.0).
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Sequence
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel

from trails import dolt, vector, vocab
from trails.store import Store
from trails.vector import VectorMatch
from trails.vocab import EntryType, Relation, WorkStatus

RRF_K = 60

# Kept anywhere in a word, because they carry meaning: `100%` is a real query,
# `cross-encoder` is one term, and `/ @ .` are what make a token look like an
# identifier. Trimmed at the ends only, because there they are sentence
# punctuation: `outcome:` is the word `outcome`.
_WORD_CHARS = "_-/@%.:"
_TRIM_CHARS = ".,;:!?-()[]{}<>\"'"
# A sha-shaped token. Git's abbreviated form starts at 7 characters and a full
# sha1 is 40. It does NOT match a record id: the hyphen splits the token and no
# prefix is hex, which is why `vocab.ID_PATTERN` is checked separately in
# `looks_like_identifier`.
_HEX_TOKEN = re.compile(r"[0-9a-f]{7,40}")
# A stem of two or more characters before the dot, so `template.md` qualifies and
# `e.g.` does not.
_FILE_EXTENSION = re.compile(r"[\w-]{2,}\.[A-Za-z]{1,8}(?![A-Za-z0-9])")


def split_words(text: str) -> tuple[str, ...]:
    """Split into words on ONE boundary rule, used for queries and field text.

    Two boundary rules would mean a query token that cannot equal any text word:
    a query for `cross-encoder` against text split into `cross` and `encoder`
    matches nothing, and the search then abstains on a term printed verbatim in
    the record it should have returned — a wrong answer indistinguishable from an
    empty store.

    Built on `_word_spans`, which is the rule itself. `_match_position` walks the same
    spans to place a snippet, so where a token is found and whether it is found are
    decided by one definition.
    """
    return tuple(
        stripped for _position, word in _word_spans(text) if (stripped := word.strip(_TRIM_CHARS))
    )


def _word_spans(text: str) -> Iterator[tuple[int, str]]:
    """Each word of `text` with the index it starts at, untrimmed.

    THE POSITION IS THE SPAN'S START, before `_TRIM_CHARS` is stripped. Several trim
    characters are also word characters — `.`, `-`, `:` — so a leading one shifts a word's
    own start by a character or two against the position reported here. That is
    immaterial to the only caller that reads a position: a snippet window is cut
    `SNIPPET_LEAD` characters ahead of it.
    """
    start: int | None = None
    for index, char in enumerate(text):
        if char.isalnum() or char in _WORD_CHARS:
            if start is None:
                start = index
            continue
        if start is not None:
            yield start, text[start:index]
            start = None
    if start is not None:
        yield start, text[start:]


def keyword_match_span(text: str, term: str) -> tuple[int, int] | None:
    """The first whole-word `term` span in `text`, comparing with Unicode case-folding.

    Uses `_word_spans`, the same boundary rule as keyword retrieval and `keyword_matches`, so an
    earlier substring inside another word cannot displace the whole word that made the hit.
    """
    target = term.casefold()
    if not target:
        return None
    for position, raw_word in _word_spans(text):
        word = raw_word.strip(_TRIM_CHARS)
        if word.casefold() != target:
            continue
        leading_trim = len(raw_word) - len(raw_word.lstrip(_TRIM_CHARS))
        start = position + leading_trim
        return start, start + len(word)
    return None


def tokenize(query: str) -> tuple[str, ...]:
    """Query tokens: `split_words`, lowercased, deduped, order preserved."""
    seen: list[str] = []
    for word in split_words(query.lower()):
        if word not in seen:
            seen.append(word)
    return tuple(seen)


def keyword_tokens(query: str) -> tuple[str, ...]:
    """Natural-language query tokens after function words that add no subject are removed.

    Exact terms and identifier-shaped tokens still use `tokenize` directly, so this filter
    cannot hide an identifier that happens to look like a stopword.
    """
    return tuple(token for token in tokenize(query) if token not in KEYWORD_STOPWORDS)


def tag_equivalence(store: Store) -> dict[str, tuple[str, ...]]:
    """Every tag mapped to every tag meaning the same thing, from ONE read.

    `Store.alias_map` is the single definition of canonical, including its answer for a
    recorded cycle, so this inverts that map rather than walking `tag_alias` again: each
    canonical tag collects itself plus every tag resolving to it, and each member of the
    class maps to the whole class.

    ONE read for the whole store, because a search consults tag equivalence on three
    paths — the tag filter, the tag ranker, and the exact-match tag arm — and an
    earlier revision paid a `resolve_tag` call plus an alias walk PER TOKEN PER PATH,
    which was the largest source of dolt subprocesses in one search.
    """
    classes: dict[str, set[str]] = {}
    for tag, canonical in store.alias_map().items():
        classes.setdefault(canonical, {canonical}).add(tag)
    return {member: tuple(sorted(members)) for members in classes.values() for member in members}


def equivalent_tags(store: Store, tag: str) -> tuple[str, ...]:
    """Every tag that means the same thing as `tag`, canonical form included.

    Resolution has to run BOTH ways. Forward only would make `tags=["opus-4.6"]`
    return nothing while records tagged canonically exist, which reads as "nobody
    has looked at this" — the store's most consequential false negative. Both
    directions come out of `tag_equivalence`, which is where the one definition lives.
    """
    return expand_tags(tag_equivalence(store), [tag])


def expand_tags(equivalence: dict[str, tuple[str, ...]], tags: Sequence[str]) -> tuple[str, ...]:
    """`tags` widened to every equivalent form, sorted and deduped.

    A tag nobody has aliased expands to itself, so a query token that names no tag at
    all still reaches `_records_with_tag`, which is what decides whether any record
    actually carries it.
    """
    found: set[str] = set()
    for tag in tags:
        found.update(equivalence.get(tag, (tag,)))
    return tuple(sorted(found))


def rrf_scores(rankings: Sequence[Sequence[str]]) -> dict[str, float]:
    """Reciprocal rank fusion at k=60, the Cormack convention.

    Rank-based, so it needs no per-ranker weights and sidesteps the
    incomparability of FULLTEXT relevance against a tag match count entirely.
    """
    scores: dict[str, float] = {}
    for ranking in rankings:
        for index, record_id in enumerate(ranking):
            scores[record_id] = scores.get(record_id, 0.0) + 1.0 / (RRF_K + index + 1)
    return scores


def looks_like_identifier(token: str) -> bool:
    """True for a token that names a thing rather than describing one.

    An identifier lookup is a near-certainty when it hits, and RRF deliberately
    discards score magnitude — at k=60 rank 1 scores 1/61 and rank 10 scores 1/70,
    about 13% apart — so without pinning, an exact identifier hit is flattened
    toward parity with a fuzzy one.

    A record id is matched by `vocab.ID_PATTERN` and NOT by the sha-shaped pattern:
    `rec-6c57ffcd1a8b5fd0` fails `[0-9a-f]{7,40}` because the hyphen splits the token
    and `r` is not a hex digit. Without the second test a pasted id stops
    being an identifier and silently loses its pin, which is the failure this function
    exists to prevent.

    The file-extension form requires a stem of at least two characters: without
    that, `e.g.` is an identifier and pins whatever prose contains it above every
    real match.
    """
    lowered = token.lower()
    if "/" in token or "@" in token:
        return True
    if _HEX_TOKEN.fullmatch(lowered) is not None:
        return True
    if vocab.ID_PATTERN.fullmatch(lowered) is not None:
        return True
    return _FILE_EXTENSION.search(token) is not None


def like_pattern(term: str) -> str:
    """`%term%`, lowercased, with LIKE metacharacters escaped.

    Unescaped, a term containing `%` or `_` matches far more than the term: `%` alone matches
    every row.
    """
    escaped = term.lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


# ---------------------------------------------------------------------------- search

# The similarity ranker contributes at most this many records whose cosine is at least the
# floor. An exploratory 8-record, 11-query measurement found that 0.20 kept three unrelated
# vector-only records, while 0.25 kept both related ones and no unrelated ones. Exact and
# meaningful keyword hits do not need to clear it.
VECTOR_DEPTH = 10
VECTOR_MIN_COSINE = 0.25

# Dolt 2.4.0 FULLTEXT ranked records that matched a query only through these words, including
# "the" above the record the query named. One definition drives both the SQL and its printed
# explanation. `not` is deliberately absent because dropping negation changes the request.
KEYWORD_STOPWORDS = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "did",
        "do",
        "does",
        "for",
        "from",
        "had",
        "has",
        "have",
        "how",
        "i",
        "in",
        "is",
        "it",
        "of",
        "on",
        "or",
        "that",
        "the",
        "this",
        "to",
        "was",
        "were",
        "what",
        "when",
        "where",
        "which",
        "who",
        "why",
        "will",
        "with",
    }
)

# The filters a no-match search reruns without, one at a time, in this order.
FILTER_NAMES: tuple[str, ...] = (
    "entry_type",
    "work_status",
    "tags",
    "projects",
    "unscoped",
    "since",
    "until",
    "links",
)


class Filters(BaseModel):
    """Hard constraints on which records a search may return."""

    entry_type: EntryType | None = None
    work_status: WorkStatus | None = None
    tags: list[str] = []
    projects: list[str] = []
    unscoped: bool = False
    since: date | None = None
    until: date | None = None
    links: tuple[Relation, str] | None = None
    include_history: bool = False

    def given(self) -> tuple[str, ...]:
        """The names in FILTER_NAMES that this search sets."""
        return tuple(name for name in FILTER_NAMES if getattr(self, name))

    def without(self, name: str) -> Filters:
        """A copy with the filter `name` removed."""
        empty: Any = [] if name in {"tags", "projects"} else False if name == "unscoped" else None
        return self.model_copy(update={name: empty})


class Ranked(BaseModel):
    """Every record a search matched, in final order, and which rankers returned each."""

    order: list[str]
    rankers: dict[str, dict[str, int]]  # record id -> ranker name -> 1-based rank
    vector: dict[str, VectorMatch]
    unembedded: int | None  # records with no chunk row for the model, within the project
    # filter when one is set; None when not asked


def run_search(
    store: Store,
    filters: Filters,
    *,
    query: str,
    terms: Sequence[str],
    query_vector: Sequence[float] | None,
    model_id: str | None,
    equivalence: dict[str, tuple[str, ...]],
    count_unembedded: bool = False,
) -> Ranked:
    """Run every ranker in ONE dolt invocation and fuse them.

    keyword: FULLTEXT MATCH over title and body after common English function words are
    removed. exact: each term, and each identifier-shaped query token, matched
    case-insensitively over every searchable column; a term under 3 characters matches as a
    whole word, because FULLTEXT never indexes it. vector: the nearest VECTOR_DEPTH records
    by their best chunk, keeping only cosine >= VECTOR_MIN_COSINE. With no query and no terms
    the filters alone select the records, newest first.

    Every value reaches SQL through `dolt.sql_literal`. `Store._bind` is not used, because it
    splices a later parameter into an earlier one that contains `?` (labbook-79y.7).
    """
    where = _where(filters, equivalence)
    exact_terms = _exact_terms(query, terms)
    keyword_query = " ".join(keyword_tokens(query))
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
        statements.append(vector.nearest_statement(query_vector, model_id, where))
    if not names:
        if query.strip() or any(term.strip() for term in terms):
            # A nonempty request reduced to no usable ranker, for example a stopword-only query
            # on a machine without the embedding extra. Listing the scope here would label every
            # record as a match; listing is only the explicit empty-query operation.
            return Ranked(order=[], rankers={}, vector={}, unembedded=None)
        names.append("listing")
        statements.append(
            f"SELECT r.id AS id, r.created_at AS created_at FROM record r WHERE {where} "
            "ORDER BY r.created_at DESC, r.id ASC"
        )
    if count_unembedded and model_id is not None:
        names.append("unembedded")
        statements.append(vector.unembedded_statement(model_id, filters.projects, filters.unscoped))
    results = dict(zip(names, dolt.read_result_sets(store.db_path, statements), strict=True))

    unembedded = int(results["unembedded"][0]["n"]) if "unembedded" in results else None
    if "listing" in results:
        order = _dedupe(str(row["id"]) for row in results["listing"])
        return Ranked(order=order, rankers={}, vector={}, unembedded=unembedded)

    created: dict[str, str] = {}
    rankings: dict[str, list[str]] = {}
    for name in ("keyword", "exact"):
        if name in results:
            rankings[name] = _dedupe(str(row["id"]) for row in results[name])
            created.update({str(row["id"]): str(row["created_at"]) for row in results[name]})
    nearest = [
        match
        for match in vector.parse_nearest(results.get("vector", []), VECTOR_DEPTH)
        if match.cosine >= VECTOR_MIN_COSINE
    ]
    for row in results.get("vector", []):
        created.setdefault(str(row["record_id"]), str(row["created_at"]))
    if nearest:
        rankings["vector"] = [match.record_id for match in nearest]

    scores = rrf_scores(list(rankings.values()))
    # Score first, then newest, then id; a stable sort applied in reverse priority order.
    fused = sorted(scores, key=lambda record_id: record_id)
    fused.sort(key=lambda record_id: created.get(record_id, ""), reverse=True)
    fused.sort(key=lambda record_id: scores[record_id], reverse=True)
    # An exact hit is a near-certainty and RRF flattens it toward a fuzzy one, so every
    # exact hit leads, in fused order among themselves.
    exact = set(rankings.get("exact", []))
    order = [i for i in fused if i in exact] + [i for i in fused if i not in exact]
    rankers = {
        record_id: {
            name: ranking.index(record_id) + 1
            for name, ranking in rankings.items()
            if record_id in ranking
        }
        for record_id in order
    }
    return Ranked(
        order=order,
        rankers=rankers,
        vector={match.record_id: match for match in nearest},
        unembedded=unembedded,
    )


def keyword_matches(
    query: str, title: str, body: str
) -> list[tuple[str, Literal["title", "body"]]]:
    """(token, column) for each query token that is a word of the title or the body.

    Decided by `split_words`, the same boundary rule the query is tokenized with, so a token
    reported here is a whole word of that column.
    """
    tokens = keyword_tokens(query)
    found: list[tuple[str, Literal["title", "body"]]] = []
    columns: tuple[tuple[Literal["title", "body"], str], ...] = (
        ("title", title),
        ("body", body),
    )
    for column, text in columns:
        words = set(split_words(text.lower()))
        found.extend((token, column) for token in tokens if token in words)
    return found


def _exact_terms(query: str, terms: Sequence[str]) -> list[str]:
    candidates = [term.strip() for term in terms]
    candidates += [token for token in tokenize(query) if looks_like_identifier(token)]
    unique: dict[str, str] = {}
    for term in candidates:
        for variant in _term_variants(term):
            if variant and variant.lower() not in unique:
                unique[variant.lower()] = variant
    return list(unique.values())


def _term_variants(term: str) -> tuple[str, ...]:
    """An exact term, plus the locally resolved form when it names an absolute path.

    Both stay in the query: the stored value may intentionally preserve a symlink while a code
    capture records git's canonical top level. On macOS this also pairs `/tmp` with
    `/private/tmp`. A URI is not a local path and is never rewritten.
    """
    if not term or "://" in term:
        return (term,)
    try:
        path = Path(term).expanduser()
        if not path.is_absolute():
            return (term,)
        resolved = str(path.resolve(strict=False))
    except (OSError, RuntimeError):
        return (term,)
    return (term,) if resolved == term else (term, resolved)


def _where(filters: Filters, equivalence: dict[str, tuple[str, ...]]) -> str:
    """The filters as one SQL condition over the record aliased `r`."""
    clauses = ["1 = 1"]
    if not filters.include_history:
        clauses.append("NOT EXISTS (SELECT 1 FROM retirement t WHERE t.record_id = r.id)")
    if filters.entry_type is not None:
        clauses.append(f"r.entry_type = {_lit(filters.entry_type)}")
    if filters.work_status is not None:
        if not filters.include_history:
            clauses.append(
                "NOT EXISTS (SELECT 1 FROM link replacement "
                "WHERE replacement.target_id = r.id "
                "AND replacement.relation = 'supersedes')"
            )
        clauses.append(
            "EXISTS (SELECT 1 FROM record_work_status w WHERE w.record_id = r.id "
            f"AND w.work_status = {_lit(filters.work_status)} AND NOT EXISTS ("
            "SELECT 1 FROM record_work_status newer WHERE newer.record_id = w.record_id "
            "AND newer.sequence > w.sequence))"
        )
    for tag in filters.tags:
        forms = ", ".join(_lit(form) for form in expand_tags(equivalence, [tag.strip().lower()]))
        clauses.append(
            f"EXISTS (SELECT 1 FROM record_tag g WHERE g.record_id = r.id AND g.tag IN ({forms}))"
        )
    if filters.projects or filters.unscoped:
        scope_parts: list[str] = []
        if filters.projects:
            ids = ", ".join(_lit(project_id) for project_id in filters.projects)
            scope_parts.append(
                "EXISTS (SELECT 1 FROM record_project_event membership WHERE "
                "membership.record_id = r.id AND membership.project_id IN ("
                f"{ids}) AND membership.action = 'add' AND NOT EXISTS (SELECT 1 FROM "
                "record_project_event newer_membership WHERE newer_membership.record_id = "
                "membership.record_id AND newer_membership.project_id = membership.project_id "
                "AND newer_membership.sequence > membership.sequence))"
            )
        if filters.unscoped:
            scope_parts.append(
                "NOT EXISTS (SELECT 1 FROM record_project_event membership WHERE "
                "membership.record_id = r.id AND membership.action = 'add' AND NOT EXISTS ("
                "SELECT 1 FROM record_project_event newer_membership WHERE "
                "newer_membership.record_id = membership.record_id AND "
                "newer_membership.project_id = membership.project_id AND "
                "newer_membership.sequence > membership.sequence))"
            )
        clauses.append(f"({' OR '.join(scope_parts)})")
    if filters.since is not None:
        clauses.append(f"r.created_at >= {_lit(datetime.combine(filters.since, time()))}")
    if filters.until is not None:
        end = datetime.combine(filters.until + timedelta(days=1), time())
        clauses.append(f"r.created_at < {_lit(end)}")
    if filters.links is not None:
        relation, other = (_lit(part) for part in filters.links)
        clauses.append(
            f"EXISTS (SELECT 1 FROM link l WHERE l.relation = {relation} AND "
            f"((l.source_id = r.id AND l.target_id = {other}) OR "
            f"(l.target_id = r.id AND l.source_id = {other})))"
        )
    return " AND ".join(clauses)


def _keyword_statement(query: str, where: str) -> str:
    q = _lit(query)
    title = f"MATCH(r.title) AGAINST ({q} IN NATURAL LANGUAGE MODE)"
    body = f"MATCH(r.body) AGAINST ({q} IN NATURAL LANGUAGE MODE)"
    return (
        f"SELECT r.id AS id, r.created_at AS created_at, {title} + {body} AS score "
        f"FROM record r WHERE ({title} OR {body}) AND {where} "
        "ORDER BY score DESC, r.created_at DESC, r.id ASC"
    )


def _exact_statement(terms: Sequence[str], where: str) -> str:
    conditions = [_term_condition(term) for term in terms]
    hits = " + ".join(f"(CASE WHEN {condition} THEN 1 ELSE 0 END)" for condition in conditions)
    return (
        f"SELECT r.id AS id, r.created_at AS created_at, {hits} AS hits FROM record r "
        f"WHERE ({' OR '.join(conditions)}) AND {where} "
        "ORDER BY hits DESC, r.created_at DESC, r.id ASC"
    )


def _term_condition(term: str) -> str:
    """One term against every searchable column, ignoring case.

    3 or more characters: LIKE '%term%' with metacharacters escaped. Under 3: a whole-word
    REGEXP_LIKE, so `AB` matches the word AB and not `about` (measured on dolt 2.4.0). The
    JSON value is lowercased first, because a JSON cast is binary-collated.
    """
    if len(term) >= 3:  # Dolt's minimum FULLTEXT token length
        pattern = _lit(like_pattern(term))

        def match(column: str) -> str:
            return f"{column} LIKE {pattern}"
    else:
        regex = _lit(rf"\b{re.escape(term.lower())}\b")

        def match(column: str) -> str:
            return f"REGEXP_LIKE({column}, {regex}, 'i')"

    return (
        f"({match('r.title')} OR {match('r.body')} OR {match('LOWER(CAST(r.value AS CHAR))')}"
        f" OR EXISTS (SELECT 1 FROM record_tag g WHERE g.record_id = r.id AND {match('g.tag')})"
        " OR EXISTS (SELECT 1 FROM artifact a WHERE a.record_id = r.id AND "
        f"({match('a.location')} OR {match('a.notes')}))"
        " OR EXISTS (SELECT 1 FROM code_capture c WHERE c.record_id = r.id AND "
        f"({match('c.repo_path')} OR {match('c.head_sha')} OR {match('c.captured_sha')}))"
        " OR EXISTS (SELECT 1 FROM link k WHERE k.source_id = r.id AND "
        f"{match('k.target_id')}))"
    )


def _lit(value: str | datetime) -> str:
    return dolt.sql_literal(value)


def _dedupe(record_ids: Iterator[str] | Sequence[str]) -> list[str]:
    return list(dict.fromkeys(record_ids))


# ---------------------------------------------------------- project discovery


class ProjectRanked(BaseModel):
    """Every project matched by identity or current framing, ranked separately from records."""

    order: list[str]
    rankers: dict[str, dict[str, int]]
    vector: dict[str, vector.ProjectVectorMatch]


def run_project_search(
    store: Store,
    *,
    query: str,
    query_vector: Sequence[float] | None,
    model_id: str | None,
) -> ProjectRanked:
    """Fuse exact project identity, current-framing keyword, and current-framing vector ranks."""
    text = query.strip()
    if not text:
        return ProjectRanked(order=[], rankers={}, vector={})
    keyword_query = " ".join(keyword_tokens(query))
    names: list[str] = ["exact"]
    statements = [_project_exact_statement(text)]
    if keyword_query:
        names.append("keyword")
        statements.append(_project_keyword_statement(keyword_query))
    if query_vector is not None and model_id is not None:
        names.append("vector")
        statements.append(vector.project_nearest_statement(query_vector, model_id))
    results = dict(zip(names, dolt.read_result_sets(store.db_path, statements), strict=True))
    rankings: dict[str, list[str]] = {"exact": _dedupe(str(row["id"]) for row in results["exact"])}
    if "keyword" in results:
        rankings["keyword"] = _dedupe(str(row["id"]) for row in results["keyword"])
    nearest = [
        match
        for match in vector.parse_project_nearest(
            results.get("vector", []), len(results.get("vector", []))
        )
        if match.cosine >= VECTOR_MIN_COSINE
    ]
    if nearest:
        rankings["vector"] = [match.project_id for match in nearest]
    created: dict[str, str] = {}
    for result in results.values():
        for row in result:
            identity = row.get("id", row.get("project_id"))
            if identity is not None:
                created[str(identity)] = str(row.get("updated_at", row.get("created_at", "")))
    scores = rrf_scores(list(rankings.values()))
    fused = sorted(scores)
    fused.sort(key=lambda item: created.get(item, ""), reverse=True)
    fused.sort(key=lambda item: scores[item], reverse=True)
    exact = set(rankings["exact"])
    order = [item for item in fused if item in exact] + [
        item for item in fused if item not in exact
    ]
    return ProjectRanked(
        order=order,
        rankers={
            project_id: {
                name: ranking.index(project_id) + 1
                for name, ranking in rankings.items()
                if project_id in ranking
            }
            for project_id in order
        },
        vector={match.project_id: match for match in nearest},
    )


def project_keyword_matches(
    query: str, name: str, title: str, body: str
) -> list[tuple[str, Literal["name", "framing_title", "framing_body"]]]:
    """Located whole-word keyword matches in current searchable project fields."""
    tokens = keyword_tokens(query)
    fields: tuple[tuple[Literal["name", "framing_title", "framing_body"], str], ...] = (
        ("name", name),
        ("framing_title", title),
        ("framing_body", body),
    )
    found: list[tuple[str, Literal["name", "framing_title", "framing_body"]]] = []
    for field, text in fields:
        words = set(split_words(text.lower()))
        found.extend((token, field) for token in tokens if token in words)
    return found


def _project_exact_statement(query: str) -> str:
    value = _lit(query)
    return (
        "SELECT p.id AS id, COALESCE(f.created_at, p.created_at) AS updated_at "
        "FROM project p LEFT JOIN project_framing_version f ON f.project_id = p.id "
        "AND NOT EXISTS (SELECT 1 FROM project_framing_version newer WHERE "
        "newer.project_id = f.project_id AND newer.version > f.version) WHERE "
        f"p.id = {value} OR p.name = {value} ORDER BY updated_at DESC, p.id ASC"
    )


def _project_keyword_statement(query: str) -> str:
    q = _lit(query)
    tokens = keyword_tokens(query)
    name_conditions = [f"p.name LIKE {_lit(like_pattern(token))}" for token in tokens]
    name_match = " OR ".join(name_conditions) if name_conditions else "FALSE"
    title = f"MATCH(f.title) AGAINST ({q} IN NATURAL LANGUAGE MODE)"
    body = f"MATCH(f.body) AGAINST ({q} IN NATURAL LANGUAGE MODE)"
    return (
        "SELECT p.id AS id, COALESCE(f.created_at, p.created_at) AS updated_at, "
        f"({title} + {body}) AS score FROM project p LEFT JOIN "
        "project_framing_version f ON f.project_id = p.id AND NOT EXISTS (SELECT 1 FROM "
        "project_framing_version newer WHERE newer.project_id = f.project_id AND "
        "newer.version > f.version) WHERE "
        f"({name_match} OR {title} OR {body}) ORDER BY score DESC, updated_at DESC, p.id ASC"
    )


# ------------------------------------------------------------------ show and lineage

LINEAGE_DEPTH = 5


def incoming_links(store: Store, record_id: str) -> list[tuple[Relation, str]]:
    """(relation, source id) of every link onto `record_id`, oldest first."""
    rows = dolt.read_result_sets(
        store.db_path,
        [
            "SELECT source_id, relation, created_at FROM link "
            f"WHERE target_id = {_lit(record_id)} ORDER BY created_at ASC, source_id ASC"
        ],
    )[0]
    return [(_relation(row["relation"]), str(row["source_id"])) for row in rows]


def lineage_walk(
    store: Store, record_id: str, depth: int = LINEAGE_DEPTH
) -> tuple[list[tuple[Relation, str, int]], list[tuple[Relation, str, int]]]:
    """Ancestors and descendants of `record_id` over builds_on and supersedes links.

    An ancestor is reached by following links from source to target (what the record was
    built on, or the version it replaced); a descendant by following them backwards. Each
    entry is (relation of the link it was first reached by, id, steps), breadth first, up to
    `depth` steps. One read of every such link in the store.
    """
    rows = dolt.read_result_sets(
        store.db_path,
        [
            "SELECT source_id, relation, target_id FROM link "
            "WHERE relation = 'builds_on' OR relation = 'supersedes' "
            "ORDER BY source_id ASC, target_id ASC"
        ],
    )[0]
    forward: dict[str, list[tuple[Relation, str]]] = {}
    backward: dict[str, list[tuple[Relation, str]]] = {}
    for row in rows:
        source, target, relation = str(row["source_id"]), str(row["target_id"]), row["relation"]
        forward.setdefault(source, []).append((_relation(relation), target))
        backward.setdefault(target, []).append((_relation(relation), source))
    return _walk(forward, record_id, depth), _walk(backward, record_id, depth)


def incoming_links_many(
    store: Store, record_ids: Sequence[str]
) -> dict[str, list[tuple[Relation, str]]]:
    """Every incoming link onto `record_ids`, grouped by target, in creation order."""
    ids = list(dict.fromkeys(record_ids))
    if not ids:
        return {}
    within = ", ".join(_lit(record_id) for record_id in ids)
    rows = dolt.read_result_sets(
        store.db_path,
        [
            "SELECT source_id, relation, target_id, created_at FROM link "
            f"WHERE target_id IN ({within}) ORDER BY created_at ASC, source_id ASC"
        ],
    )[0]
    grouped: dict[str, list[tuple[Relation, str]]] = {}
    for row in rows:
        grouped.setdefault(str(row["target_id"]), []).append(
            (_relation(row["relation"]), str(row["source_id"]))
        )
    return grouped


def _walk(
    edges: dict[str, list[tuple[Relation, str]]], start: str, depth: int
) -> list[tuple[Relation, str, int]]:
    reached: list[tuple[Relation, str, int]] = []
    seen = {start}
    frontier = [start]
    for step in range(1, depth + 1):
        following: list[str] = []
        for current in frontier:
            for relation, other in edges.get(current, []):
                if other not in seen:
                    seen.add(other)
                    reached.append((relation, other, step))
                    following.append(other)
        frontier = following
    return reached


def _relation(value: Any) -> Relation:
    for relation in vocab.RELATIONS:
        if value == relation:
            return relation
    raise ValueError(f"unknown link relation {value!r} read from the store")


# --------------------------------------------------------------------- prime


class Overview(BaseModel):
    """The ids and disjoint status counts prime needs for one project."""

    current_count: int
    replaced_count: int
    retired_count: int
    current: list[str]
    problem_statements: list[str]


class ProjectIndexRow(BaseModel):
    """Internal whole-store project index row."""

    project_id: str
    name: str
    framing_title: str | None
    record_count: int
    open_count: int
    last_activity: datetime


def overview(store: Store, project_id: str) -> Overview:
    """Every project-scoped prime index query in one Dolt invocation."""
    scope = _current_membership("r", _lit(project_id))
    current = f"{scope} AND {_LIVE} AND {_NOT_SUPERSEDED}"
    replaced = f"{scope} AND {_LIVE} AND NOT ({_NOT_SUPERSEDED})"
    statements = [
        f"SELECT COUNT(*) AS n FROM record r WHERE {current}",
        f"SELECT COUNT(*) AS n FROM record r WHERE {replaced}",
        f"SELECT COUNT(*) AS n FROM record r WHERE {scope} AND NOT ({_LIVE})",
        _ids(current),
        _ids(f"{current} AND {_typed('problem-statement')}"),
    ]
    counts_current, counts_replaced, counts_retired, ids, problems = dolt.read_result_sets(
        store.db_path, statements
    )
    return Overview(
        current_count=int(counts_current[0]["n"]),
        replaced_count=int(counts_replaced[0]["n"]),
        retired_count=int(counts_retired[0]["n"]),
        current=[str(row["id"]) for row in ids],
        problem_statements=[str(row["id"]) for row in problems],
    )


def project_index(store: Store) -> tuple[list[ProjectIndexRow], int]:
    """Current project counts, open counts, activity, and current unscoped count."""
    current_member = _current_membership("r", "p.id")
    current_record = f"{_LIVE} AND {_NOT_SUPERSEDED}"
    latest_open = (
        "EXISTS (SELECT 1 FROM record_work_status w WHERE w.record_id = r.id "
        "AND w.work_status = 'open' AND NOT EXISTS (SELECT 1 FROM record_work_status newer "
        "WHERE newer.record_id = w.record_id AND newer.sequence > w.sequence))"
    )
    projects, unscoped = dolt.read_result_sets(
        store.db_path,
        [
            "SELECT p.id, p.name, p.created_at, "
            "(SELECT f.title FROM project_framing_version f WHERE f.project_id = p.id "
            "ORDER BY f.version DESC LIMIT 1) AS framing_title, "
            "(SELECT MAX(f.created_at) FROM project_framing_version f WHERE "
            "f.project_id = p.id) AS framing_at, "
            "(SELECT MAX(e.created_at) FROM record_project_event e WHERE "
            "e.project_id = p.id) AS membership_at, "
            "(SELECT MAX(r.created_at) FROM record r WHERE "
            f"{current_member} AND {current_record}) AS record_at, "
            "(SELECT COUNT(*) FROM record r WHERE "
            f"{current_member} AND {current_record}) AS record_count, "
            "(SELECT COUNT(*) FROM record r WHERE "
            f"{current_member} AND {current_record} AND {latest_open}) AS open_count "
            "FROM project p ORDER BY p.name, p.id",
            "SELECT COUNT(*) AS n FROM record r WHERE "
            f"{current_record} AND NOT EXISTS (SELECT 1 FROM record_project_event e WHERE "
            "e.record_id = r.id AND e.action = 'add' AND NOT EXISTS (SELECT 1 FROM "
            "record_project_event newer WHERE newer.record_id = e.record_id AND "
            "newer.project_id = e.project_id AND newer.sequence > e.sequence))",
        ],
    )
    rows: list[ProjectIndexRow] = []
    for row in projects:
        moments = [
            _as_index_datetime(row[key])
            for key in ("created_at", "framing_at", "membership_at", "record_at")
            if row.get(key) is not None
        ]
        rows.append(
            ProjectIndexRow(
                project_id=str(row["id"]),
                name=str(row["name"]),
                framing_title=(
                    str(row["framing_title"]) if row.get("framing_title") is not None else None
                ),
                record_count=int(row["record_count"]),
                open_count=int(row["open_count"]),
                last_activity=max(moments),
            )
        )
    return rows, int(unscoped[0]["n"])


def _current_membership(record_alias: str, project_id: str) -> str:
    return (
        "EXISTS (SELECT 1 FROM record_project_event membership WHERE membership.record_id = "
        f"{record_alias}.id AND membership.project_id = {project_id} AND membership.action = "
        "'add' AND NOT EXISTS (SELECT 1 FROM record_project_event newer_membership WHERE "
        "newer_membership.record_id = membership.record_id AND "
        "newer_membership.project_id = membership.project_id AND "
        "newer_membership.sequence > membership.sequence))"
    )


def _as_index_datetime(value: Any) -> datetime:
    return datetime.fromisoformat(str(value).replace(" ", "T")).replace(tzinfo=UTC)


_LIVE = "NOT EXISTS (SELECT 1 FROM retirement t WHERE t.record_id = r.id)"
_NOT_SUPERSEDED = (
    "NOT EXISTS (SELECT 1 FROM link s WHERE s.relation = 'supersedes' AND s.target_id = r.id)"
)


def _typed(entry_type: EntryType) -> str:
    return f"r.entry_type = {_lit(entry_type)}"


def _ids(condition: str) -> str:
    return f"SELECT r.id AS id FROM record r WHERE {condition} ORDER BY r.created_at DESC, r.id ASC"
