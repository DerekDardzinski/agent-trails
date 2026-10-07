"""Who the store records as the writer.

ONE resolver for both surfaces. Two existed before — one in the CLI and one in the
MCP server — with differing fallbacks and a `--author` flag beside them, so the field
recorded whatever the caller asserted rather than who ran the process.

`TRAILS_AUTHOR` IS NOT READ, and there is no `--author` flag. An author a caller
can assert is not evidence of anything, and the store's other guarantees are all about
not accepting an assertion where a mechanical fact is available.
"""

from __future__ import annotations

import getpass
import os


def current_author() -> str:
    """The operating system user running this process.

    `$USER` first because it is what a shell sets and what a container usually carries.
    `getpass.getuser()` next, which consults the password database and so answers where
    the environment is empty. `"unknown"` last, because a write must not fail for want
    of a name — the record is worth more than the attribution.
    """
    from_env = os.environ.get("USER")
    if from_env:
        return from_env
    try:
        return getpass.getuser()
    except (KeyError, OSError):
        # getpass consults the environment and then the password database, and raises
        # when neither answers — a container with no passwd entry and no USER. That is
        # a nameless process, not a broken one.
        return "unknown"
