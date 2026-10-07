"""Every operation's description says when to use it, what to use instead, and what it returns.

Runs over `ops.operations()`, so an operation added by a later unit is covered without editing
this file. The descriptions are what an agent reads to choose a tool and fill its arguments, so
a missing section is a tool an agent misuses rather than a cosmetic gap.
"""

import inspect
from typing import Any, get_args, get_type_hints

import pytest
from pydantic.fields import FieldInfo

from trails import ops
from trails.ops.types import RenderedResult

SECTIONS = ("Use this when:", "Use instead:", "Returns:")


def _description(annotation: Any) -> str | None:
    for item in get_args(annotation)[1:]:
        if isinstance(item, FieldInfo):
            return item.description
    return None


@pytest.mark.parametrize("operation", ops.operations(), ids=ops.operation_name)
def test_every_operation_documents_when_to_use_it_and_every_parameter(operation: Any) -> None:
    """Catches an operation shipping without one of the three sections, or a parameter whose
    meaning an agent has to guess."""
    doc = inspect.getdoc(operation) or ""
    hints = get_type_hints(operation, include_extras=True)

    assert doc.split("\n", 1)[0].strip(), "no summary line"
    assert [section for section in SECTIONS if section not in doc] == []
    undescribed = [
        name
        for name in inspect.signature(operation).parameters
        if not (_description(hints[name]) or "").strip()
    ]
    assert undescribed == []


@pytest.mark.parametrize("operation", ops.operations(), ids=ops.operation_name)
def test_every_operation_result_renders_its_own_text(operation: Any) -> None:
    """Catches a result type falling back to the default JSON dump, which every surface would
    then print as the agent-facing text."""
    returned = get_type_hints(operation)["return"]

    assert issubclass(returned, RenderedResult)
    assert returned.render_text is not RenderedResult.render_text
