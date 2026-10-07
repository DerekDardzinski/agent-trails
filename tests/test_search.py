"""Tests for the search operation against real Dolt, with the engine faked at its seam."""

from __future__ import annotations

import asyncio
import math
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from tests.fakes import FakeEngine, basis
from trails import embeddings, engine as engine_seam, retrieval
from trails.embeddings import EmbeddingEngine
from trails.models import ArtifactRef, CodeCapture, Link, RecordDraft
from trails.ops import context
from trails.ops._read_types import SearchResult
from trails.ops.search import search
from trails.store import Store
from trails.vector import embed_records


class _Engine(FakeEngine):
    """Model present. A text containing any of `near` embeds to basis(1), anything else to
    basis(2), so only those texts are similar to a query containing one of them."""

    def __init__(self, near: Sequence[str]) -> None:
        super().__init__()
        self.near = tuple(near)

    def model_present(self) -> bool:
        return True

    def embed(self, text: str) -> Sequence[float]:
        self.calls.append(text)
        return basis(1) if any(word in text for word in self.near) else basis(2)


def _search(store: Store, **arguments: Any) -> SearchResult:
    async def go() -> SearchResult:
        return await context.bind(search, lambda: store.db_path)(**arguments)

    return asyncio.run(go())


def _at_cosine(value: float) -> list[float]:
    """A unit vector whose cosine with basis(0) is exactly `value`."""
    vector = [0.0] * 384
    vector[0] = value
    vector[1] = math.sqrt(1.0 - value * value)
    return vector


def _write(store: Store, title: str, body: str = "", **fields: Any) -> str:
    fields.setdefault("entry_type", "note")
    fields.setdefault("work_status", "closed")
    return store.write_record(RecordDraft(title=title, body=body, **fields)).record.id


def _ids(result: SearchResult) -> list[str]:
    return [hit.id for hit in result.hits]


def test_keyword_finds_a_body_word_and_a_capitalized_title_word(store: Store) -> None:
    """Catches a case-sensitive MATCH (the default binary collation) and a body column left
    out of the keyword ranker."""
    by_body = _write(store, "Notes", "the uplift estimate held")
    by_title = _write(store, "Sync Results Green", "nothing here")
    _write(store, "Unrelated", "other words entirely")

    assert _ids(_search(store, query="uplift")) == [by_body]
    result = _search(store, query="green")
    assert _ids(result) == [by_title]
    assert result.hits[0].matched_by == ["keyword"]
    assert result.hits[0].excerpt is None
    assert result.render_text().splitlines()[3] == '  matched by: keyword "green" in title'


def test_a_body_keyword_excerpt_is_centered_bounded_and_whitespace_collapsed(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches a body keyword plus semantic match returning the full chunk instead of the
    centered bounded window, or preserving authored whitespace runs."""
    engine = _Engine(near=("P99", "p99"))
    monkeypatch.setattr(engine_seam, "build_engine", lambda: engine)
    left = " ".join(f"left{index}" for index in range(30))
    right = " ".join(f"right{index}" for index in range(40))
    record_id = _write(store, "Latency result", f"{left}\n\tP99   {right}")
    embed_records(store, engine, [record_id])

    result = _search(store, query="p99")
    hit = result.hits[0]

    assert hit.id == record_id
    assert hit.matched_by == ["keyword", "semantic"]
    assert hit.keyword_matches[0].model_dump() == {"term": "p99", "field": "body"}
    assert hit.excerpt is not None
    assert hit.excerpt.startswith("...") and hit.excerpt.endswith("...")
    assert "left29" in hit.excerpt and "P99" in hit.excerpt and "right0" in hit.excerpt
    assert "\n" not in hit.excerpt and "  " not in hit.excerpt
    assert len(hit.excerpt) <= 240


def test_keyword_excerpt_uses_the_whole_word_that_made_the_hit(store: Store) -> None:
    """Catches an earlier query substring inside another word displacing the later whole-word
    match that the keyword ranker actually used."""
    body = "prefix concatenate " + "filler " * 50 + "cat decisive evidence"
    record_id = _write(store, "Animal", body)

    hit = _search(store, query="cat").hits[0]

    assert hit.id == record_id
    assert hit.keyword_matches[0].model_dump() == {"term": "cat", "field": "body"}
    assert hit.excerpt is not None
    assert "cat decisive evidence" in hit.excerpt
    assert "concatenate" not in hit.excerpt


def test_public_match_methods_have_keyword_exact_semantic_order(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches internal ranker insertion order or the vector name leaking into the public
    contract when all three search methods return the same record."""
    engine = _Engine(near=("Needle", "needle"))
    monkeypatch.setattr(engine_seam, "build_engine", lambda: engine)
    record_id = _write(store, "Needle outcome", "the needle remained visible")
    embed_records(store, engine, [record_id])

    hit = _search(store, query="needle", terms=["needle"]).hits[0]

    assert hit.id == record_id
    assert hit.matched_by == ["keyword", "exact", "semantic"]


def test_ten_large_records_produce_compact_structured_hits_without_body_tails(
    store: Store,
) -> None:
    """Catches SearchHit regaining a full Record or retrieval diagnostics: ten 50,000-character
    bodies must stay below the approved structured-response cap and hide every sentinel tail."""
    sentinels = []
    for index in range(10):
        sentinel = f"SENTINEL_TAIL_{index}"
        sentinels.append(sentinel)
        prefix = f"compactmarker record {index} "
        body = prefix + ("x" * (50_000 - len(prefix) - len(sentinel))) + sentinel
        assert len(body) == 50_000
        _write(
            store,
            f"Compact payload record {index}",
            body,
            project="search-size-measurement",
        )

    result = _search(store, query="compactmarker", limit=10)
    payload = result.model_dump_json().encode("utf-8")
    expected_fields = {
        "id",
        "entry_type",
        "title",
        "project",
        "created_at",
        "lifecycle",
        "superseded_by",
        "work_status",
        "tags",
        "matched_by",
        "keyword_matches",
        "excerpt",
    }

    assert len(result.hits) == 10
    assert set(result.model_dump()) == {
        "state",
        "hits",
        "total",
        "offset",
        "filters",
        "relaxed",
        "tag_aliases",
        "next",
    }
    assert all(set(hit.model_dump()) == expected_fields for hit in result.hits)
    assert len(payload) < 10_000
    assert all(sentinel.encode() not in payload for sentinel in sentinels)


def test_a_two_character_term_matches_only_the_whole_word(store: Store) -> None:
    """Catches a short term matched as a substring (finding `about`) or not at all, since
    FULLTEXT never indexes a token under 3 characters."""
    whole = _write(store, "Test plan", "the AB split ran twice")
    _write(store, "Other", "about the weather")

    assert _ids(_search(store, terms=["ab"])) == [whole]


def test_exact_terms_reach_artifact_locations_and_captured_paths_and_shas(
    store: Store, tmp_path: Path
) -> None:
    """Catches a searchable column left out of the exact ranker, or an absolute path not
    searched in its resolved form: these strings appear in no title or body."""
    uri = "s3://bucket/runs/2026/out.parquet"
    sha = "0123456789abcdef0123456789abcdef01234567"
    real = tmp_path / "real-repo"
    real.mkdir()
    alias = tmp_path / "repo-alias"
    alias.symlink_to(real, target_is_directory=True)
    with_artifact = _write(store, "Run", artifacts=[ArtifactRef(location=uri, digest=None)])
    with_code = _write(
        store,
        "Code",
        code=[
            CodeCapture(repo_path=str(real.resolve()), head_sha=None, captured_sha=sha, dirty=True)
        ],
    )

    assert _ids(_search(store, terms=[uri.upper()])) == [with_artifact]
    assert _ids(_search(store, terms=[str(alias)])) == [with_code]
    assert _ids(_search(store, query=f"commit {sha}")) == [with_code]


def test_a_paraphrase_is_found_by_the_vector_ranker_alone(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches the vector ranker not running when the engine is ready; no query word
    appears in the record, so neither the keyword nor the exact ranker can return it."""
    engine = _Engine(near=("Cats", "felines"))
    monkeypatch.setattr(engine_seam, "build_engine", lambda: engine)
    cats = _write(store, "Cats", "sleep most of the day")
    other = _write(store, "Dogs", "walk twice daily")
    embed_records(store, engine, [cats, other])

    result = _search(store, query="how long do felines nap")

    assert _ids(result) == [cats]
    assert result.hits[0].matched_by == ["semantic"]
    assert result.render_text().splitlines()[0] == (
        "state: embeddings ready (all-MiniLM-L6-v2/model_O4) · 0 unembedded · 1 match"
    )


def test_a_semantic_match_uses_the_selected_chunk_without_public_diagnostics(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches a semantic excerpt taken from the first body chunk instead of the internally
    selected chunk, or retrieval score and chunk diagnostics leaking into the public hit."""
    engine = _Engine(near=("zebra", "striped"))
    monkeypatch.setattr(engine_seam, "build_engine", lambda: engine)
    selected = ("a zebra selected chunk. " * 20).strip()
    body = "\n\n".join([" ".join(["alpha"] * 150), " ".join(["beta"] * 150), selected])
    long_id = _write(store, "Long", body)
    embed_records(store, engine, [long_id])

    result = _search(store, query="striped")
    hit = result.hits[0]
    dumped = hit.model_dump()
    lines = result.render_text().splitlines()

    assert hit.id == long_id
    assert hit.matched_by == ["semantic"]
    assert hit.excerpt == selected
    assert len(hit.excerpt) > 240
    assert "alpha" not in hit.excerpt and "beta" not in hit.excerpt
    assert {"rank", "rankers", "cosine", "chunk", "chunk_index"}.isdisjoint(dumped)
    assert lines[3] == "  matched by: semantic"
    assert lines[4] == f"  excerpt: {hit.excerpt}"


def test_ten_semantic_full_chunks_stay_below_the_approved_response_target(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches semantic hits regaining the 240-character clip or ten representative full
    chunks exceeding the user-approved structured-response target."""
    engine = _Engine(near=("unrelated", "context"))
    monkeypatch.setattr(engine_seam, "build_engine", lambda: engine)
    body_by_id = {}
    for index in range(10):
        body = " ".join([f"context{index}"] * 80)
        record_id = _write(
            store,
            f"Stored entry {index}",
            body,
            project="chunk-size-measurement",
        )
        body_by_id[record_id] = body
    embed_records(store, engine, list(body_by_id))

    result = _search(store, query="unrelated semantic request")
    payload = result.model_dump_json().encode("utf-8")

    assert len(result.hits) == 10
    assert all(hit.matched_by == ["semantic"] for hit in result.hits)
    assert all(hit.excerpt == body_by_id[hit.id] for hit in result.hits)
    assert all(len(hit.excerpt or "") == 719 for hit in result.hits)
    assert len(payload) < 13_000


def test_a_tag_filter_that_hides_a_match_reports_it(store: Store) -> None:
    """Criterion 4. Catches the no-match report omitting what removing a filter would find,
    which leaves an agent concluding the store holds nothing."""
    hidden = _write(store, "CUPED variance note", "use CUPED", entry_type="note")

    text = _search(store, query="CUPED", tags=["decision"], project=None).render_text()

    assert text.splitlines() == [
        "state: embeddings unavailable: dependencies not installed · keyword and exact "
        "matching only · 0 matches",
        "filters applied: tags=[decision]",
        f"without the tags filter: 1 match ({hidden}, note)",
        'tag aliases: none for "decision"',
        'Next: search(query="CUPED", include_history=true) · if nothing relevant, stop',
    ]


def test_missing_model_files_leave_keyword_search_working_and_download_nothing(
    store: Store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Criterion 5. Catches a read downloading the model, and the vector failure taking the
    keyword results down with it."""
    models = tmp_path / "models"
    models.mkdir()
    monkeypatch.setattr(embeddings, "embedding_available", lambda: True)

    def no_download(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("a reading operation tried to download the model")

    monkeypatch.setattr(embeddings, "_download_file", no_download)
    monkeypatch.setattr(engine_seam, "build_engine", lambda: EmbeddingEngine(models_dir=models))
    found = _write(store, "Lift report", "lift was flat")

    result = _search(store, query="lift")

    assert _ids(result) == [found]
    assert "embeddings unavailable: model not downloaded; run embed" in result.state
    assert list(models.iterdir()) == []


def test_retired_records_are_hidden_and_superseded_ones_labeled(store: Store) -> None:
    """Criterion 6. Catches a retired record returned by default, or a superseded record
    dropped or unlabeled."""
    retired = _write(store, "Old lift fact", "lift")
    store.retire(retired, "wrong")
    old = _write(store, "Lift procedure", "lift steps")
    new = _write(store, "Lift procedure v2", "lift steps", supersedes=old, reason="fixed")

    default = _search(store, query="lift")
    assert sorted(_ids(default)) == sorted([old, new])
    assert f"superseded by {new}" in default.render_text()
    assert retired in _ids(_search(store, query="lift", include_history=True))


def test_links_filter_returns_exactly_the_contradicting_records(store: Store) -> None:
    """Criterion 7. Catches one direction of the link missed, or another relation leaking
    in."""
    target = _write(store, "Claim")
    outgoing = _write(store, "Rebuttal", links=[Link(relation="contradicts", target_id=target)])
    incoming = _write(store, "Counter")
    store.add_link(target, "contradicts", incoming)
    _write(store, "Builds", links=[Link(relation="builds_on", target_id=target)])

    result = _search(store, links=f"contradicts:{target}")

    assert sorted(_ids(result)) == sorted([outgoing, incoming])


def test_paging_reports_the_remaining_count_and_the_offset_call(store: Store) -> None:
    """Criterion 3 (search). Catches a wrong remaining count or a Next: call that would
    repeat the same page."""
    for n in range(3):
        _write(store, f"Lift note {n}", "lift")

    lines = _search(store, query="lift", limit=2).render_text().splitlines()

    assert lines[0].startswith("state:")
    assert lines[-1] == 'Next: 1 more · search(query="lift", limit=2, offset=2)'


def test_date_bounds_are_inclusive_days_and_project_is_equality(store: Store) -> None:
    """Catches until excluding its own day, since excluding its own day, and the project
    filter leaking another project's records."""
    today = datetime.now(UTC).date()
    here = _write(store, "Lift here", "lift", project="p1")
    _write(store, "Lift there", "lift", project="p2")

    assert _ids(_search(store, query="lift", project="p1", until=today.isoformat())) == [here]
    assert _ids(_search(store, query="lift", project="p1", since=today.isoformat())) == [here]
    day_before = (today - timedelta(days=1)).isoformat()
    day_after = (today + timedelta(days=1)).isoformat()
    assert _ids(_search(store, query="lift", project="p1", until=day_before)) == []
    assert _ids(_search(store, query="lift", project="p1", since=day_after)) == []


def test_a_tag_filter_also_matches_the_tags_aliases(store: Store) -> None:
    """Catches a tag filter compared literally, which hides every record tagged with an
    alias of the requested tag."""
    store.write_tag_alias("choice", "decision")
    aliased = _write(store, "Lift choice", "lift", tags=["choice"])
    _write(store, "Lift note", "lift", entry_type="note")

    assert _ids(_search(store, query="lift", tags=["decision"])) == [aliased]


def test_an_exact_hit_leads_a_tied_keyword_hit(store: Store) -> None:
    """Catches exact hits left in fused order: both records rank 1 in one ranker, so without
    the pin the newer keyword hit would lead."""
    uri = "s3://bucket/lift.csv"
    exact = _write(store, "Data", artifacts=[ArtifactRef(location=uri, digest=None)])
    keyword = _write(store, "Lift lift", "lift")

    result = _search(store, query=f"lift {uri}")

    assert _ids(result) == [exact, keyword]
    assert result.hits[0].matched_by == ["exact"]
    assert result.hits[0].excerpt is None


class _Truncating(_Engine):
    """Like _Engine, but sees only the first 256 words of a text, as the real tokenizer
    truncates at 256 tokens."""

    def embed(self, text: str) -> Sequence[float]:
        return super().embed(" ".join(text.split()[:256]))


def test_the_last_sentence_of_a_long_paragraph_is_found_by_similarity(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """labbook-79y.6 item 13. Catches a long paragraph embedded as one chunk, whose final
    sentence the tokenizer truncates away."""
    engine = _Truncating(near=("zebra", "striped"))
    monkeypatch.setattr(engine_seam, "build_engine", lambda: engine)
    filler = " ".join(f"{' '.join(['alpha'] * 29)} end." for _ in range(10))
    long_id = _write(store, "Long", f"{filler} The zebra was seen.")
    embed_records(store, engine, [long_id])

    result = _search(store, query="striped")

    assert _ids(result) == [long_id]
    assert result.hits[0].matched_by == ["semantic"]


def test_a_vector_only_hit_breaks_a_score_tie_by_creation_date(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """labbook-79y.6 item 12. Catches a vector-only hit sorting as created "" on an RRF tie,
    which puts an older keyword hit first whatever the dates."""
    engine = _Engine(near=("Cats", "felines"))
    monkeypatch.setattr(engine_seam, "build_engine", lambda: engine)
    older = _write(store, "Uplift notes", "the uplift held")
    newer = _write(store, "Cats", "they nap all day")
    embed_records(store, engine, [newer])

    result = _search(store, query="uplift felines")

    assert _ids(result) == [newer, older]
    assert [hit.matched_by for hit in result.hits] == [["semantic"], ["keyword"]]


def test_the_state_line_counts_unembedded_records_in_the_filtered_project_only(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """labbook-79y.6 item 18, from the labbook-79y.5 probe. Catches a store-wide count
    printed beside a project filter, which disagrees with lint for that project."""
    engine = _Engine(near=())
    monkeypatch.setattr(engine_seam, "build_engine", lambda: engine)
    inside = _write(store, "Inside", project="p")
    _write(store, "Elsewhere", project="q")
    embed_records(store, engine, [inside])

    state = _search(store, query="Inside", project="p").render_text().splitlines()[0]

    assert state == (
        "state: embeddings ready (all-MiniLM-L6-v2/model_O4) · 0 unembedded · project p · 1 match"
    )


def test_stopword_only_matches_do_not_bypass_the_vector_floor(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches a record matching only `the` or `how` surviving as a keyword hit even when
    its vector similarity is below the floor, while preserving a meaningful keyword hit."""
    query = "what did the onboarding experiment show"
    engine = FakeEngine({query: basis(0)})
    relevant = _write(store, "Onboarding experiment result", "retention rose")
    stopwords_only = _write(store, "How to rotate credentials", "the scheduled procedure")
    vector_only = _write(store, "New user flow outcome", "day-seven use increased")
    store.upsert_chunk_embeddings(
        [
            (relevant, 0, engine.model_id(), _at_cosine(0.10)),
            (stopwords_only, 0, engine.model_id(), _at_cosine(0.24)),
            (vector_only, 0, engine.model_id(), _at_cosine(0.26)),
        ]
    )
    monkeypatch.setattr(engine_seam, "build_engine", lambda: engine)

    result = _search(store, query=query)

    assert set(_ids(result)) == {relevant, vector_only}
    by_id = {hit.id: hit for hit in result.hits}
    assert by_id[relevant].matched_by == ["keyword"]
    assert by_id[vector_only].matched_by == ["semantic"]


def test_short_query_vector_only_results_must_clear_point_25(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches the retrieval floor being absent, rounded down, or applied to exact hits."""
    query = "slow writes"
    engine = FakeEngine({query: basis(0)})
    related = _write(store, "Cache eviction regression", "tail response times rose")
    unrelated = _write(store, "Firmware schedule", "device rollout calendar")
    exact_but_low = _write(store, "Other", "artifact details", entry_type="note")
    store.upsert_chunk_embeddings(
        [
            (related, 0, engine.model_id(), _at_cosine(0.31)),
            (unrelated, 0, engine.model_id(), _at_cosine(0.24)),
            (exact_but_low, 0, engine.model_id(), _at_cosine(-0.10)),
        ]
    )
    monkeypatch.setattr(engine_seam, "build_engine", lambda: engine)

    result = _search(store, query=query, terms=["artifact details"])

    assert _ids(result) == [exact_but_low, related]
    assert result.hits[0].matched_by == ["exact"]
    assert result.hits[1].matched_by == ["semantic"]


def test_no_match_hint_drops_the_project_and_never_repeats_the_same_call(store: Store) -> None:
    """Catches a failed project-scoped search suggesting the identical call, and an already
    broad search suggesting itself forever."""
    scoped = _search(store, query="not-present", project="mint").render_text().splitlines()
    broad = _search(store, query="not-present").render_text().splitlines()
    history = _search(store, query="not-present", include_history=True).render_text().splitlines()

    assert scoped[-1] == (
        'Next: search(query="not-present", include_history=true) · if nothing relevant, stop'
    )
    assert broad[-1] == (
        'Next: search(query="not-present", include_history=true) · if nothing relevant, stop'
    )
    assert history[-1] == "Next: nothing left to broaden; stop"


def test_stopword_only_query_without_vector_search_returns_no_matches(
    store: Store,
) -> None:
    """Catches a nonempty query reduced to no keyword tokens falling into the empty-query
    listing branch and labeling the whole store or project as matches."""
    _write(store, "Alpha", "unrelated words", project="p")
    _write(store, "Beta", "unrelated words", project="p")
    _write(store, "Gamma", "unrelated words", project="q")

    whole = _search(store, query="what is the")
    scoped = _search(store, query="how to", project="p")

    assert _ids(whole) == []
    assert _ids(scoped) == []
    assert whole.render_text().splitlines()[-1] == (
        'Next: search(query="what is the", include_history=true) · if nothing relevant, stop'
    )
    assert scoped.render_text().splitlines()[-1] == (
        'Next: search(query="how to", include_history=true) · if nothing relevant, stop'
    )


def test_entry_type_and_work_status_are_independent_search_filters(store: Store) -> None:
    """Catches type inferred from tags or work-status search reading the initial event only."""
    open_hypothesis = _write(
        store,
        "Open claim",
        tags=["latency"],
        entry_type="hypothesis",
        work_status="open",
    )
    closed_hypothesis = _write(
        store,
        "Closed claim",
        tags=["latency"],
        entry_type="hypothesis",
        work_status="open",
    )
    note = _write(store, "Open note", tags=["latency"], work_status="open")
    store.set_work_status(closed_hypothesis, "closed", "planned analysis completed")

    assert set(_ids(_search(store, entry_type="hypothesis"))) == {
        open_hypothesis,
        closed_hypothesis,
    }
    open_result = _search(store, work_status="open")
    assert set(_ids(open_result)) == {open_hypothesis, note}
    assert all(hit.matched_by == [] and hit.excerpt is None for hit in open_result.hits)
    assert _ids(_search(store, entry_type="hypothesis", work_status="closed")) == [
        closed_hypothesis
    ]
    assert set(_ids(_search(store, tags=["latency"]))) == {
        open_hypothesis,
        closed_hypothesis,
        note,
    }


def test_work_status_search_restores_superseded_and_retired_only_with_history(
    store: Store,
) -> None:
    """Catches an operational open-work query returning a replaced version, or history mode
    failing to restore that record with its superseded lifecycle."""
    old = _write(store, "Open question", work_status="open")
    replacement = _write(
        store,
        "Question answered",
        work_status="closed",
        supersedes=old,
        reason="the result resolved it",
    )

    retired = _write(store, "Retired open question", work_status="open")
    store.retire(retired, "no longer relevant")
    current_only = _search(store, work_status="open")
    with_history = _search(store, work_status="open", include_history=True)

    assert _ids(current_only) == []
    assert set(_ids(with_history)) == {old, retired}
    by_id = {hit.id: hit for hit in with_history.hits}
    assert by_id[old].lifecycle == "superseded"
    assert by_id[old].superseded_by == replacement
    assert by_id[retired].lifecycle == "retired"
    assert by_id[retired].superseded_by is None


def test_every_record_id_prefix_is_recognized_as_an_exact_search_term() -> None:
    """Catches current or compatibility IDs falling through to fuzzy text search."""
    assert retrieval.looks_like_identifier("rec-0123456789abcdef") is True
    for prefix in (
        "ctx",
        "prb",
        "hyp",
        "pro",
        "res",
        "ver",
        "fac",
        "ins",
        "dec",
        "rpt",
        "note",
    ):
        assert retrieval.looks_like_identifier(f"{prefix}-0a1bz") is True
        assert retrieval.looks_like_identifier(f"{prefix}-0a1bz9") is True
        assert retrieval.looks_like_identifier(f"{prefix}-0123456789abcdef") is True
    assert retrieval.looks_like_identifier("other-0a1bz") is False
    assert retrieval.looks_like_identifier("rec-0a1bz") is False
