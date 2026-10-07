"""The only module that runs the `dolt` binary.

Every other module reaches Dolt through the functions here, so the checks
that make a subprocess call safe — binary present, database present, non-zero
exit reported — exist in exactly one place.

Three behaviours of `dolt sql -q` were established by probe on dolt 1.86.5 and
the code depends on all three:

* `START TRANSACTION; INSERT ...` with no `COMMIT` ROLLS BACK. This is what makes
  a partial write invisible, and it is why `run_script` sends one script rather
  than looping over statements.
* A statement failing mid-script ABORTS THE REMAINING STATEMENTS, so a trailing
  `COMMIT` never runs and the transaction rolls back.
* `dolt sql -q "SELECT 1"` SUCCEEDS in a directory that is not a Dolt database.
  Without the `.dolt` guard below, a read against an uninitialised path would
  return no rows and an absent store would be indistinguishable from an empty
  one.

THE COST OF THIS MODEL IS MEASURED, AND THE MODEL IS KEPT DELIBERATELY.
Probed on dolt 1.86.5: `dolt version` alone costs 47ms, `dolt sql -q "SELECT 1"`
costs 70ms, and TWENTY STATEMENTS IN ONE INVOCATION ALSO COST 70ms. Invocations
are the entire cost and statements within one are free, so roughly 47ms of every
operation is booting the binary. That is why every read in `store` is batch-first:
a loop over ids at the Store API is a loop over process spawns.

A long-lived `dolt sql-server` with a MySQL driver would cut per-operation cost
about 70x. It was considered and NOT taken, for three reasons recorded here so the
next reader does not re-derive them:

1. The workload does not need it. One read operation costs a bounded number of
   invocations, flat in the number of records — under a second, which is invisible beside
   the model inference these tool calls sit inside.
2. The safety property above is currently FREE. One invocation is one transaction
   and a failed `CALL DOLT_COMMIT` rolls back with the process. Under a connection
   that becomes something to maintain: explicit transaction scoping, a crashed
   agent leaving a transaction open, server lifecycle, and two agents sharing one
   server.
3. The remaining pain is the test suite, which is a developer-experience number
   rather than a runtime one, and parallelising it already cut the suite 3.5x.

Revisit when something concrete makes it hurt — a hot loop reading the store
hundreds of times, or a study an order of magnitude larger than any measured
here. Concurrent writers are handled by the lock in `run_script`, and a server that
replaces it changes this one module.
"""

from __future__ import annotations

import fcntl
import json
import shutil
import subprocess
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from trails.errors import DoltError, StoreUnavailableError

DOLT_DATETIME = "%Y-%m-%d %H:%M:%S.%f"

# Held EXCLUSIVELY around every write and SHARED around every read. A `dolt sql` process that
# starts while another holds the database opens it read-only, and a writer opened that way
# learns it at CALL DOLT_COMMIT: "cannot update manifest: database is read only". Measured on
# dolt 1.86.5 with no lock: 8 writers x 50 writes persisted 121 of 400. Measured on dolt 2.4.0
# with the exclusive lock on writes only: an unlocked READ overlapping a commit still failed
# the write (8 x 25 persisted 175 of 200, twice; the 8 x 50 test failed 4 of 4 runs). With the
# shared lock on reads as well, 400 of 400 in 3 of 3 runs. Readers do not
# block each other. flock releases when its holder dies, so a crashed process cannot wedge
# the store.
WRITE_LOCK_NAME = ".trails-write.lock"

# The databases whose write lock THIS execution context already holds exclusively, through
# `exclusive`. flock is per open file, so a second open-and-lock of the same file inside the
# holder would wait on itself forever; a call made inside `exclusive` skips the lock instead.
_HELD_EXCLUSIVE: ContextVar[frozenset[Path]] = ContextVar("_HELD_EXCLUSIVE", default=frozenset())


def sql_literal(value: str | int | datetime | None) -> str:
    """Render a Python value as a SQL literal.

    Dolt has no client-side parameter binding through `dolt sql -q`, so literals
    are built here and nowhere else.
    """
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        # Before the int branch: bool is an int subclass, and silently writing
        # 1/0 into an ENUM or VARCHAR column would store a value no reader wants.
        raise TypeError("bool is not a SQL literal in this schema; pass an explicit value")
    if isinstance(value, datetime):
        # Dolt DATETIME is tz-naive and rejects an offset suffix, so an aware
        # value is converted to UTC and a naive one is assumed to be UTC already.
        moment = value.astimezone(UTC) if value.tzinfo is not None else value
        return f"'{moment.strftime(DOLT_DATETIME)}'"
    if isinstance(value, int):
        return str(value)
    if "\x00" in value:
        raise ValueError("NUL byte cannot be stored in a SQL string literal")
    escaped = (
        value.replace("\\", "\\\\").replace("'", "\\'").replace("\n", "\\n").replace("\r", "\\r")
    )
    return f"'{escaped}'"


def vector_literal(values: Sequence[float]) -> str:
    """Render floats as `[0.1,0.2,...]`, the body `STRING_TO_VECTOR` accepts.

    FIXED POINT, NEVER `repr`. Six write forms were probed on dolt 1.86.5 and only
    `STRING_TO_VECTOR('[...]')` works — a plain string, an integer string,
    `JSON_ARRAY(...)`, a spaced string, EXPONENT NOTATION and `CAST(... AS VECTOR(n))`
    all fail. `repr(1e-05)` produces exponent notation, so formatting through `repr`
    would fail on whichever component of whichever vector happened to be small enough,
    which is a failure that appears with the data rather than with the code.

    Nine decimals exceeds float32's ~7 significant digits over the unit-norm range
    these vectors occupy, so nothing meaningful is lost.

    This is the ONLY place a vector becomes SQL. The write path and the vector query
    both call it, so a stored vector and a query vector cannot be serialised
    differently — which would be invisible, since both would still be valid vectors.
    """
    return "[" + ",".join(f"{float(value):.9f}" for value in values) + "]"


def init_database(db_path: Path) -> None:
    """Create an empty Dolt database at `db_path`, idempotently.

    Creates no tables: the DDL belongs to `store`, the binary belongs here.
    """
    try:
        db_path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        # Translated rather than propagated: `open_store` contracts to raise
        # StoreUnavailableError when the database cannot be opened, and a raw
        # NotADirectoryError would escape the CLI's error handling as a traceback.
        raise StoreUnavailableError(f"cannot create a database at {db_path}: {exc}") from exc
    if (db_path / ".dolt").is_dir():
        return
    _run(db_path, "init", require_db=False)


def read_rows(db_path: Path, statement: str) -> list[dict[str, Any]]:
    """Run one read statement and return its rows as dicts.

    A JSON column arrives already decoded into a dict or list — `-r json` parses
    it — so callers must not `json.loads` it a second time. Holds the write lock SHARED, so
    the read cannot overlap a commit (see WRITE_LOCK_NAME).
    """
    with _locked(db_path, fcntl.LOCK_SH):
        completed = _run(db_path, "sql", "-q", statement, "-r", "json", require_db=True)
    out = completed.stdout.strip()
    if not out:
        return []
    rows = json.loads(out).get("rows", [])
    return list(rows)


def run_script(db_path: Path, statements: Sequence[str]) -> None:
    """Run `statements` as ONE `dolt sql` invocation, and so one transaction.

    Sending them separately would autocommit each one, which would leave an interrupted write
    half-recorded.

    THE ONE FUNCTION EVERY WRITE PASSES THROUGH, and so the one place writes are serialized:
    an exclusive `fcntl.flock` on `<db_path>/.trails-write.lock` is held for the duration of
    the subprocess. Reads hold the same lock shared. There is no retry.
    """
    if not statements:
        return
    with _locked(db_path, fcntl.LOCK_EX):
        _run(db_path, "sql", "-q", "; ".join(statements), require_db=True)


def read_result_sets(db_path: Path, statements: Sequence[str]) -> list[list[dict[str, Any]]]:
    """Run several read statements in ONE invocation and return one row list per statement.

    `dolt sql -r json` prints one JSON document per statement, `{}` for an empty result
    (probed on dolt 2.4.0), so a batch of reads costs one process spawn rather than one each.

    Raises:
        DoltError: the read failed, or the output did not hold one document per statement.
    """
    if not statements:
        return []
    with _locked(db_path, fcntl.LOCK_SH):
        completed = _run(db_path, "sql", "-q", "; ".join(statements), "-r", "json", require_db=True)
    decoder = json.JSONDecoder()
    text = completed.stdout
    position = 0
    results: list[list[dict[str, Any]]] = []
    while True:
        while position < len(text) and text[position].isspace():
            position += 1
        if position >= len(text):
            break
        document, position = decoder.raw_decode(text, position)
        results.append(list(document.get("rows", [])))
    if len(results) != len(statements):
        raise DoltError(f"expected {len(statements)} result sets from dolt, got {len(results)}")
    return results


@contextmanager
def exclusive(db_path: Path) -> Iterator[None]:
    """Hold the write lock EXCLUSIVELY across several reads, writes and commands.

    For an operation whose reads must not go stale before its write, such as retire, which
    reads whether a record is already retired and then writes. Every `read_rows`,
    `read_result_sets` and `run_script` call made inside the block in the same execution context runs
    under this one lock rather than taking its own. Other processes wait for the whole block.
    Nesting `exclusive` on the same database is allowed and takes nothing more.

    Raises:
        StoreUnavailableError: there is no database at `db_path`.
    """
    key = db_path.resolve()
    if key in _HELD_EXCLUSIVE.get():
        yield
        return
    with _locked(db_path, fcntl.LOCK_EX):
        token = _HELD_EXCLUSIVE.set(_HELD_EXCLUSIVE.get() | {key})
        try:
            yield
        finally:
            _HELD_EXCLUSIVE.reset(token)


@contextmanager
def _locked(db_path: Path, mode: int) -> Iterator[None]:
    """Hold `<db_path>/.trails-write.lock` in `mode` (LOCK_SH or LOCK_EX) for the block.

    Takes nothing when this execution context already holds it through `exclusive`.
    """
    _require_database(db_path)
    if db_path.resolve() in _HELD_EXCLUSIVE.get():
        yield
        return
    with (db_path / WRITE_LOCK_NAME).open("a") as lock:
        fcntl.flock(lock, mode)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def _run(db_path: Path, *args: str, require_db: bool) -> subprocess.CompletedProcess[str]:
    if require_db:
        _require_database(db_path)
    elif shutil.which("dolt") is None:
        raise StoreUnavailableError("the `dolt` binary is not on PATH; trails requires it")
    completed = subprocess.run(
        ["dolt", *args],
        cwd=db_path,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise DoltError(f"dolt {args[0]} failed: {completed.stderr.strip() or completed.stdout}")
    return completed


def _require_database(db_path: Path) -> None:
    if shutil.which("dolt") is None:
        raise StoreUnavailableError("the `dolt` binary is not on PATH; trails requires it")
    if not (db_path / ".dolt").is_dir():
        raise StoreUnavailableError(
            f"no Dolt database at {db_path}; any trails operation that reads or writes "
            "creates it. `dolt sql` would otherwise succeed here and report an absent store "
            "as an empty one"
        )
