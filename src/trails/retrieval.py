"""Retrieval: the search rankers and the primitives they are built on.

Reciprocal-rank fusion, identifier detection, the one word-boundary rule, tag alias
expansion, and project discovery. Entry search, built on these, is in `entry_search`.

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
from datetime import datetime
from typing import Literal

from pydantic import BaseModel

from trails import dolt, vector, vocab
from trails.store import Store

RRF_K = 60

# Kept anywhere in a word, because they carry meaning: `100%` is a real query,
# `cross-encoder` is one term, and `/ @ .` are what make a token look like an
# identifier. Trimmed at the ends only, because there they are sentence
# punctuation: `outcome:` is the word `outcome`.
_WORD_CHARS = "_-/@%.:"
_TRIM_CHARS = ".,;:!?-()[]{}<>\"'"
# A sha-shaped token. Git's abbreviated form starts at 7 characters and a full
# sha1 is 40. It does NOT match a record id: the hyphen splits the token and no
# prefix is hex, which is why `vocab.ENTRY_ID_PATTERN` is checked separately in
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

    An entry id is matched by `vocab.ENTRY_ID_PATTERN` and NOT by the sha-shaped pattern:
    `tsk-6c57f` fails `[0-9a-f]{7,40}` because the hyphen splits the token
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
    if vocab.ENTRY_ID_PATTERN.fullmatch(lowered) is not None:
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


# filter when one is set; None when not asked


def _lit(value: str | datetime) -> str:
    return dolt.sql_literal(value)


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


# --------------------------------------------------------------------- prime


_LIVE = "NOT EXISTS (SELECT 1 FROM retirement t WHERE t.record_id = r.id)"
_NOT_SUPERSEDED = (
    "NOT EXISTS (SELECT 1 FROM link s WHERE s.relation = 'supersedes' AND s.target_id = r.id)"
)


def _dedupe(ids: Iterator[str] | Sequence[str]) -> list[str]:
    return list(dict.fromkeys(ids))
