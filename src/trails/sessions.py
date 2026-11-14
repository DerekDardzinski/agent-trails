"""Which harness and session is writing, found without the agent supplying anything.

Every write records a `Provenance`: the harness name, the session id, the session's transcript
path and the agent name. The agent never passes them; an agent asked to would get them wrong or
leave them out, and the store's point is that a write is attributable.

THE HARNESS IS DECIDED BEFORE ANY HARNESS-SPECIFIC VARIABLE IS READ (trails `fac-ahxap`). A
harness launched from another harness's shell inherits the other's environment: Claude Code
started from a Kiro shell carries `KIRO_SESSION_ID`, so reading variables first would attribute a
Claude session to Kiro.

The order:

1. The MCP client name, when the MCP binder bound one in `CLIENT_NAME`: `claude-code` is Claude
   Code, a name containing `kiro` is Kiro. Any other name falls through to step 2 and, when the
   process tree names no harness either, is stored as given.
2. The first ancestor process whose command line names Kiro (`kiro-cli` anywhere in it, or an
   executable named `kiro`) or Claude Code (an executable named `claude`). Kiro runs as a `node`
   process whose path contains `kiro-cli` (trails `fac-7azma`), so the executable's name alone
   would miss it.
3. Otherwise `unknown`, with no session.

Kiro gives an MCP server no session variable. Its session lease
`~/.kiro/session-leases/<session>.lease` holds the `pid` of the Kiro process, which is an MCP
server's parent and a shell command's ancestor, so the session is the lease whose pid is an
ancestor. It is looked up on every write rather than at server start, because a lease can change
while a server runs. `KIRO_SESSION_ID` is the fallback. Claude Code gives MCP servers
`CLAUDE_CODE_SESSION_ID`.

Nothing here raises: a write is worth more than its attribution, so any failure to read the
process table or a harness file leaves the corresponding field empty.

The process table and the home directory are the external boundaries; tests replace
`process_table` and set `HOME`.
"""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Callable, Mapping, Sequence
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path

from trails.models import Provenance

# Bound per MCP call by the MCP binder from the client's `initialize` info. None on the CLI.
CLIENT_NAME: ContextVar[str | None] = ContextVar("trails_mcp_client_name", default=None)

UNKNOWN_HARNESS = "unknown"
# The width of every harness column; a longer client name is cut rather than refusing writes.
HARNESS_CHARS = 200
KIRO = "kiro"
CLAUDE = "claude"


@dataclass(frozen=True)
class ProcessRow:
    """One row of the process table."""

    pid: int
    ppid: int
    command: str


def process_table() -> dict[int, ProcessRow]:
    """Every process on the machine, from one `ps` call. Empty when `ps` cannot be run."""
    try:
        completed = subprocess.run(
            ["ps", "-A", "-o", "pid=,ppid=,command="],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return {}
    rows: dict[int, ProcessRow] = {}
    for line in completed.stdout.splitlines():
        parts = line.split(None, 2)
        if len(parts) < 2:
            continue
        try:
            pid, ppid = int(parts[0]), int(parts[1])
        except ValueError:
            continue
        rows[pid] = ProcessRow(pid=pid, ppid=ppid, command=parts[2] if len(parts) > 2 else "")
    return rows


def ancestors(table: Mapping[int, ProcessRow], start: int) -> list[ProcessRow]:
    """The ancestors of process `start`, parent first. A loop in the table ends the walk."""
    chain: list[ProcessRow] = []
    seen = {start}
    row = table.get(start)
    while row is not None and row.ppid not in seen and row.ppid > 0:
        seen.add(row.ppid)
        row = table.get(row.ppid)
        if row is not None:
            chain.append(row)
    return chain


def current_provenance() -> Provenance:
    """The provenance of a write made now by this process."""
    return resolve(
        CLIENT_NAME.get(),
        env=os.environ,
        home=Path.home(),
        chain=lambda: ancestors(process_table(), os.getpid()),
    )


def resolve(
    client_name: str | None,
    *,
    env: Mapping[str, str],
    home: Path,
    chain: Callable[[], Sequence[ProcessRow]],
) -> Provenance:
    """Decide the harness, then read that harness's session, transcript and agent.

    `chain` is called at most once, and only when the client name does not settle the harness
    or the harness is Kiro, so a Claude Code write never runs `ps`.
    """
    ancestry: Sequence[ProcessRow] | None = None

    def processes() -> Sequence[ProcessRow]:
        nonlocal ancestry
        if ancestry is None:
            try:
                ancestry = list(chain())
            except Exception:  # the process table is a boundary; a write never fails on it
                ancestry = []
        return ancestry

    harness = harness_from_client(client_name) or harness_from_processes(processes())
    if harness is None:
        stated = (client_name or "").strip()[:HARNESS_CHARS]
        return Provenance(harness=stated or UNKNOWN_HARNESS)
    if harness == KIRO:
        return _kiro(env, home, processes())
    return _claude(env, home)


def harness_from_client(client_name: str | None) -> str | None:
    """The harness an MCP client name identifies, or None when it identifies none."""
    if not client_name:
        return None
    name = client_name.strip().lower()
    if name == "claude-code":
        return CLAUDE
    if KIRO in name:
        return KIRO
    return None


def harness_from_processes(chain: Sequence[ProcessRow]) -> str | None:
    """The harness of the nearest ancestor whose command line names one."""
    for row in chain:
        command = row.command
        executable = Path(command.split(" ", 1)[0]).name if command else ""
        if "kiro-cli" in command or executable in {"kiro", "kiro-cli"}:
            return KIRO
        if executable == "claude":
            return CLAUDE
    return None


def _kiro(env: Mapping[str, str], home: Path, chain: Sequence[ProcessRow]) -> Provenance:
    session = _kiro_session_from_leases(home, chain) or (env.get("KIRO_SESSION_ID") or None)
    if session is None:
        return Provenance(harness=KIRO)
    folder = _first(home / ".kiro" / "sessions", f"*/{session}")
    transcript = folder / "messages.jsonl" if folder is not None else None
    return Provenance(
        harness=KIRO,
        session_id=session,
        transcript=str(transcript) if transcript is not None and transcript.is_file() else None,
        agent=_kiro_agent(folder) if folder is not None else None,
    )


def _kiro_session_from_leases(home: Path, chain: Sequence[ProcessRow]) -> str | None:
    """The session whose lease names the nearest ancestor's pid."""
    leases = home / ".kiro" / "session-leases"
    by_pid: dict[int, str] = {}
    try:
        files = sorted(leases.glob("*.lease"))
    except OSError:
        return None
    for path in files:
        try:
            pid = json.loads(path.read_text()).get("pid")
        except (OSError, ValueError, AttributeError):
            continue
        if isinstance(pid, int):
            by_pid[pid] = path.stem
    for row in chain:
        if row.pid in by_pid:
            return by_pid[row.pid]
    return None


def _kiro_agent(folder: Path) -> str | None:
    try:
        mode = json.loads((folder / "session.json").read_text()).get("agentMode")
    except (OSError, ValueError, AttributeError):
        return None
    return mode if isinstance(mode, str) and mode else None


def _claude(env: Mapping[str, str], home: Path) -> Provenance:
    session = env.get("CLAUDE_CODE_SESSION_ID") or None
    if session is None:
        return Provenance(harness=CLAUDE)
    transcript = _first(home / ".claude" / "projects", f"*/{session}.jsonl")
    return Provenance(
        harness=CLAUDE,
        session_id=session,
        transcript=str(transcript) if transcript is not None else None,
    )


def _first(root: Path, pattern: str) -> Path | None:
    try:
        found = sorted(root.glob(pattern))
    except OSError:
        return None
    return found[0] if found else None
