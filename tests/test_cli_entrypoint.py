"""Smoke tests: the console-script entry point is wired, and every path a caller reaches first.

Catches a packaging regression — a broken [project.scripts] target, or an import error in cli.py
— that no other test would surface, because every other test builds the app through
`surfaces.cli.build_cli` rather than going through the module the console script names.

It also covers the paths cyclopts produces rather than the operations: help and version. Those
return NOTHING, and the CLI binder's result action json-dumps whatever an invocation produced, so
they are the one case where the emitter is handed something that is not a response model.
"""

from __future__ import annotations

import pytest

from trails import __version__, cli
from trails.models import RecordDraft
from trails.store import Store


def _run(argv: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, str]:
    code = 0
    try:
        cli.app(argv, exit_on_error=False)
    except SystemExit as exc:
        code = int(exc.code or 0)
    return code, capsys.readouterr().out


def test_version_command_reports_package_version(capsys: pytest.CaptureFixture[str]) -> None:
    """The app is taken from `trails.cli`, which is what `trails` loads.

    Silent failure: `cli.app` stops being built, or is built from something other than the
    operation registry, and every test that calls `build_cli()` itself stays green while the
    installed `trails` cannot run at all.
    """
    code, out = _run(["version"], capsys)

    assert code == 0
    assert __version__ in out


@pytest.mark.parametrize(
    "argv",
    [[], ["--help"], ["search", "--help"], ["write", "goal", "--help"], ["--version"]],
    ids=["no arguments", "root help", "command help", "nested command help", "version flag"],
)
def test_every_help_and_version_path_succeeds(
    argv: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    """The first thing anybody runs must not end in a traceback.

    Silent failure is the wrong word for this one — it was LOUD and nothing looked at it. cyclopts
    prints the help and returns None, and the binder's result action called `model_dump` on it
    unconditionally, so `trails --help`, `trails <command> --help` and a bare `trails` each printed
    their help correctly and THEN exited 1 with an AttributeError. Every functional test stayed
    green because none of them asks for help, and the entry-point test covered only `version`.

    Exit 0 and non-empty stdout together are what the assertion needs: the crash happened after
    the help had already been written, so output alone would have passed.
    """
    code, out = _run(argv, capsys)

    assert code == 0
    assert out.strip() != ""


def test_removed_render_operation_is_absent_from_cli_help(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Catches module discovery continuing to publish a removed human-document operation."""
    code, out = _run(["--help"], capsys)

    assert code == 0
    assert "prime" in out
    assert "render" not in out


def test_cli_show_accepts_one_or_several_positional_ids(
    store: Store, capsys: pytest.CaptureFixture[str]
) -> None:
    """Catches Cyclopts trying to infer one token count for `str | list[str]` and crashing
    before the operation runs."""
    first = store.write_record(
        RecordDraft(work_status="closed", title="First", entry_type="note")
    ).record.id
    second = store.write_record(
        RecordDraft(work_status="closed", title="Second", entry_type="note")
    ).record.id
    db = ["--db", str(store.db_path)]

    one_code, one = _run(["show", first, *db], capsys)
    many_code, many = _run(["show", second, first, "--detail", "full", *db], capsys)

    assert (one_code, many_code) == (0, 0)
    assert f"state: {first} · lifecycle current · work closed · detail summary" in one
    assert many.index(second) < many.index(first)
    assert many.count("Next:") == 1
