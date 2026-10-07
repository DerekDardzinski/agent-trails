"""Error vocabulary for trails.

Flat hierarchy under one root so a consumer can catch everything this package raises with a
single clause.

The store refuses exactly two things, both raised as ValidationError: malformed input, and an
attempt to overwrite a record. Everything else is recorded as given.
"""

from __future__ import annotations


class TrailsError(Exception):
    """Base class for every error raised by trails."""


class StoreUnavailableError(TrailsError):
    """The `dolt` binary is missing, or the database cannot be opened."""


class ValidationError(TrailsError):
    """The input cannot be recorded: it is malformed, or it would overwrite a record."""


class RecordNotFoundError(TrailsError):
    """An id names no record."""


class UsageError(TrailsError):
    """A mistake in the INVOCATION rather than a refusal by the store.

    Exists so a binder can exit 2 where it exits 1 for a refusal: exit 1 is the store
    refusing something, and exit 2 is a flag that does not apply.
    """


class DoltError(TrailsError):
    """A SQL statement failed.

    Neither "store unavailable" nor a caller mistake. It exists so a failed write cannot be
    silently swallowed or misreported as one of the conditions above.
    """
