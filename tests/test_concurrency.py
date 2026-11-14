"""The process boundary of the Dolt write lock.

Measured on Dolt 1.86.5 before the lock existed: 8 processes of 50 writes persisted 121 of
400, every loser failing at CALL DOLT_COMMIT with "cannot update manifest: database is read
only". Repeating 400 writes in every gate is a stress test, not a regression test. This module
checks the mechanism directly: a production writer must wait while another process owns the
lock, then write exactly once after it is released.
"""

from __future__ import annotations

import fcntl
import select
import subprocess
import sys

from trails import dolt
from trails.entry_store import EntryStore
from trails.models import EntryDraft

_WORKER = """
import sys
from pathlib import Path
from trails.entry_store import EntryStore
from trails.models import EntryDraft

store = EntryStore(db_path=Path(sys.argv[1]))
print("ready", flush=True)
sys.stdin.readline()
store.write_entry(EntryDraft(kind="note", title="child write", tags=["concurrency"]))
print("written", flush=True)
"""


_VERSION_WORKER = """
import sys
from pathlib import Path
from trails.entry_store import EntryStore
from trails.errors import ValidationError
from trails.models import EntryDraft

store = EntryStore(db_path=Path(sys.argv[1]))
print("ready", flush=True)
sys.stdin.readline()
try:
    entry = store.write_entry(
        EntryDraft(id=sys.argv[2], base_version=1, reason="race", body=sys.argv[3])
    ).entry
    print(f"written:{entry.current.body}", flush=True)
except ValidationError as exc:
    print(f"refused:{exc}", flush=True)
"""


def test_a_writer_waits_for_a_lock_held_by_another_process(store: EntryStore) -> None:
    """Catches a write entering Dolt while another process holds the trails write lock.

    The ready handshake means the child interpreter and imports have finished before the timed
    assertion. Without the production lock, one write finishes well inside two seconds; with
    it, no output or exit can occur until the parent releases the lock.
    """
    lock_path = store.db_path / dolt.WRITE_LOCK_NAME
    worker = subprocess.Popen(
        [sys.executable, "-c", _WORKER, str(store.db_path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert worker.stdout is not None
        assert worker.stdin is not None
        assert worker.stdout.readline().strip() == "ready"
        with lock_path.open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            worker.stdin.write("write\n")
            worker.stdin.flush()
            readable, _, _ = select.select([worker.stdout], [], [], 2.0)
            assert readable == []
            assert worker.poll() is None
        stdout, stderr = worker.communicate(timeout=60)
    finally:
        _stop(worker)

    assert (worker.returncode, stderr, stdout.strip()) == (0, "", "written")
    assert store.query("SELECT title FROM entry_version") == [{"title": "child write"}]


def test_two_racing_versions_from_one_base_allow_exactly_one_to_land(store: EntryStore) -> None:
    """Catches a stale base_version check letting two processes each write version 2 from
    version 1, so one silently overwrites the other's edit."""
    old = store.write_entry(EntryDraft(kind="note", title="old")).entry.id
    workers = [
        subprocess.Popen(
            [sys.executable, "-c", _VERSION_WORKER, str(store.db_path), old, title],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for title in ("first", "second")
    ]
    inputs = []
    try:
        for worker in workers:
            assert worker.stdout is not None
            assert worker.stdin is not None
            assert worker.stdout.readline().strip() == "ready"
            inputs.append(worker.stdin)
        for input_stream in inputs:
            input_stream.write("write\n")
            input_stream.flush()
        completed = [worker.communicate(timeout=60) for worker in workers]
    finally:
        for worker in workers:
            _stop(worker)

    outputs = [stdout.strip() for stdout, _stderr in completed]
    errors = [stderr for _stdout, stderr in completed]
    written = [
        output.removeprefix("written:") for output in outputs if output.startswith("written:")
    ]
    refused = [output for output in outputs if output.startswith("refused:")]

    assert errors == ["", ""]
    assert len(written) == len(refused) == 1
    assert "base_version=1 is stale" in refused[0]
    assert [v.body for v in store.get_versions(old)] == ["", written[0]]


def _stop(worker: subprocess.Popen[str]) -> None:
    """Leave no child behind when an assertion or timeout interrupts the test."""
    if worker.poll() is None:
        worker.kill()
        worker.communicate(timeout=10)
