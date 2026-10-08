"""Tests for the two binders, against probe operations defined here.

The probes are throwaway: this phase ports no real operation, so the binders are proved
against the SHAPES the porting phases rely on — a required str, a list[str], a dict[str, Any],
a Literal, a FromFile-marked prose parameter, and bodies that raise.

Deliberately NO `from __future__ import annotations`, for the reason recorded in
`src/trails/ops/__init__.py`: it would make the type-hint equality `bind` must preserve
unsatisfiable, and the real operation modules omit it too.
"""

import ast
import asyncio
import json
import subprocess
import sys
from pathlib import Path
from typing import Annotated, Any, Literal

import pytest
from fastmcp import Client, FastMCP
from pydantic import BaseModel, Field

from trails import ops
from trails.errors import TrailsError, UsageError
from trails.models import RecordDraft
from trails.ops import context
from trails.ops._marks import FromFile, Operation
from trails.store import Store
from trails.surfaces import cli as cli_surface, mcp as mcp_surface

SRC_ROOT = Path(__file__).resolve().parent.parent / "src"

# The probes' mark. The MCP binder reads an operation's mark off its module and refuses an
# unmarked one, so the probes defined in this module share this one.
OPERATION = Operation(toolset="agent", read_only=False, destructive=False, idempotent=False)


class Shapes(BaseModel):
    """Everything the probe was given, plus the store it was bound to."""

    title: str
    subjects: list[str]
    entry: dict[str, Any]
    kind: Literal["goal", "claim"]
    prose: str | None
    db_path: str


async def probe(
    title: Annotated[str, Field(description="one line")],
    subjects: Annotated[list[str], Field(description="at least one tag")],
    entry: Annotated[dict[str, Any], Field(description="a JSON object")],
    kind: Annotated[Literal["goal", "claim"], Field(description="which kind")] = "goal",
    prose: Annotated[str | None, Field(description="free prose"), FromFile()] = None,
) -> Shapes:
    """Echo every parameter shape the porting phases rely on."""
    return Shapes(
        title=title,
        subjects=subjects,
        entry=entry,
        kind=kind,
        prose=prose,
        db_path=str(context.store().db_path),
    )


class Optional(BaseModel):
    """What an optional, from-file-markable JSON object arrived as."""

    entry: dict[str, Any] | None


async def optional_entry(
    entry: Annotated[dict[str, Any] | None, Field(description="a JSON object"), FromFile()] = None,
) -> Optional:
    """Echo an optional JSON object, inline or read from a file."""
    return Optional(entry=entry)


class Refused(BaseModel):
    id: str


async def refuses_usage(flag: Annotated[str, Field(description="a flag")]) -> Refused:
    """Raise a usage error."""
    raise UsageError(f"{flag} does not apply to this record")


async def refuses_store(record_id: Annotated[str, Field(description="an id")]) -> Refused:
    """Raise a store refusal."""
    raise TrailsError(f"no record {record_id}")


async def binder_bug(label: Annotated[str, Field(description="a label")]) -> Refused:
    """Fail the way `store()` fails when no binder bound a store."""
    raise RuntimeError(f"no store is bound for this invocation ({label})")


class Maybe(BaseModel):
    id: str


async def optional_return(present: Annotated[bool, Field(description="present")]) -> Maybe | None:
    """Return an optional model. REGISTERED ONLY BY THE CHARACTERIZATION TEST."""
    return Maybe(id="present") if present else None


def _registered(monkeypatch: pytest.MonkeyPatch, *operations: Any) -> None:
    """Point the registry at probe operations; both binders read it through the module."""
    monkeypatch.setattr(ops, "operations", lambda: operations)


def _run_cli(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], argv: list[str]
) -> tuple[int, str, str]:
    """Invoke the built app the way a shell would, returning (exit code, stdout, stderr)."""
    app = cli_surface.build_cli()
    code = 0
    try:
        app(argv, exit_on_error=False)
    except SystemExit as exc:
        code = int(exc.code or 0)
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def _cli_raises(capsys: pytest.CaptureFixture[str], argv: list[str]) -> BaseException:
    """Invoke the app and return whatever escaped it, for the errors no binder may translate."""
    app = cli_surface.build_cli()
    try:
        app(argv, exit_on_error=False)
    except BaseException as exc:
        capsys.readouterr()
        return exc
    raise AssertionError("nothing escaped the app")


def _call_tool(
    server: FastMCP, tool: str, arguments: dict[str, Any], *, raise_on_error: bool = True
) -> Any:
    async def run() -> Any:
        async with Client(server) as client:
            return await client.call_tool(tool, arguments, raise_on_error=raise_on_error)

    return asyncio.run(run())


def test_cli_invokes_an_operation_with_every_parameter_shape(
    store: Store, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A shape the binder cannot build is not discovered until a phase needs it.

    The dict is the live one: cyclopts raises CoercionError on a bare dict parameter, so
    without the shared converter every operation taking a JSON object would be unusable on
    this surface while the MCP surface worked.
    """
    _registered(monkeypatch, probe)

    code, out, err = _run_cli(
        monkeypatch,
        capsys,
        [
            "probe",
            "reranking latency",
            "--subjects",
            "retrieval",
            "--subjects",
            "latency",
            "--entry",
            '{"attempt": 1}',
            "--kind",
            "claim",
            "--db",
            str(store.db_path),
        ],
    )

    assert (code, err) == (0, "")
    assert json.loads(out) == {
        "db_path": str(store.db_path),
        "entry": {"attempt": 1},
        "kind": "claim",
        "prose": None,
        "subjects": ["retrieval", "latency"],
        "title": "reranking latency",
    }


def test_cli_reads_a_from_file_parameter_and_refuses_both_forms_at_once(
    store: Store,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The pair exists only on this surface, so the exclusion is the binder's own rule.

    Accepting both forms lets one silently win, and the caller then sees text recorded that it
    did not pass.
    """
    _registered(monkeypatch, probe)
    prose = tmp_path / "framing.txt"
    prose.write_text("the frame we agreed on")
    common = ["probe", "t", "--subjects", "s", "--entry", "{}", "--db", str(store.db_path)]

    code, out, _ = _run_cli(monkeypatch, capsys, [*common, "--prose-from-file", str(prose)])
    assert code == 0
    assert json.loads(out)["prose"] == "the frame we agreed on"

    both = [*common, "--prose", "inline", "--prose-from-file", str(prose)]
    code, _, _ = _run_cli(monkeypatch, capsys, both)

    # Exit 2, because supplying both forms is a mistake in the invocation rather than the
    # store refusing something legitimate to ask for.
    assert code == 2


def test_cli_exit_codes_distinguish_a_usage_error_from_a_refusal(
    store: Store, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Collapsing the two makes a flag that does not apply read as a refusal.

    An agent driving the CLI reads the difference to decide whether to change the invocation or
    to accept the store's answer.
    """
    _registered(monkeypatch, refuses_usage, refuses_store)

    usage_code, _, usage_err = _run_cli(
        monkeypatch, capsys, ["refuses", "usage", "--flag", "strict", "--db", str(store.db_path)]
    )
    refusal_code, _, refusal_err = _run_cli(
        monkeypatch,
        capsys,
        ["refuses", "store", "--record-id", "01ABC", "--db", str(store.db_path)],
    )

    assert usage_code == 2
    assert usage_err == "usage: strict does not apply to this record\n"
    assert refusal_code == 1
    assert refusal_err == "error: no record 01ABC\n"


def test_mcp_returns_a_refusal_as_an_error_result_with_readable_data(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A false error flag makes a client treat a refused write as successful; missing content
    leaves an agent unable to correct the call.

    Both refusal classes are covered because the binder catches them at one place and a future
    change could easily let one past.
    """
    _registered(monkeypatch, refuses_usage, refuses_store)
    server = mcp_surface.build_mcp_server(store.db_path)

    usage = _call_tool(server, "refuses_usage", {"flag": "--strict"}, raise_on_error=False)
    refusal = _call_tool(server, "refuses_store", {"record_id": "01ABC"}, raise_on_error=False)

    assert usage.is_error is True
    assert usage.structured_content == {"error": "--strict does not apply to this record"}
    assert refusal.is_error is True
    assert refusal.structured_content == {"error": "no record 01ABC"}


def test_mcp_tool_schema_carries_only_the_operation_parameters(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A transport affordance in the schema is a parameter the agent cannot use.

    `db` and `prose_from_file` exist on the CLI only, and the FromFile mark must add nothing:
    an agent told to pass a path here would have its value read as prose.
    """
    _registered(monkeypatch, probe)
    server = mcp_surface.build_mcp_server(store.db_path)

    async def schema() -> dict[str, Any]:
        async with Client(server) as client:
            return {tool.name: tool.inputSchema for tool in await client.list_tools()}["probe"]

    properties = asyncio.run(schema())["properties"]

    assert sorted(properties) == ["entry", "kind", "prose", "subjects", "title"]
    assert properties["prose"] == {
        "anyOf": [{"type": "string"}, {"type": "null"}],
        "default": None,
        "description": "free prose",
    }


def test_the_two_surfaces_emit_the_same_object_for_the_same_call(
    store: Store, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The whole point: two surfaces answering the same question with the same object.

    Byte-comparable payloads are what make the parity assertions in the porting phases
    meaningful, and they hold only because every operation returns a top-level BaseModel.
    """
    _registered(monkeypatch, probe)
    arguments = {
        "title": "reranking latency",
        "subjects": ["retrieval"],
        "entry": {"attempt": 1},
        "kind": "claim",
    }

    code, out, _ = _run_cli(
        monkeypatch,
        capsys,
        [
            "probe",
            "reranking latency",
            "--subjects",
            "retrieval",
            "--entry",
            '{"attempt": 1}',
            "--kind",
            "claim",
            "--db",
            str(store.db_path),
        ],
    )
    server = mcp_surface.build_mcp_server(store.db_path)
    result = _call_tool(server, "probe", arguments)

    assert code == 0
    assert json.loads(out) == result.structured_content


def test_real_search_has_structural_parity_between_cli_and_mcp(
    store: Store, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Catches one surface wrapping, aliasing, or dropping compact SearchHit fields while the
    other emits the operation model unchanged."""
    record = store.write_record(
        RecordDraft(
            entry_type="result",
            work_status="closed",
            title="Batching cut p99 latency",
            body="The measured p99 fell after batching.",
            projects=[],
        )
    ).record

    code, out, err = _run_cli(
        monkeypatch,
        capsys,
        ["search", "p99", "--json", "--db", str(store.db_path)],
    )
    result = _call_tool(
        mcp_surface.build_mcp_server(store.db_path),
        "search",
        {"query": "p99"},
    )

    assert (code, err) == (0, "")
    assert json.loads(out) == result.structured_content
    assert result.structured_content["hits"][0]["id"] == record.id
    assert set(result.structured_content["hits"][0]) == {
        "id",
        "entry_type",
        "title",
        "projects",
        "created_at",
        "lifecycle",
        "superseded_by",
        "work_status",
        "tags",
        "matched_by",
        "keyword_matches",
        "excerpt",
    }


def test_an_optional_model_return_produces_different_shapes_on_the_two_surfaces(
    store: Store,
) -> None:
    """CHARACTERIZATION, and the reason the registry refuses this return type.

    fastmcp wraps an optional model in a synthetic `{"result": ...}` envelope while the CLI
    binder dumps the model bare, so the two surfaces emit different objects for the same call.
    Recorded here so a later builder cannot relax the rule by accident, and so a change in
    fastmcp's behaviour is reported rather than silently making the rule pointless. It is
    paired with the registry invariant in tests/test_ops_registry.py, which is what stops a
    real operation reaching a surface this way.
    """
    server = FastMCP("characterization")
    server.tool(context.bind(optional_return, lambda: store.db_path), name="optional")

    present = _call_tool(server, "optional", {"present": True})
    absent = _call_tool(server, "optional", {"present": False})

    assert present.structured_content == {"result": {"id": "present"}}
    assert absent.structured_content == {"result": None}
    # What the CLI binder would emit for the same present value: the model, bare.
    assert json.dumps(Maybe(id="present").model_dump(), sort_keys=True) == '{"id": "present"}'


def test_cli_delivers_an_optional_json_object_inline_and_from_a_file(
    store: Store,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The shape the porting phases use for update's flags: an optional, from-file-markable
    JSON object.

    Reading the annotation only one level deep left the dict inside the union unseen, so the
    parameter got no converter: the inline form died with a CoercionError and the from-file
    form handed the operation raw text where it expected an object. Neither failure is visible
    to a probe carrying a REQUIRED dict, which is why both forms are asserted here.
    """
    _registered(monkeypatch, optional_entry)
    entry_file = tmp_path / "entry.json"
    entry_file.write_text('{"attempt": 2}')

    inline_code, inline_out, inline_err = _run_cli(
        monkeypatch, capsys, ["optional", "entry", "--entry", '{"attempt": 1}']
    )
    file_code, file_out, _ = _run_cli(
        monkeypatch, capsys, ["optional", "entry", "--entry-from-file", str(entry_file)]
    )
    omitted_code, omitted_out, _ = _run_cli(monkeypatch, capsys, ["optional", "entry"])

    assert (inline_code, inline_err) == (0, "")
    assert json.loads(inline_out) == {"entry": {"attempt": 1}}
    assert file_code == 0
    assert json.loads(file_out) == {"entry": {"attempt": 2}}
    assert omitted_code == 0
    assert json.loads(omitted_out) == {"entry": None}


def test_neither_binder_translates_a_runtime_error(
    store: Store, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A RuntimeError is a BINDER bug, not a refusal the agent asked for.

    `store()` raises exactly this when no binder bound a store, which
    tests/test_ops_context.py asserts directly. Here it goes through the BUILT binders, because
    the failure to catch is one of them widening its translator: `except RuntimeError` beside
    `except TrailsError` would turn a bug in our own code into an exit code or an
    `{"error": ...}` payload, telling the agent to reason about a store refusal that never
    happened. Both surfaces must let it out.
    """
    _registered(monkeypatch, binder_bug)

    # `binder_bug` carries an underscore, so its command path is `binder bug`.
    escaped = _cli_raises(capsys, ["binder", "bug", "--label", "x", "--db", str(store.db_path)])

    assert isinstance(escaped, RuntimeError)
    assert not isinstance(escaped, SystemExit)

    server = mcp_surface.build_mcp_server(store.db_path)
    result = _call_tool(server, "binder_bug", {"label": "x"}, raise_on_error=False)

    assert result.is_error is True
    assert result.structured_content is None


@pytest.mark.parametrize("module", ["trails.surfaces.cli", "trails.cli"])
def test_the_cli_binder_does_not_import_fastmcp(module: str) -> None:
    """Measured 480ms against 62ms, paid on every CLI invocation and invisible to every
    functional test.

    Both modules are checked because they can fail independently. The binder is where the
    import would be reached for; `trails.cli` is the module `trails` actually loads, and it
    is the one that could acquire the cost by importing the wrong builder — which is exactly
    what the cutover changed. A slow import is not a wrong answer, so nothing else notices.

    A subprocess is required because this session has already imported fastmcp, so an
    in-process check would pass no matter what the module imports.
    """
    probe_source = (
        f"import sys; import {module}; print('fastmcp' in sys.modules or 'mcp' in sys.modules)"
    )
    completed = subprocess.run(
        [sys.executable, "-c", probe_source],
        capture_output=True,
        text=True,
        check=True,
        cwd=SRC_ROOT.parent,
    )

    assert completed.stdout.strip() == "False"


def test_no_module_uses_fastmcp_exclude_args() -> None:
    """`exclude_args` is deprecated, and it is how a surface-specific parameter comes back.

    An operation signature carries only operation parameters, so there is never anything to
    exclude — but the API still works on this version, so nothing else would notice its return.
    """
    offenders = [
        path.relative_to(SRC_ROOT).as_posix()
        for path in sorted(SRC_ROOT.rglob("*.py"))
        for node in ast.walk(ast.parse(path.read_text()))
        if isinstance(node, ast.Call)
        and any(keyword.arg == "exclude_args" for keyword in node.keywords)
    ]

    assert offenders == []


def test_publish_help_shows_field_descriptions_and_the_entry_format(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """labbook-79y.6 items 6 and 17, over the real registry. Silent failure: the CLI help
    showing parameter names only, so an agent cannot tell what a list-of-object flag takes;
    measured in the labbook-79y.5 probe as two refused publishes."""
    code, raw, _ = _run_cli(monkeypatch, capsys, ["publish", "--help"])
    # The help is boxed and wrapped to the terminal width, so compare it as one line of words.
    out = " ".join(raw.replace("│", " ").split())

    assert code == 0
    assert (
        "One line stating the claim or outcome rather than only naming the activity; prime "
        "lists this title when orienting an agent."
    ) in out
    assert (
        "Open means a later action is expected on this exact record, for example an untested "
        "hypothesis or a note with an unresolved question; closed means no later action is "
        "expected on it. A record may be current and closed."
    ) in out
    assert (
        'Each entry is one JSON object with keys "relation", "target_id"; repeat the flag'
    ) in out
    assert 'with keys "location", "digest" (optional), "notes" (optional)' in out
    assert '"sha256:<64 lowercase hex>"' in out


def test_cli_publish_takes_each_link_and_artifact_as_one_json_object(
    store: Store, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """labbook-79y.6 item 17. Silent failure: a link or artifact accepted in a form the help
    does not describe, or a malformed one stored rather than refused as a usage error."""
    db = ["--db", str(store.db_path)]
    required = ["--entry-type", "note", "--work-status", "closed"]
    _, first, _ = _run_cli(
        monkeypatch, capsys, ["publish", *required, "--title", "a", "--json", *db]
    )
    target = json.loads(first)["record_id"]
    link = json.dumps({"relation": "cites", "target_id": target})
    artifact = json.dumps({"location": "s3://bucket/key", "digest": None})

    code, out, _ = _run_cli(
        monkeypatch,
        capsys,
        [
            "publish",
            *required,
            "--title",
            "b",
            "--links",
            link,
            "--artifacts",
            artifact,
            "--json",
            *db,
        ],
    )
    bad, _, err = _run_cli(
        monkeypatch,
        capsys,
        [
            "publish",
            *required,
            "--title",
            "c",
            "--links",
            '{"relation": "bogus", "target_id": "x"}',
            *db,
        ],
    )

    record = store.get_record(json.loads(out)["record_id"])
    assert code == 0
    assert [(x.relation, x.target_id) for x in record.links] == [("cites", target)]
    assert [(x.location, x.digest, x.notes) for x in record.artifacts] == [
        ("s3://bucket/key", None, "")
    ]
    assert bad == 2
    assert err.startswith("usage: not a valid Link")
