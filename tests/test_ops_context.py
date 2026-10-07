"""Tests for the store binding an operation reads through `ops.context.store`.

Deliberately NO `from __future__ import annotations`: with it, `get_type_hints` re-evaluates
the annotation strings on every call and builds a fresh pydantic `FieldInfo` each time, which
has no value equality — so the hint-equality assertion below would fail for any function
compared with itself. The operation package omits it for the same reason.
"""

import asyncio
import inspect
import shutil
from pathlib import Path
from typing import Annotated, get_type_hints

import pytest
from pydantic import BaseModel, Field

from trails.errors import TrailsError, UsageError
from trails.ops import context
from trails.store import Store


class Where(BaseModel):
    db_path: str


async def probe(label: Annotated[str, Field(description="a label")]) -> Where:
    """Report the store bound for this call."""
    return Where(db_path=f"{label}:{context.store().db_path}")


def test_bind_is_indistinguishable_from_the_operation() -> None:
    """cyclopts and fastmcp read the signature, the annotations and the docstring.

    A wrapper that loses the signature produces a command with no flags; one that loses an
    annotation makes fastmcp raise KeyError at registration; one that loses the docstring
    leaves both surfaces describing the operation as nothing. None of those is visible to a
    test of the operation itself.
    """
    wrapped = context.bind(probe, lambda: None)

    assert inspect.signature(wrapped) == inspect.signature(probe)
    assert get_type_hints(wrapped, include_extras=True) == get_type_hints(
        probe, include_extras=True
    )
    assert wrapped.__doc__ == probe.__doc__


def test_two_concurrent_calls_each_observe_their_own_store(
    initialised_db: Path, tmp_path: Path
) -> None:
    """The binding is task-local, so two calls in one event loop cannot collide.

    A module global, or a binding taken once at server construction, makes one call read the
    other's store. That is not an error — it is a correct-looking answer computed against the
    wrong database. The handshake forces both bodies to be inside their bindings at the same
    time, so a shared binding cannot pass by luck of ordering.
    """
    first_path = tmp_path / "db-first"
    second_path = tmp_path / "db-second"
    shutil.copytree(initialised_db, first_path)
    shutil.copytree(initialised_db, second_path)

    both_bound = asyncio.Event()
    arrived = 0

    async def interleaved(label: Annotated[str, Field(description="a label")]) -> Where:
        """Report the store bound for this call, once both calls are inside their binding."""
        nonlocal arrived
        arrived += 1
        if arrived == 2:
            both_bound.set()
        await both_bound.wait()
        return Where(db_path=f"{label}:{context.store().db_path}")

    async def run() -> list[Where]:
        return list(
            await asyncio.gather(
                context.bind(interleaved, lambda: first_path)(label="first"),
                context.bind(interleaved, lambda: second_path)(label="second"),
            )
        )

    results = asyncio.run(run())

    assert [result.db_path for result in results] == [
        f"first:{first_path}",
        f"second:{second_path}",
    ]


def test_the_token_is_reset_after_a_raising_call_and_after_a_cancelled_call(
    store: Store,
) -> None:
    """A binding left set leaks into the next task on the same event loop.

    The next operation then reads the previous call's store instead of failing, which is a
    wrong answer rather than an error. Both exceptional exits are covered because they leave
    the coroutine by different paths.
    """

    async def raises(label: Annotated[str, Field(description="a label")]) -> Where:
        """Refuse."""
        raise TrailsError("refused")

    async def waits(label: Annotated[str, Field(description="a label")]) -> Where:
        """Never finish."""
        await asyncio.sleep(3600)
        raise AssertionError("unreachable")

    async def run() -> None:
        with pytest.raises(TrailsError):
            await context.bind(raises, lambda: store.db_path)(label="x")
        with pytest.raises(RuntimeError):
            context.store()

        task = asyncio.ensure_future(context.bind(waits, lambda: store.db_path)(label="x"))
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        with pytest.raises(RuntimeError):
            context.store()

    asyncio.run(run())


def test_store_raises_runtime_error_no_binder_can_translate() -> None:
    """An unbound store is a BINDER bug, not something an agent asked for.

    Making it a TrailsError would turn it into an exit code or an `{"error": ...}` payload,
    telling the agent to reason about a store refusal that never happened.
    """
    with pytest.raises(RuntimeError) as raised:
        context.store()

    assert not isinstance(raised.value, TrailsError)


def test_an_operation_that_never_reads_the_store_creates_no_database(tmp_path: Path) -> None:
    """Opening happens on first use, so a database is a consequence of reading one.

    Silent failure, and it shipped: binding an already-open store made `trails version --db
    <fresh path>` run `dolt init` and leave a `.dolt` directory behind — an operation that
    touches no data creating a database, and paying two dolt invocations to report a string.
    Nothing failed and nothing said so.
    """
    fresh = tmp_path / "never-opened"

    async def reports_nothing() -> Where:
        """Report a constant without reading the store."""
        return Where(db_path="unread")

    async def go() -> Where:
        return await context.bind(reports_nothing, lambda: fresh)()

    assert asyncio.run(go()).db_path == "unread"
    assert not fresh.exists()


def test_a_usage_error_surfaces_ahead_of_a_store_that_cannot_be_opened(tmp_path: Path) -> None:
    """The body's own refusal is reported, not the store's.

    Silent failure: an operation refusing an inapplicable combination of flags reported a
    database problem instead, because the open ran before the body. The agent then goes looking
    at its store path when the fix is in its arguments — and on the CLI the two exit at
    different codes, 2 for the usage mistake against 1 for the store, so the wrong one is
    actionably wrong.
    """
    blocked = tmp_path / "not-a-directory"
    blocked.write_text("a file where a database directory would go\n")

    async def refuses() -> Where:
        """Refuse before reading the store."""
        raise UsageError("pass one form or the other, not both")

    async def go() -> Where:
        return await context.bind(refuses, lambda: blocked / "db")()

    with pytest.raises(UsageError) as raised:
        asyncio.run(go())

    assert str(raised.value) == "pass one form or the other, not both"


def test_one_invocation_opens_the_store_once_however_often_the_body_reads_it(
    store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The memo is per invocation, and it is what keeps laziness from costing invocations.

    Silent failure: an unmemoized opener runs `dolt init` on every `store()` call, so an
    operation reading the store three times pays three extra subprocesses — about 70ms each —
    and every result stays correct while the store's whole cost model quietly breaks.
    """
    opened: list[Path | None] = []
    real_open = context.open_store

    def counting_open(path: Path | None) -> Store:
        opened.append(path)
        return real_open(path)

    monkeypatch.setattr(context, "open_store", counting_open)

    async def reads_three_times() -> Where:
        """Read the store three times."""
        first, second, third = context.store(), context.store(), context.store()
        assert first is second is third
        return Where(db_path=str(third.db_path))

    async def go() -> Where:
        return await context.bind(reads_three_times, lambda: store.db_path)()

    assert asyncio.run(go()).db_path == str(store.db_path)
    assert opened == [store.db_path]
