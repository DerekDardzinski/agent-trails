"""Tests for project discovery through current framing."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import Any

import pytest

from tests.fakes import FakeEngine, basis
from trails import engine as engine_seam, vector
from trails.models import ProjectFramingDraft, RecordDraft
from trails.ops import context
from trails.ops.search import search
from trails.store import Store


class _Engine(FakeEngine):
    def __init__(self, near: Sequence[str]) -> None:
        super().__init__()
        self.near = tuple(near)

    def embed(self, text: str) -> Sequence[float]:
        self.calls.append(text)
        return basis(1) if any(word in text for word in self.near) else basis(2)


def _search(store: Store, **arguments: Any) -> Any:
    async def go() -> Any:
        return await context.bind(search, lambda: store.db_path)(**arguments)

    return asyncio.run(go())


def _project(store: Store, name: str, title: str | None = None, body: str = "") -> Any:
    framing = ProjectFramingDraft(title=title, body=body) if title is not None else None
    return store.write_project(name, "host", [], [], framing=framing).project


def test_project_search_returns_three_of_four_and_reports_total(store: Store) -> None:
    """Catches project hits being paged with records or hiding the number beyond the fixed cap."""
    for index in range(4):
        _project(store, f"Latency {index}", f"Latency project {index}", "Reduce device latency.")

    result = _search(store, query="latency")
    text = result.render_text()

    assert len(result.project_hits) == 3
    assert result.project_total == 4
    assert "1 more project matches; prime() starts the project index" in text
    assert text.index("project matches:") < text.index("Next:")


def test_search_exactly_finds_a_project_without_framing_by_id_or_name(store: Store) -> None:
    project = _project(store, "No framing")

    by_id = _search(store, query=project.id)
    by_name = _search(store, query="No framing")

    assert [item.id for item in by_id.project_hits] == [project.id]
    assert [item.id for item in by_name.project_hits] == [project.id]
    assert by_id.project_hits[0].framing_version is None


@pytest.mark.parametrize(
    "arguments",
    [
        {"entry_type": "note"},
        {"work_status": "closed"},
        {"tags": ["x"]},
        {"links": "cites:rec-0000000000000000"},
        {"since": "2026-01-01"},
        {"until": "2026-12-31"},
        {"projects": ["Scope"]},
        {"unscoped": True},
    ],
)
def test_every_record_only_filter_suppresses_project_hits(
    store: Store, arguments: dict[str, Any]
) -> None:
    _project(store, "Scope", "Discovery marker", "Discovery marker body.")

    result = _search(store, query="discovery", **arguments)

    assert result.project_hits == []
    assert result.project_total == 0


def test_project_semantic_search_uses_current_framing_and_keeps_old_vectors(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = _Engine(("zebra", "striped"))
    monkeypatch.setattr(engine_seam, "build_engine", lambda: engine)
    first = store.write_project(
        "Wildlife",
        "host",
        [],
        [],
        framing=ProjectFramingDraft(title="Zebra study", body="Observe zebra movement."),
    )
    assert first.framing is not None
    vector.embed_project_framings(store, engine, [(first.project, first.framing)])
    second = store.write_project(
        "Wildlife",
        "host",
        [],
        [],
        framing=ProjectFramingDraft(title="Bird study", body="Observe birds."),
        base_version=1,
        reason="Changed scope.",
    )
    assert second.framing is not None
    vector.embed_project_framings(store, engine, [(second.project, second.framing)])

    result = _search(store, query="striped animals")
    old_keyword = _search(store, query="zebra")

    assert result.project_hits == []
    assert old_keyword.project_hits == []
    assert store.query(
        "SELECT version FROM project_framing_embedding WHERE project_id = ? ORDER BY version",
        [first.project.id],
    ) == [{"version": 1}, {"version": 2}]


def test_combined_ten_record_and_three_project_full_chunks_stays_bounded(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Measures the approved largest default structured page with complete selected chunks."""
    engine = _Engine(("semantic-request", "context"))
    monkeypatch.setattr(engine_seam, "build_engine", lambda: engine)
    for index in range(10):
        body = " ".join([f"context-record-{index}"] * 80)
        record = store.write_record(
            RecordDraft(entry_type="note", work_status="closed", title=f"Record {index}", body=body)
        ).record
        vector.embed_records(store, engine, [record.id])
    for index in range(4):
        outcome = store.write_project(
            f"Project {index}",
            "host",
            [],
            [],
            framing=ProjectFramingDraft(
                title=f"Project frame {index}",
                body=" ".join([f"context-project-{index}"] * 80),
            ),
        )
        assert outcome.framing is not None
        vector.embed_project_framings(store, engine, [(outcome.project, outcome.framing)])

    result = _search(store, query="semantic-request")
    payload = result.model_dump_json().encode()

    assert (len(result.hits), len(result.project_hits), result.project_total) == (10, 3, 4)
    assert all((item.excerpt or "").startswith("context-record") for item in result.hits)
    assert all((item.excerpt or "").startswith("context-project") for item in result.project_hits)
    assert len(payload) < 25_000


def test_semantic_project_total_counts_every_eligible_current_framing(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = _Engine(("semantic-count", "Eligible"))
    monkeypatch.setattr(engine_seam, "build_engine", lambda: engine)
    for index in range(11):
        outcome = store.write_project(
            f"Eligible {index}",
            "host",
            [],
            [],
            framing=ProjectFramingDraft(
                title=f"Candidate {index}", body="Eligible project purpose."
            ),
        )
        assert outcome.framing is not None
        vector.embed_project_framings(store, engine, [(outcome.project, outcome.framing)])

    result = _search(store, query="semantic-count")

    assert result.project_total == 11
    assert len(result.project_hits) == 3
