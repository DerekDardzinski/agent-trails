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
from trails.store import Store

_WORKER = """
import sys
from pathlib import Path
from trails.models import RecordDraft
from trails.store import Store

store = Store(db_path=Path(sys.argv[1]))
print("ready", flush=True)
sys.stdin.readline()
store.write_record(
    RecordDraft(
        entry_type="note", work_status="closed", title="child write", tags=["concurrency"]
    )
)
print("written", flush=True)
"""


def test_a_writer_waits_for_a_lock_held_by_another_process(store: Store) -> None:
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
    assert store.query("SELECT title FROM record") == [{"title": "child write"}]


def _stop(worker: subprocess.Popen[str]) -> None:
    """Leave no child behind when an assertion or timeout interrupts the test."""
    if worker.poll() is None:
        worker.kill()
        worker.communicate(timeout=10)
