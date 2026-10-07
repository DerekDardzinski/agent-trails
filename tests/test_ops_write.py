"""Tests for the publish, link, retire and version operations, through the binders."""

from __future__ import annotations

import asyncio
import fcntl
import json
from pathlib import Path
from typing import Any

import pytest
from fastmcp import Client

from tests.fakes import FakeEngine, basis
from trails import dolt, engine as engine_seam
from trails.errors import ValidationError
from trails.models import RecordDraft
from trails.ops import context
from trails.ops._write_types import PublishResult
from trails.ops.link import link
from trails.ops.publish import publish
from trails.ops.retire import retire
from trails.ops.set_work_status import set_work_status
from trails.record_file import to_markdown
from trails.store import Store
from trails.surfaces import cli as cli_surface, mcp as mcp_surface

AWS_KEY = "AKIAIOSFODNN7EXAMPLE"


def _run(store: Store, operation: Any, **arguments: Any) -> Any:
    if operation is publish and "from_file" not in arguments:
        arguments.setdefault("entry_type", "note")
        arguments.setdefault("work_status", "closed")

    async def go() -> Any:
        return await context.bind(operation, lambda: store.db_path)(**arguments)

    return asyncio.run(go())


def _mcp(store: Store, tool: str, arguments: dict[str, Any]) -> Any:
    server = mcp_surface.build_mcp_server(store.db_path)

    async def go() -> Any:
        async with Client(server) as client:
            return await client.call_tool(tool, arguments, raise_on_error=False)

    return asyncio.run(go())


def _cli(capsys: pytest.CaptureFixture[str], argv: list[str]) -> tuple[int, str, str]:
    code = 0
    try:
        cli_surface.build_cli()(argv, exit_on_error=False)
    except SystemExit as exc:
        code = int(exc.code or 0)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_a_possible_secret_is_stored_and_reported_without_its_text(store: Store) -> None:
    """Criterion 4. Catches the secret blocking the write, its location missing from the
    warning, and the key text reaching the warning or the rendered output."""
    result = _run(
        store, publish, title="creds", entry_type="note", body=f"setup\nkey = {AWS_KEY}\n"
    )

    assert result.warnings == ["possible secret: AWSKeyDetector in body line 2"]
    assert AWS_KEY not in result.render_text()
    assert AWS_KEY not in result.model_dump_json()
    assert store.get_record(result.record_id).body == f"setup\nkey = {AWS_KEY}\n"


def test_a_sha256_body_produces_no_secret_warning(store: Store) -> None:
    """Criterion 4, second half. Catches the high-entropy detectors flagging digests."""
    digest = "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08"

    assert _run(store, publish, title="digest", body=digest).warnings == []


def test_publish_supersedes_needs_a_reason_and_then_marks_the_old_record(store: Store) -> None:
    """Criterion 5, through the operation."""
    old = _run(store, publish, title="v1").record_id

    with pytest.raises(ValidationError, match="needs a reason"):
        _run(store, publish, title="v2", supersedes=old)
    new = _run(store, publish, title="v2", supersedes=old, reason="fixed step 2")

    assert new.superseded_id == old
    assert new.notes == [
        "superseding records inherit no links, artifacts, captured code or verification "
        "stamps; this record contains only what this call supplied"
    ]
    assert new.warnings == []
    assert store.get_record(old).superseded_by == new.record_id


def test_publish_from_file_supersedes_the_exported_record_and_refuses_extra_arguments(
    store: Store, tmp_path: Path
) -> None:
    """Catches from_file silently merging with inline arguments, and a revised file
    publishing as an unrelated record."""
    original = store.write_record(
        RecordDraft(entry_type="note", work_status="closed", title="procedure", body="step 1")
    ).record
    path = tmp_path / "procedure.md"
    path.write_text(
        to_markdown(original).replace("+++\n", '+++\nreason = "added step 2"\n', 1) + "\nstep 2"
    )

    with pytest.raises(ValidationError, match="from_file supplies title"):
        _run(store, publish, from_file=str(path), title="other")
    revised = _run(store, publish, from_file=str(path))

    assert revised.superseded_id == original.id
    assert store.get_record(revised.record_id).body == "step 1\nstep 2"


def test_link_refuses_supersedes_and_names_the_call_to_make(store: Store) -> None:
    """Catches link writing a supersession with no reason and no new record."""
    a = _run(store, publish, title="a").record_id
    b = _run(store, publish, title="b").record_id

    with pytest.raises(ValidationError, match=r"publish\(supersedes=\.\.\., reason=\.\.\.\)"):
        _run(store, link, source_id=a, relation="supersedes", target_id=b)
    linked = _run(store, link, source_id=a, relation="builds_on", target_id=b)
    retired = _run(store, retire, record_id=b, reason="obsolete")
    again = _run(store, retire, record_id=b, reason="a different reason")

    assert linked.render_text() == f'linked {a} builds_on {b}\nNext: show("{a}")'
    assert retired.render_text() == f'retired {b}: obsolete\nNext: show("{b}")'
    assert again.render_text() == (
        f'already retired {b}, nothing changed: obsolete\nNext: show("{b}")'
    )
    assert store.get_record(b).retired is True


def test_publish_embeds_when_the_engine_is_available_and_warns_when_it_fails(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches publish not reaching the engine seam, and an embedding failure hiding a
    write that already happened."""
    monkeypatch.setattr(engine_seam, "build_engine", lambda: FakeEngine({"t": basis(0)}))
    embedded = _run(store, publish, title="t")

    broken = FakeEngine(failure=RuntimeError("model file truncated"))
    monkeypatch.setattr(engine_seam, "build_engine", lambda: broken)
    failed = _run(store, publish, title="u")

    assert embedded.embedded is True
    assert store.query(
        "SELECT chunk_index FROM record_chunk_embedding WHERE record_id = ?",
        [embedded.record_id],
    ) == [{"chunk_index": 0}]
    assert failed.embedded is False
    assert failed.warnings == [
        "the record was written but not embedded for similarity search: model file truncated"
    ]
    assert store.get_record(failed.record_id).title == "u"


def test_mcp_publish_returns_the_rendered_text_and_the_model(store: Store) -> None:
    """Criterion 9. Catches the MCP surface returning only the JSON model, which an agent
    then reads instead of the text, or text that disagrees with the model."""
    result = _mcp(
        store,
        "publish",
        {
            "entry_type": "note",
            "work_status": "closed",
            "title": "via mcp",
            "tags": [],
            "body": "x",
        },
    )

    model = PublishResult.model_validate(result.structured_content)
    assert [block.text for block in result.content] == [model.render_text()]
    assert result.structured_content == model.model_dump(mode="json")
    assert model.render_text() == (
        f'published {model.record_id} · not embedded\nNext: show("{model.record_id}")'
    )


def test_mcp_returns_a_refusal_as_data(store: Store) -> None:
    """Catches a ValidationError reaching the agent as a protocol failure."""
    result = _mcp(
        store,
        "publish",
        {"entry_type": "note", "work_status": "closed", "title": ""},
    )

    assert result.structured_content == {"error": "title is empty: a record needs a one-line title"}


def test_cli_prints_text_by_default_and_json_with_the_flag(
    store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    """Catches the CLI printing only JSON, or --json printing something MCP would not."""
    db = ["--db", str(store.db_path)]
    record = ["--entry-type", "note", "--work-status", "closed"]

    code, text, _ = _cli(capsys, ["publish", *record, "--title", "from cli", *db])
    record_id = text.split()[1]
    json_code, as_json, _ = _cli(capsys, ["publish", *record, "--title", "again", "--json", *db])
    refused, _, err = _cli(capsys, ["link", record_id, "supersedes", record_id, *db])

    assert (code, json_code, refused) == (0, 0, 1)
    assert text == f'published {record_id} · not embedded\nNext: show("{record_id}")\n'
    assert sorted(json.loads(as_json)) == [
        "captures",
        "embedded",
        "notes",
        "record_id",
        "superseded_id",
        "warnings",
    ]
    assert err.startswith("error: link does not write supersedes")


def test_retire_reads_and_writes_under_one_exclusive_lock(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """labbook-79y.6 item 4. Catches the already-retired read taken outside the write lock,
    which lets two concurrent retires both report already_retired=False."""
    record = store.write_record(
        RecordDraft(work_status="closed", title="Old", entry_type="fact")
    ).record
    lock_path = store.db_path / dolt.WRITE_LOCK_NAME
    real_read = dolt.read_rows
    held: list[bool] = []

    def checking(db_path: Path, statement: str) -> Any:
        if "FROM retirement" in statement:
            with lock_path.open("a") as other:
                try:
                    fcntl.flock(other, fcntl.LOCK_SH | fcntl.LOCK_NB)
                except BlockingIOError:
                    held.append(True)
                else:
                    fcntl.flock(other, fcntl.LOCK_UN)
                    held.append(False)
        return real_read(db_path, statement)

    monkeypatch.setattr(dolt, "read_rows", checking)
    _run(store, retire, record_id=record.id, reason="obsolete")

    assert held and all(held)


def test_publish_with_a_missing_superseded_id_keeps_the_validation_error_contract(
    store: Store,
) -> None:
    """Catches the warning lookup leaking RecordNotFoundError or moving ahead of the store's
    malformed-input contract."""
    missing = "rec-0000000000000000"

    with pytest.raises(ValidationError, match=f"no existing record has id {missing}"):
        _run(store, publish, title="replacement", supersedes=missing, reason="corrected")


def test_a_result_that_only_cites_a_hypothesis_gets_the_builds_on_call(store: Store) -> None:
    """Catches a scientifically relevant link leaving prime to report the hypothesis untested
    without telling the agent how to record that the result tested it."""
    hypothesis = _run(store, publish, title="Hypothesis", entry_type="hypothesis").record_id

    cited = _run(
        store,
        publish,
        title="Result",
        entry_type="result",
        links=[{"relation": "cites", "target_id": hypothesis}],
    )
    built = _run(
        store,
        publish,
        title="Result with evidence link",
        entry_type="result",
        links=[
            {"relation": "cites", "target_id": hypothesis},
            {"relation": "builds_on", "target_id": hypothesis},
        ],
    )

    assert cited.notes == [
        f"this result cites {hypothesis} (hypothesis). prime counts only builds_on when it "
        "decides whether a hypothesis has a result. If this result tests that hypothesis, "
        f'run: link("{cited.record_id}", "builds_on", "{hypothesis}")'
    ]
    assert built.notes == []


def test_linking_a_result_to_cite_a_hypothesis_gets_the_same_note(store: Store) -> None:
    """Catches guidance depending on whether the relation was supplied during publish or
    added later, and duplicate link calls duplicating the note."""
    hypothesis = _run(store, publish, title="Hypothesis", entry_type="hypothesis").record_id
    result = _run(store, publish, title="Result", entry_type="result").record_id

    first = _run(store, link, source_id=result, relation="cites", target_id=hypothesis)
    again = _run(store, link, source_id=result, relation="cites", target_id=hypothesis)
    _run(store, link, source_id=result, relation="builds_on", target_id=hypothesis)
    after_evidence = _run(
        store, link, source_id=result, relation="contradicts", target_id=hypothesis
    )

    expected = [
        f"this result cites {hypothesis} (hypothesis). prime counts only builds_on when it "
        "decides whether a hypothesis has a result. If this result tests that hypothesis, "
        f'run: link("{result}", "builds_on", "{hypothesis}")'
    ]
    assert first.notes == again.notes == expected
    assert after_evidence.notes == []


def test_set_work_status_reports_changes_and_no_change(store: Store) -> None:
    """Catches an agent being told a transition was stored when the state was already current."""
    record_id = _run(
        store,
        publish,
        entry_type="hypothesis",
        work_status="open",
        title="Testable claim",
    ).record_id

    changed = _run(
        store,
        set_work_status,
        record_id=record_id,
        work_status="closed",
        reason="planned analysis completed",
    )
    unchanged = _run(
        store,
        set_work_status,
        record_id=record_id,
        work_status="closed",
        reason="",
    )

    assert changed.changed is True
    assert changed.render_text() == (
        f"work status for {record_id} set to closed: planned analysis completed\n"
        f'Next: show("{record_id}")'
    )
    assert unchanged.changed is False
    assert unchanged.render_text() == (
        f'work status for {record_id} already closed; nothing changed\nNext: show("{record_id}")'
    )
