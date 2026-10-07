"""The one author resolver, and the order it asks in.

Each fallback is a separate branch that writes a DIFFERENT name into every record it
touches, and a wrong name is not visible from anything the store reports — which is why
the sequence is asserted rather than assumed. Closes advisory issue 3 from the
decomposition review.
"""

from __future__ import annotations

import getpass

import pytest

from trails.author import current_author


def test_the_environment_user_is_preferred(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("USER", "ddardzin")
    assert current_author() == "ddardzin"


def test_getpass_is_reached_when_the_environment_is_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Skipping this fallback would write "unknown" on every host with no `$USER`.

    A container or a cron job commonly has an empty environment and a real password
    entry, so the failure would look like a store that cannot identify anybody.
    """
    monkeypatch.delenv("USER", raising=False)
    monkeypatch.setattr(getpass, "getuser", lambda: "from-passwd")
    assert current_author() == "from-passwd"


def test_a_blank_environment_user_falls_through(monkeypatch: pytest.MonkeyPatch) -> None:
    """`USER=` set to the empty string is not a name.

    Treating it as one would record an author of "", which reads as a missing column
    rather than as an unidentified process.
    """
    monkeypatch.setenv("USER", "")
    monkeypatch.setattr(getpass, "getuser", lambda: "from-passwd")
    assert current_author() == "from-passwd"


def test_both_lookups_failing_yields_unknown(monkeypatch: pytest.MonkeyPatch) -> None:
    """A write must not fail for want of a name: the record is worth more than the
    attribution.

    `getpass.getuser` raises when neither the environment nor the password database
    answers, and an uncaught error here would abort a write that had nothing wrong with
    it.
    """
    monkeypatch.delenv("USER", raising=False)

    def no_such_user() -> str:
        raise KeyError("getpwuid(): uid not found")

    monkeypatch.setattr(getpass, "getuser", no_such_user)
    assert current_author() == "unknown"


def test_trails_author_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    """An author a caller can assert is not evidence of anything.

    The variable used to override this, so a host could record any name it liked and the
    author field silently stopped meaning "who ran this".
    """
    monkeypatch.setenv("TRAILS_AUTHOR", "an-agent-that-did-not-run-this")
    monkeypatch.setenv("USER", "ddardzin")
    assert current_author() == "ddardzin"
