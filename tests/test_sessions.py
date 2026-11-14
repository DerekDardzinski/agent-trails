"""Session and harness detection, with the process table, home directory and environment
replaced, since those are the boundaries `sessions` reads."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from trails import sessions
from trails.models import Provenance
from trails.sessions import ProcessRow

KIRO_NODE = (
    "/Users/someone/Library/Application Support/kiro-cli/node --experimental-wasm-modules "
    "/Users/someone/Library/Application Support/kiro-cli/kas/3.3.0/index.js"
)


def kiro_home(home: Path, session: str, pid: int, agent: str | None = "lab-lead") -> Path:
    """A home directory holding one Kiro lease and one Kiro session folder."""
    leases = home / ".kiro" / "session-leases"
    leases.mkdir(parents=True, exist_ok=True)
    (leases / f"{session}.lease").write_text(
        json.dumps({"v": 1, "pid": pid, "clientName": "synth"})
    )
    folder = home / ".kiro" / "sessions" / "04fdfad6869b6b02" / session
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "messages.jsonl").write_text("{}\n")
    (folder / "session.json").write_text(json.dumps({"id": session, "agentMode": agent}))
    return folder / "messages.jsonl"


def chain(*rows: ProcessRow):
    return lambda: list(rows)


def test_kiro_lease_names_the_session_of_the_nearest_kiro_ancestor(tmp_path: Path) -> None:
    """Silent failure caught: a write attributed to another live Kiro session. Two leases
    exist; only the one whose pid is this process's ancestor is ours."""
    transcript = kiro_home(tmp_path, "sess_ours", pid=200)
    kiro_home(tmp_path, "sess_other", pid=300, agent="lab-scout")
    found = sessions.resolve(
        None,
        env={"KIRO_SESSION_ID": "sess_from_env"},
        home=tmp_path,
        chain=chain(ProcessRow(100, 200, "zsh"), ProcessRow(200, 1, KIRO_NODE)),
    )
    assert found == Provenance(
        harness="kiro", session_id="sess_ours", transcript=str(transcript), agent="lab-lead"
    )


def test_kiro_without_a_matching_lease_falls_back_to_the_environment(tmp_path: Path) -> None:
    """Silent failure caught: a Kiro write recorded with no session although Kiro exported one
    to the shell."""
    found = sessions.resolve(
        None,
        env={"KIRO_SESSION_ID": "sess_env"},
        home=tmp_path,
        chain=chain(ProcessRow(200, 1, KIRO_NODE)),
    )
    assert found == Provenance(harness="kiro", session_id="sess_env")


def test_kiro_mcp_server_never_takes_the_inherited_session_variable(tmp_path: Path) -> None:
    """Silent failure caught: an MCP write under Kiro attributed to the session of the shell
    that launched Kiro. Kiro sets no `KIRO_SESSION_ID` for MCP servers, so one they see is
    inherited (trails fac-x8s2n)."""
    kiro_home(tmp_path, "sess_launching_shell", pid=900)
    found = sessions.resolve(
        "kiro",
        env={"KIRO_SESSION_ID": "sess_launching_shell"},
        home=tmp_path,
        chain=chain(ProcessRow(200, 1, KIRO_NODE)),
    )
    assert found == Provenance(harness="kiro")


def test_cli_under_synth_takes_a_kiro_session_that_has_a_lease(tmp_path: Path) -> None:
    """Silent failure caught: a Kiro agent's CLI write under Synth recorded as `unknown`.
    Synth runs the agent's shell commands, so no Kiro process is an ancestor (fac-x8s2n)."""
    transcript = kiro_home(tmp_path, "sess_live", pid=200)
    found = sessions.resolve(
        None,
        env={"KIRO_SESSION_ID": "sess_live"},
        home=tmp_path,
        chain=chain(ProcessRow(100, 50, "/bin/zsh -c trails write"), ProcessRow(50, 1, "synth")),
    )
    assert found == Provenance(
        harness="kiro", session_id="sess_live", transcript=str(transcript), agent="lab-lead"
    )


def test_stale_lease_with_a_reused_pid_loses_to_the_live_one(tmp_path: Path) -> None:
    """Silent failure caught: writes attributed to an ended session whose lease was left behind
    and whose pid a new Kiro process reused (stale leases observed in fac-x8s2n). The stale
    lease sorts last by name, so name order alone would pick it."""
    leases = tmp_path / ".kiro" / "session-leases"
    leases.mkdir(parents=True)
    (leases / "sess_zz_stale.lease").write_text(
        json.dumps({"pid": 200, "parentPid": 77, "acquiredAt": "2026-11-14T04:40:06.717Z"})
    )
    (leases / "sess_aa_live.lease").write_text(
        json.dumps({"pid": 200, "parentPid": 150, "acquiredAt": "2026-11-14T05:10:00.000Z"})
    )
    (leases / "sess_mm_stale_same_parent.lease").write_text(
        json.dumps({"pid": 200, "parentPid": 150, "acquiredAt": "2026-11-13T01:00:00.000Z"})
    )
    found = sessions.resolve(
        "kiro", env={}, home=tmp_path, chain=chain(ProcessRow(200, 150, KIRO_NODE))
    )
    assert found == Provenance(harness="kiro", session_id="sess_aa_live")


def test_an_empty_mcp_client_name_is_not_the_cli(tmp_path: Path) -> None:
    """Silent failure caught: an MCP client with no name taking the CLI path, which accepts
    `KIRO_SESSION_ID` and so attributes the write to the launching shell's session."""
    kiro_home(tmp_path, "sess_launching_shell", pid=900)
    env = {"KIRO_SESSION_ID": "sess_launching_shell"}
    assert sessions.resolve("", env=env, home=tmp_path, chain=chain()) == Provenance(
        harness="unknown"
    )
    assert sessions.resolve(
        "", env=env, home=tmp_path, chain=chain(ProcessRow(200, 1, KIRO_NODE))
    ) == Provenance(harness="kiro")


def test_claude_reads_its_variable_and_ignores_an_inherited_kiro_one(tmp_path: Path) -> None:
    """Silent failure caught: a Claude Code session started from a Kiro shell attributed to the
    Kiro session whose variable it inherited (trails fac-ahxap)."""
    kiro_home(tmp_path, "sess_kiro", pid=200)
    transcript = tmp_path / ".claude" / "projects" / "-Users-someone-repo" / "d35e71f8.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text("{}\n")
    found = sessions.resolve(
        "claude-code",
        env={"KIRO_SESSION_ID": "sess_kiro", "CLAUDE_CODE_SESSION_ID": "d35e71f8"},
        home=tmp_path,
        chain=chain(ProcessRow(150, 200, "claude"), ProcessRow(200, 1, KIRO_NODE)),
    )
    assert found == Provenance(harness="claude", session_id="d35e71f8", transcript=str(transcript))


def test_claude_found_by_process_when_no_client_name(tmp_path: Path) -> None:
    """Silent failure caught: a CLI write from a Claude shell inside a Kiro shell attributed to
    Kiro. The nearest harness ancestor decides."""
    found = sessions.resolve(
        None,
        env={"KIRO_SESSION_ID": "sess_kiro", "CLAUDE_CODE_SESSION_ID": "abc"},
        home=tmp_path,
        chain=chain(
            ProcessRow(120, 150, "/bin/zsh"),
            ProcessRow(150, 200, "/opt/bin/claude --resume"),
            ProcessRow(200, 1, KIRO_NODE),
        ),
    )
    assert found == Provenance(harness="claude", session_id="abc")


def test_unknown_client_name_is_stored_as_given_when_no_harness_ancestor(tmp_path: Path) -> None:
    """Silent failure caught: a write from an unrecognised harness recorded as `unknown`,
    losing the one name the client gave."""
    found = sessions.resolve("cursor-vscode", env={}, home=tmp_path, chain=chain())
    assert found == Provenance(harness="cursor-vscode")


def test_no_harness_at_all_records_unknown(tmp_path: Path) -> None:
    found = sessions.resolve(
        None,
        env={"KIRO_SESSION_ID": "sess_x"},
        home=tmp_path,
        chain=chain(ProcessRow(1, 0, "launchd")),
    )
    assert found == Provenance(harness="unknown")


def test_process_table_failure_never_fails_the_write(tmp_path: Path) -> None:
    """Silent failure caught: an exception from `ps` escaping into a write, which would refuse
    a valid write for want of attribution."""

    def broken() -> list[ProcessRow]:
        raise OSError("ps missing")

    assert sessions.resolve(None, env={}, home=tmp_path, chain=broken) == Provenance(
        harness="unknown"
    )


def test_ancestors_walks_parents_and_stops_on_a_loop() -> None:
    table = {
        10: ProcessRow(10, 20, "python"),
        20: ProcessRow(20, 30, "zsh"),
        30: ProcessRow(30, 20, "loop"),
    }
    assert [row.pid for row in sessions.ancestors(table, 10)] == [20, 30]


def test_current_provenance_reads_the_bound_client_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Silent failure caught: the MCP binder's client name never reaching the resolver, so
    every MCP write falls back to the process tree."""
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "s-1")
    token = sessions.CLIENT_NAME.set("claude-code")
    try:
        assert sessions.current_provenance() == Provenance(harness="claude", session_id="s-1")
    finally:
        sessions.CLIENT_NAME.reset(token)
