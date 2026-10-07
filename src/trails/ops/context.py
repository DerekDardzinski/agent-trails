"""The store bound for one invocation, and the binder that binds it.

An operation calls `store()` instead of taking a store parameter, which is what keeps the
store out of both the MCP schema and the CLI flag set with no per-parameter exclusion.

WHAT IS BOUND IS AN OPENER, NOT A STORE, and the difference is observable twice. An operation
that never calls `store()` no longer creates a database as a side effect — `trails version`
against a fresh path used to leave a `.dolt` directory behind. And a failure to OPEN the
database now reaches the operation body, so an operation with a reason to degrade can catch it:
`prime` is a startup hook and reports an unopenable store as data rather than failing, which it
could not do while the open happened outside its frame.

NOTE: no `from __future__ import annotations` in this package. See `ops/__init__.py`.
"""

from collections.abc import Callable
from contextvars import ContextVar
from functools import cache, wraps
from pathlib import Path

from trails.ops.types import OpFn
from trails.store import Store, open_store, resolve_db_path

_store: ContextVar[Callable[[], Store]] = ContextVar("trails_store")
_db_path: ContextVar[Callable[[], Path | None]] = ContextVar("trails_db_path")


def store() -> Store:
    """The store bound for the current invocation, opened on first use.

    Every call within one invocation returns the SAME store: opening is memoized per
    invocation, because `open_store` runs `dolt init` and every dolt invocation costs about
    70ms. The memo is never global — two operations running against two `--db` values in one
    process each have their own.

    Raises:
        RuntimeError: no binder bound a store. A programming error in a binder, not a user
            error, so it is NOT a TrailsError and must not be translated into an exit
            code or an `{"error": ...}` payload.
        StoreUnavailableError: the database cannot be opened. Raised HERE rather than before
            the operation body, so an operation may catch it.
    """
    try:
        opener = _store.get()
    except LookupError as exc:
        raise RuntimeError(
            "no store is bound for this invocation; an operation was called outside a "
            "binder, which is a bug in the binder rather than a mistake by the caller"
        ) from exc
    return opener()


def db_path() -> Path:
    """The database path bound for the current invocation, resolved WITHOUT opening it.

    For an operation that reports which store it would talk to and must not create one.

    Raises:
        RuntimeError: no binder bound a store, as for `store()`.
    """
    try:
        given = _db_path.get()
    except LookupError as exc:
        raise RuntimeError("no store is bound for this invocation") from exc
    return resolve_db_path(given())


def bind[**P, R](fn: OpFn[P, R], db_path: Callable[[], Path | None]) -> OpFn[P, R]:
    """Wrap an operation so a store is bound for the duration of each awaited call.

    THE RETURNED CALLABLE IS INDISTINGUISHABLE FROM `fn` TO BOTH LIBRARIES: the signature,
    the type hints including extras, and the docstring all compare equal. cyclopts builds
    its parameters from `inspect.signature`; fastmcp reads `__annotations__` and raises
    KeyError on a missing entry; both use the docstring as the command or tool description.
    `functools.wraps` copies all three.

    WHAT IS BOUND IS AN OPENER THAT OPENS ON FIRST USE, one per awaited call. The database is
    opened when the body first calls `store()` and not before, so an operation that never
    reads the store creates nothing, and a failure to open reaches the body where the
    operation can decide what to do about it. Binding an already-open store made both of
    those impossible: the open happened outside every operation's frame.

    THE BINDING HAPPENS INSIDE THE COROUTINE, NOT AROUND ITS CREATION. A synchronous `with`
    around `fn(**kwargs)` resets the ContextVar before the coroutine body ever runs, and
    `store()` then raises. Resetting the EXACT token in `finally` is what stops an exception
    or a task cancellation leaving a binding visible to the next task on the same event
    loop.

    A ContextVar set inside a coroutine is task-local, which is what makes concurrent tool
    calls isolated from each other. Binding once at server construction would be WRONG: two
    servers constructed in one event-loop context would both observe the later binding.

    `db_path` is a callable rather than a value because the CLI binder cannot know `--db`
    until the command is parsed.
    """

    @wraps(fn)
    async def called(*args: P.args, **kwargs: P.kwargs) -> R:
        token = _store.set(_opener(db_path))
        path_token = _db_path.set(db_path)
        try:
            return await fn(*args, **kwargs)
        finally:
            _db_path.reset(path_token)
            _store.reset(token)

    return called


def _opener(db_path: Callable[[], Path | None]) -> Callable[[], Store]:
    """One opener for one invocation: opens on first call, returns the same store after.

    A FRESH memo per invocation, which is the whole reason this is a function rather than a
    decorated module-level helper. A shared memo would hand one invocation the store another
    opened against a different `--db`, which is a correct-looking answer computed against the
    wrong database.
    """
    return cache(lambda: open_store(db_path()))
