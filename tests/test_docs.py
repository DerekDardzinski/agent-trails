"""The README names every operation and none that was removed."""

import re
from pathlib import Path

from trails import ops

ROOT = Path(__file__).resolve().parent.parent
REMOVED = ("publish", "export", "lineage", "mark_verified", "set_work_status")


def test_the_readme_names_every_operation_and_no_removed_one() -> None:
    """Silent failure caught: a reader told to call an operation that no longer exists, or
    never told about one that does."""
    words = set(re.findall(r"[a-z_]+", (ROOT / "README.md").read_text()))
    assert [name for name in ops.operation_module_names() if name not in words] == []
    assert [name for name in REMOVED if name in words] == []
