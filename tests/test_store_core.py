"""Tests for the store's write path and record assembly."""

from __future__ import annotations

import subprocess
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime
from threading import Event
from typing import Any

import pytest

from trails import dolt, store as store_module
from trails.errors import DoltError, RecordNotFoundError, ValidationError
from trails.models import ArtifactRef, CodeCapture, Link, ProjectPath, RecordDraft
from trails.store import Store

DIGEST = "sha256:" + "ab" * 32


def _write(store: Store, title: str = "a note", **fields: Any) -> str:
    fields.setdefault("entry_type", "note")
    fields.setdefault("work_status", "closed")
    return store.write_record(RecordDraft(title=title, **fields)).record.id


def test_a_written_record_reads_back_equal_to_what_the_write_returned(store: Store) -> None:
    """Catches the write path and the read path disagreeing about a record's shape.

    Every field is populated, and the order-sensitive ones are given out of order: tags in
    mixed case with a duplicate, code captures in reverse repo order. A caller comparing the
    returned record with a later read would otherwise see a difference that is not stored.
    """
    target = _write(store, "the source")
    outcome = store.write_record(
        RecordDraft(
            entry_type="result",
            work_status="closed",
            title="  Lift estimate  ",
            body="line one\nline two",
            tags=[" cuped "],
            project="mint-en-ca",
            value={"did_estimate": -0.0041, "ci95": [-0.0093, 0.0011]},
            links=[Link(relation="cites", target_id=target)],
            artifacts=[
                ArtifactRef(location="s3://bucket/key", digest=None, notes="raw"),
                ArtifactRef(location="/tmp/x.csv", digest=DIGEST),
            ],
            code=[
                CodeCapture(repo_path="/r/b", head_sha=None, captured_sha="c2" * 20, dirty=True),
                CodeCapture(
                    repo_path="/r/a", head_sha="a1" * 20, captured_sha="b1" * 20, dirty=False
                ),
            ],
        )
    )

    read = store.get_record(outcome.record.id)

    assert read == outcome.record
    assert read.title == "Lift estimate"
    assert read.tags == ["cuped"]
    assert (read.entry_type, read.work_status) == ("result", "closed")
    assert [capture.repo_path for capture in read.code] == ["/r/a", "/r/b"]
    assert read.value == {"did_estimate": -0.0041, "ci95": [-0.0093, 0.0011]}
    assert read.links == [Link(relation="cites", target_id=target)]
    assert (read.superseded_by, read.retired, read.last_verified) == (None, False, None)


def test_supersedes_needs_a_reason_and_then_marks_the_old_record(store: Store) -> None:
    """Catches a revision recorded with no stated change, and superseded_by not following it.

    Without the reason a later reader has two long records and no word on which difference
    was intended.
    """
    old = _write(store, "procedure v1")

    with pytest.raises(ValidationError, match="needs a reason"):
        store.write_record(
            RecordDraft(
                entry_type="note", work_status="closed", title="procedure v2", supersedes=old
            )
        )
    assert store.query("SELECT COUNT(*) AS n FROM record") == [{"n": 1}]

    new = store.write_record(
        RecordDraft(
            entry_type="note",
            work_status="closed",
            title="procedure v2",
            supersedes=old,
            reason="step 3 uses the red role",
        )
    ).record

    assert new.links == [Link(relation="supersedes", target_id=old)]
    assert new.reason == "step 3 uses the red role"
    assert store.get_record(old).superseded_by == new.id


def test_a_pre_minted_id_is_used_and_cannot_be_reused(store: Store) -> None:
    """Catches the id a caller named git refs after not being the id the record got, and an
    overwrite through a reused id."""
    minted = store.new_record_id("note")

    written = store.write_record(
        RecordDraft(entry_type="note", work_status="closed", id=minted, title="first")
    ).record
    with pytest.raises(ValidationError, match="already used"):
        store.write_record(
            RecordDraft(entry_type="note", work_status="closed", id=minted, title="second")
        )

    assert written.id == minted
    assert store.get_record(minted).title == "first"


def test_a_minted_id_uses_six_characters_after_ten_five_character_collisions(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches plain-write collision recovery changing type or never extending the suffix."""
    taken = _write(store, "first", entry_type="result")
    lengths: list[int] = []

    def mint(entry_type: object, length: int) -> str:
        assert entry_type == "result"
        lengths.append(length)
        return taken if length == 5 else "res-abcdef"

    monkeypatch.setattr(store_module, "_mint_id", mint)
    second = store.write_record(
        RecordDraft(entry_type="result", work_status="closed", title="second")
    ).record

    assert lengths == [5] * 10 + [6]
    assert second.id == "res-abcdef"
    assert store.get_record(taken).title == "first"
    assert store.get_record(second.id).title == "second"


def test_equal_timestamp_collision_selects_an_unused_id_before_reporting_success(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches an older equal-timestamp row being mistaken for this call's landed write."""
    moment = datetime(2026, 10, 6, 23, 30, 0, 123456, tzinfo=UTC)
    candidates = iter(["res-abcde", "res-abcde", "res-fghij"])

    def mint(entry_type: object, length: int) -> str:
        assert (entry_type, length) == ("result", 5)
        return next(candidates)

    monkeypatch.setattr(store_module, "_now", lambda: moment)
    monkeypatch.setattr(store_module, "_mint_id", mint)

    first = store.write_record(
        RecordDraft(entry_type="result", work_status="closed", title="first")
    ).record
    second = store.write_record(
        RecordDraft(entry_type="result", work_status="closed", title="second")
    ).record

    assert (first.id, second.id) == ("res-abcde", "res-fghij")
    assert store.query("SELECT id, title FROM record ORDER BY id") == [
        {"id": "res-abcde", "title": "first"},
        {"id": "res-fghij", "title": "second"},
    ]


def test_plain_write_fails_after_twenty_collisions_without_overwriting(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches the primary-key collision path looping forever or overwriting a record."""
    taken = _write(store, "first", entry_type="decision")
    lengths: list[int] = []

    def mint(entry_type: object, length: int) -> str:
        assert entry_type == "decision"
        lengths.append(length)
        return taken

    monkeypatch.setattr(store_module, "_mint_id", mint)

    with pytest.raises(DoltError, match="after 20 candidates"):
        store.write_record(RecordDraft(entry_type="decision", work_status="closed", title="second"))
    assert lengths == [5] * 10 + [6] * 10
    assert store.query("SELECT id, title FROM record") == [{"id": taken, "title": "first"}]


def test_pre_minted_id_uses_six_characters_after_ten_collisions(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches capture pre-mint using a retry policy different from plain writes."""
    taken = _write(store, "first", entry_type="hypothesis")
    lengths: list[int] = []

    def mint(entry_type: object, length: int) -> str:
        assert entry_type == "hypothesis"
        lengths.append(length)
        return taken if length == 5 else "hyp-abcdef"

    monkeypatch.setattr(store_module, "_mint_id", mint)

    assert store.new_record_id("hypothesis") == "hyp-abcdef"
    assert lengths == [5] * 10 + [6]


def test_pre_minted_id_fails_after_twenty_collisions_without_overwriting(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches an unbounded loop or a collision overwriting the existing record."""
    taken = _write(store, "first", entry_type="fact")
    lengths: list[int] = []

    def mint(entry_type: object, length: int) -> str:
        assert entry_type == "fact"
        lengths.append(length)
        return taken

    monkeypatch.setattr(store_module, "_mint_id", mint)

    with pytest.raises(DoltError, match="after 20 candidates"):
        store.new_record_id("fact")
    assert lengths == [5] * 10 + [6] * 10
    assert store.get_record(taken).title == "first"


def test_malformed_drafts_are_refused_and_write_nothing(store: Store) -> None:
    """The refusal boundary: each malformed input raises ValidationError with no row written.

    The last six are inputs the database itself refuses, which must arrive as ValidationError
    rather than a raw DoltError: two pairs of tags equal under the case- and accent-insensitive
    collation (one pair Python accent-stripping would miss, `o`/`ø`), and values wider than
    their VARCHAR or TEXT column.
    """
    existing = _write(store)
    malformed = [
        RecordDraft(entry_type="note", work_status="closed", title="   "),
        RecordDraft(entry_type="note", work_status="closed", title="t", tags=["ok", "  "]),
        RecordDraft(entry_type="note", work_status="closed", title="t", id="rec-NOTHEX"),
        RecordDraft(
            entry_type="note",
            work_status="closed",
            title="t",
            links=[Link(relation="cites", target_id="rec-0000000000000000")],
        ),
        RecordDraft(
            entry_type="note",
            work_status="closed",
            title="t",
            supersedes="rec-0000000000000000",
            reason="r",
        ),
        RecordDraft(
            entry_type="note",
            work_status="closed",
            title="t",
            links=[Link(relation="supersedes", target_id=existing)],
        ),
        RecordDraft(
            entry_type="note",
            work_status="closed",
            title="t",
            artifacts=[ArtifactRef(location="x", digest="md5:1")],
        ),
        RecordDraft(entry_type="note", work_status="closed", title="t", tags=["cafe", "café"]),
        RecordDraft(entry_type="note", work_status="closed", title="t", tags=["o", "ø"]),
        RecordDraft(entry_type="note", work_status="closed", title="x" * 1001),
        RecordDraft(entry_type="note", work_status="closed", title="t", tags=["y" * 201]),
        RecordDraft(
            entry_type="note",
            work_status="closed",
            title="t",
            artifacts=[ArtifactRef(location="x", digest=None, notes="n" * 70_000)],
        ),
        RecordDraft(
            entry_type="note",
            work_status="closed",
            title="t",
            supersedes=existing,
            reason="r" * 70_000,
        ),
    ]

    for draft in malformed:
        with pytest.raises(ValidationError):
            store.write_record(draft)

    assert store.query("SELECT COUNT(*) AS n FROM record") == [{"n": 1}]


def test_tags_the_collation_keeps_apart_are_both_stored(store: Store) -> None:
    """Catches the duplicate-tag check over-refusing. utf8mb4_0900_ai_ci keeps `ss` and `ß`
    apart, and `fi` and the ligature `ﬁ`, both of which a Python approximation got wrong."""
    first = _write(store, tags=["strasse", "straße"])
    second = _write(store, "b", tags=["fi", "ﬁ"])

    assert store.get_record(first).tags == ["strasse", "straße"]
    assert store.get_record(second).tags == ["fi", "ﬁ"]


def test_a_commit_that_landed_before_dolt_failed_is_reported_written_once(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches the collision retry writing the record a second time under a new id when the
    commit went through and the dolt process failed afterwards."""
    original = dolt.run_script

    def commits_then_fails(db_path: Any, statements: Any) -> None:
        original(db_path, statements)
        raise dolt.DoltError("exited non-zero after the commit")

    monkeypatch.setattr(dolt, "run_script", commits_then_fails)

    written = store.write_record(
        RecordDraft(entry_type="note", work_status="closed", title="once")
    ).record

    assert store.query("SELECT id, title FROM record") == [{"id": written.id, "title": "once"}]


def test_get_records_costs_the_same_dolt_processes_for_one_id_and_for_fifty(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches a per-record read, which turns one call into fifty process spawns."""
    ids = [_write(store, f"r{n}", tags=["t"]) for n in range(50)]
    original = subprocess.run
    spawned: list[list[str]] = []

    def counting(args: list[str], **kwargs: Any) -> Any:
        if args and args[0] == "dolt":
            spawned.append(args)
        return original(args, **kwargs)

    monkeypatch.setattr(dolt.subprocess, "run", counting)

    one = store.get_records(ids[:1])
    one_cost = len(spawned)
    spawned.clear()
    fifty = store.get_records(ids)

    assert len(one) == 1
    assert len(fifty) == 50
    assert len(spawned) == one_cost == 1


def test_get_record_raises_for_an_unknown_id_and_get_records_omits_it(store: Store) -> None:
    """Catches a missing record reading as an empty one."""
    known = _write(store)

    with pytest.raises(RecordNotFoundError):
        store.get_record("rec-0000000000000000")
    assert list(store.get_records([known, "rec-0000000000000000"])) == [known]


def test_add_link_refusals_and_idempotence(store: Store) -> None:
    """Catches a duplicate link failing on DOLT_COMMIT's nothing-to-commit, and a supersedes
    cycle that would make every lineage walk loop."""
    a, b = _write(store, "a"), _write(store, "b")

    with pytest.raises(ValidationError):
        store.add_link(a, "cites", a)
    with pytest.raises(RecordNotFoundError):
        store.add_link(a, "cites", "rec-0000000000000000")

    first = store.add_link(a, "builds_on", b)
    again = store.add_link(a, "builds_on", b)
    store.add_link(b, "supersedes", a)
    with pytest.raises(ValidationError, match="cycle"):
        store.add_link(a, "supersedes", b)

    assert first == again == Link(relation="builds_on", target_id=b)
    assert store.get_record(a).links == [Link(relation="builds_on", target_id=b)]
    assert store.get_record(a).superseded_by == b


def test_retire_and_stamp_verified_reach_the_record(store: Store) -> None:
    """Catches retired and last_verified not being assembled, and a repeat call failing."""
    record_id = _write(store, "procedure", entry_type="procedure")

    with pytest.raises(ValidationError):
        store.retire(record_id, "  ")
    store.retire(record_id, "the endpoint moved")
    store.retire(record_id, "said twice")
    store.stamp_verified(record_id, "green", date(2026, 9, 1))
    store.stamp_verified(record_id, "red", date(2026, 9, 12))
    store.stamp_verified(record_id, "red", date(2026, 9, 12))
    with pytest.raises(RecordNotFoundError):
        store.retire("rec-0000000000000000", "gone")

    record = store.get_record(record_id)
    assert record.retired is True
    assert record.last_verified == (date(2026, 9, 12), "red")
    assert store.query("SELECT reason FROM retirement") == [{"reason": "the endpoint moved"}]


def test_tag_alias_and_project_rows(store: Store) -> None:
    """Catches an identical alias failing the second time, a conflicting alias accepted, and
    a duplicate project accepted."""
    store.write_tag_alias("Proc", "procedure")
    store.write_tag_alias("proc", "procedure")
    with pytest.raises(ValidationError):
        store.write_tag_alias("proc", "process")
    with pytest.raises(ValidationError):
        store.write_tag_alias("same", "same")

    assert store.alias_map() == {"proc": "procedure"}


def test_project_paths_are_added_updated_and_removed_in_place(store: Store) -> None:
    """labbook-79y.10 section 5. Catches a re-registration duplicating a path instead of
    updating it, a removal ignored, a no-op call failing on Dolt's "nothing to commit", and a
    path both added and removed accepted."""
    code = ProjectPath(path="/w/code", label="analysis code", host="h")
    data = ProjectPath(path="s3://bucket/mint/", access_instructions="aws --profile ro")

    first, created = store.write_project("mint", "h", [code, data], [])
    relabelled = code.model_copy(update={"label": "scripts"})
    second, again = store.write_project("mint", "h", [relabelled], ["s3://bucket/mint/"])
    unchanged, _ = store.write_project("mint", "h", [relabelled], ["/not/registered"])
    with pytest.raises(ValidationError, match="both in paths and in remove_paths"):
        store.write_project("mint", "h", [code], ["/w/code"])

    assert (created, again) == (True, False)
    assert first.paths == [code, data]
    assert second.paths == [relabelled]
    assert unchanged == second
    assert store.get_project("mint") == second
    assert store.get_project("nope") is None
    assert store.list_projects() == [second]


def test_project_paths_differing_only_in_case_are_two_paths(store: Store) -> None:
    """labbook-79y.10 review round 1. Catches the path compared case-insensitively by the
    primary key while write_project compares it exactly, which surfaced as a raw DoltError
    and made a removal of the other spelling silently do nothing."""
    upper, lower = ProjectPath(path="s3://bucket/Data"), ProjectPath(path="s3://bucket/data")

    store.write_project("p", "h", [upper], [])
    both, _ = store.write_project("p", "h", [lower, ProjectPath(path="/tmp/A")], [])
    removed, _ = store.write_project("p", "h", [], ["s3://bucket/data"])

    assert {item.path for item in both.paths} == {"s3://bucket/Data", "s3://bucket/data", "/tmp/A"}
    assert {item.path for item in removed.paths} == {"s3://bucket/Data", "/tmp/A"}


def test_a_possible_secret_is_reported_without_its_text_and_does_not_stop_the_write(
    store: Store,
) -> None:
    """Catches a secret blocking the write, a finding missing its location, and the key text
    leaking into the finding."""
    outcome = store.write_record(
        RecordDraft(
            entry_type="note",
            work_status="closed",
            title="creds",
            body="first line\naws_key = AKIAIOSFODNN7EXAMPLE\n",
        )
    )

    assert [f.model_dump() for f in outcome.secret_findings] == [
        {"field": "body", "line": 2, "detector": "AWSKeyDetector"}
    ]
    assert "AKIAIOSFODNN7EXAMPLE" not in outcome.model_dump_json(exclude={"record"})
    assert store.get_record(outcome.record.id).body.startswith("first line")


def test_a_sha256_digest_is_not_reported_as_a_secret(store: Store) -> None:
    """Catches the high-entropy detectors being left on, which flag every digest."""
    outcome = store.write_record(
        RecordDraft(
            entry_type="note",
            work_status="closed",
            title="digest",
            body="9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08",
            artifacts=[ArtifactRef(location="/tmp/x", digest=DIGEST)],
        )
    )

    assert outcome.secret_findings == []


def test_every_entry_type_mints_its_exact_prefix(store: Store) -> None:
    """Catches a vocabulary entry falling back to a generic or another type's prefix."""
    from trails.vocab import ENTRY_TYPE_PREFIXES

    for entry_type, prefix in ENTRY_TYPE_PREFIXES.items():
        record_id = store.new_record_id(entry_type)
        assert record_id.startswith(f"{prefix}-")
        suffix = record_id.removeprefix(f"{prefix}-")
        assert len(suffix) == 5
        assert set(suffix) <= set("0123456789abcdefghijklmnopqrstuvwxyz")


def test_entry_type_is_not_a_tag_and_a_pre_minted_id_must_agree(store: Store) -> None:
    """Catches two sources of type truth or a capture ref whose prefix contradicts its record."""
    with pytest.raises(ValidationError, match="entry type, not a descriptive tag"):
        store.write_record(
            RecordDraft(
                entry_type="result",
                work_status="closed",
                title="Measured",
                tags=["result"],
            )
        )
    with pytest.raises(ValidationError, match="does not match entry_type"):
        store.write_record(
            RecordDraft(
                id="note-abcde",
                entry_type="result",
                work_status="closed",
                title="Measured",
            )
        )


def test_work_status_history_is_append_only_and_a_no_op_needs_no_reason(store: Store) -> None:
    """Catches status mutation, lost ordering, invented initial reasons, or no-op event noise."""
    record_id = _write(store, "Testable claim", entry_type="hypothesis", work_status="open")

    initial = store.get_record(record_id)
    assert initial.work_status == "open"
    assert [
        (event.sequence, event.work_status, event.reason) for event in initial.work_status_history
    ] == [(0, "open", None)]

    closed, changed = store.set_work_status(record_id, "closed", "planned analysis completed")
    repeated, repeated_changed = store.set_work_status(record_id, "closed", "")
    reopened, reopened_changed = store.set_work_status(
        record_id, "open", "additional validation requested"
    )

    assert (closed.sequence, changed) == (1, True)
    assert (repeated.sequence, repeated_changed) == (1, False)
    assert (reopened.sequence, reopened_changed) == (2, True)
    assert [
        (event.sequence, event.work_status, event.reason)
        for event in store.get_record(record_id).work_status_history
    ] == [
        (0, "open", None),
        (1, "closed", "planned analysis completed"),
        (2, "open", "additional validation requested"),
    ]

    with pytest.raises(ValidationError, match="needs a reason"):
        store.set_work_status(record_id, "closed", "  ")


def test_concurrent_work_status_changes_preserve_both_events(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Catches two agents choosing the same sequence and losing one status transition."""
    record_id = _write(store, "Tracked work", work_status="closed")
    first_entered = Event()
    release_first = Event()
    real_execute = Store._execute_script
    blocked = False

    def blocking_execute(self: Store, statements: list[str]) -> None:
        nonlocal blocked
        if not blocked and any("work status" in statement for statement in statements):
            blocked = True
            first_entered.set()
            assert release_first.wait(5)
        real_execute(self, statements)

    monkeypatch.setattr(Store, "_execute_script", blocking_execute)
    with ThreadPoolExecutor(max_workers=2) as pool:
        opened = pool.submit(store.set_work_status, record_id, "open", "more work requested")
        assert first_entered.wait(5)
        closed = pool.submit(store.set_work_status, record_id, "closed", "work completed")
        assert not closed.done()
        release_first.set()
        assert opened.result(timeout=10)[1] is True
        assert closed.result(timeout=10)[1] is True

    history = store.get_record(record_id).work_status_history
    assert [(event.sequence, event.work_status) for event in history] == [
        (0, "closed"),
        (1, "open"),
        (2, "closed"),
    ]
