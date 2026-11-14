"""Tests for the Markdown record file."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from trails.errors import ValidationError
from trails.models import ArtifactRef, Link, ProjectRef, Record, RecordDraft, WorkStatusEvent
from trails.record_file import from_markdown, to_markdown
from trails.store import Store


@pytest.fixture
def store(record_store: Store) -> Store:
    """This module tests the old record store, removed with it in the next commit."""
    return record_store


BODY = 'Step 1\n+++\nquote " and backslash \\ and tab\t and é 😀 and \x7f\n\n## Next\n'


def _record(**overrides: object) -> Record:
    fields: dict[str, object] = {
        "id": "pro-0123456789abcdef",
        "entry_type": "procedure",
        "work_status": "closed",
        "work_status_history": [
            WorkStatusEvent(
                sequence=0,
                work_status="closed",
                changed_at=datetime(2026, 10, 1, tzinfo=UTC),
                author="ddardzin",
                reason=None,
            )
        ],
        "title": 'Sync "green" → red',
        "body": BODY,
        "tags": ["sync"],
        "projects": [ProjectRef(id="prj-a7k2m", name="mint-en-ca")],
        "author": "ddardzin",
        "created_at": datetime(2026, 10, 1, tzinfo=UTC),
        "value": {"did_estimate": -0.0041, "ci95": [-0.0093, 0.0011], "note": None, "n": 3},
        "reason": None,
        "artifacts": [
            ArtifactRef(location="s3://bucket/key", digest=None, notes="raw"),
            ArtifactRef(location="/tmp/x.csv", digest="sha256:" + "ab" * 32),
        ],
        "code": [],
        "links": [
            Link(relation="builds_on", target_id="rec-1111111111111111"),
            Link(relation="cites", target_id="rec-2222222222222222"),
        ],
        "superseded_by": None,
        "retired": False,
        "last_verified": None,
    }
    fields.update(overrides)
    return Record.model_validate(fields)


def test_a_record_round_trips_through_its_file() -> None:
    """Criterion 6. Catches escaping loss, a body line reading `+++` being taken for the
    fence, a JSON null in value that TOML cannot hold, and a trailing newline dropped."""
    record = _record()

    text = to_markdown(record)
    assert 'projects = ["prj-a7k2m"] # mint-en-ca' in text
    parsed = from_markdown(text)
    draft = parsed.draft

    assert (
        draft.entry_type,
        draft.work_status,
        draft.title,
        draft.body,
        draft.tags,
        draft.projects,
        draft.value,
    ) == (
        record.entry_type,
        record.work_status,
        record.title,
        record.body,
        record.tags,
        [project.id for project in record.projects],
        record.value,
    )
    assert draft.links == record.links
    assert draft.artifacts == record.artifacts
    assert parsed.id == "pro-0123456789abcdef"


def test_an_exported_id_becomes_the_record_the_file_supersedes() -> None:
    """Catches a revised file publishing as an unrelated record rather than a revision."""
    text = to_markdown(_record(links=[], artifacts=[], value=None, projects=[]))
    edited = text.replace("+++\n", '+++\nreason = "step 3 changed"\n', 1)

    parsed = from_markdown(edited)

    assert (parsed.id, parsed.reason) == ("pro-0123456789abcdef", "step 3 changed")
    assert (parsed.draft.supersedes, parsed.draft.reason) == (
        "pro-0123456789abcdef",
        "step 3 changed",
    )
    assert (parsed.draft.projects, parsed.draft.value, parsed.draft.links) == ([], None, [])


def test_a_hand_written_file_without_an_id_is_a_new_record_and_value_may_be_a_table() -> None:
    """Catches a file an agent wrote from scratch being refused for lacking export-only keys."""
    parsed = from_markdown(
        '+++\nentry_type = "note"\nwork_status = "open"\ntitle = "fresh"\n'
        "value = {n = 3}\n+++\nbody text"
    )

    assert parsed.id is None
    assert (parsed.draft.entry_type, parsed.draft.work_status) == ("note", "open")
    assert parsed.draft.supersedes is None
    assert (parsed.draft.title, parsed.draft.body, parsed.draft.value) == (
        "fresh",
        "body text",
        {"n": 3},
    )


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ('title = "x"\n+++\nbody', "starts with"),
        ('+++\ntitle = "x"\nbody', "not closed"),
        ('+++\ntitle = "x"\nsubject = "y"\n+++\n', "unknown front-matter key subject"),
        ('+++\ntitle = "x"\ntags = = 1\n+++\n', "line 2"),
        (
            '+++\nentry_type = "note"\nwork_status = "closed"\ntitle = 3\n+++\n',
            "title",
        ),
        (
            '+++\nentry_type = "note"\nwork_status = "closed"\ntitle = "x"\n'
            'value = "{not json"\n+++\n',
            "value is not JSON",
        ),
        (
            '+++\nentry_type = "note"\nwork_status = "closed"\ntitle = "x"\n'
            '[[artifacts]]\nlocation = "/tmp/x"\ndigset = "sha256:ab"\n'
            'notes = ""\n+++\n',
            r"unknown key digset in artifacts\[0\]",
        ),
        (
            '+++\nentry_type = "note"\nwork_status = "closed"\ntitle = "x"\n'
            '[[links]]\nrelation = "cites"\ntarget_id = "rec-1"\nnote = "y"\n+++\n',
            r"unknown key note in links\[0\]",
        ),
    ],
)
def test_malformed_files_are_refused(text: str, message: str) -> None:
    """Each refusal the contract names, distinguished by its message."""
    with pytest.raises(ValidationError, match=message):
        from_markdown(text)


def test_an_exported_revision_publishes_back_as_a_revision_of_itself(store: Store) -> None:
    """Catches the export carrying the record's own supersedes link, which made every record
    with history impossible to republish (write_record refuses supersedes in links), and the
    other links being lost on the way."""
    first = store.write_record(
        RecordDraft(entry_type="note", work_status="closed", title="v1")
    ).record.id
    source = store.write_record(
        RecordDraft(entry_type="note", work_status="closed", title="source")
    ).record.id
    second = store.write_record(
        RecordDraft(
            entry_type="note",
            work_status="closed",
            title="v2",
            supersedes=first,
            reason="first fix",
            links=[Link(relation="cites", target_id=source)],
        )
    ).record
    text = to_markdown(store.get_record(second.id)).replace(
        "+++\n", '+++\nreason = "second fix"\n', 1
    )

    third = store.write_record(from_markdown(text).draft).record

    assert third.links == [
        Link(relation="cites", target_id=source),
        Link(relation="supersedes", target_id=second.id),
    ]
    assert store.get_record(second.id).superseded_by == third.id


def test_a_record_file_reports_missing_required_fields_before_type_validation() -> None:
    """Catches missing semantic fields being mislabeled as wrong types or silently defaulted,
    while present invalid values continue through the combined Pydantic error report."""
    with pytest.raises(ValidationError) as missing_entry_type:
        from_markdown('+++\nwork_status = "closed"\ntitle = "x"\n+++\n')
    with pytest.raises(ValidationError) as missing_work_status:
        from_markdown('+++\nentry_type = "note"\ntitle = "x"\n+++\n')
    with pytest.raises(ValidationError, match="field of the wrong type"):
        from_markdown('+++\nentry_type = "bogus"\nwork_status = "waiting"\ntitle = "x"\n+++\n')

    assert str(missing_entry_type.value) == (
        "front matter is missing entry_type; allowed values: problem-statement, hypothesis, "
        "procedure, result, verification, fact, insight, decision, report, note"
    )
    assert str(missing_work_status.value) == (
        "front matter is missing work_status; allowed values: open, closed"
    )
