"""The CLI surface, derived from the operations at import.

MUST NOT IMPORT FASTMCP, directly or transitively. Measured: `import fastmcp` costs 480ms
against 62ms for `import cyclopts`, and the CLI would pay it on every invocation. A test
asserts it in a subprocess.

There is no per-operation branch here. Everything this module knows about an operation it
reads off the signature.

NOTE: no `from __future__ import annotations`. See `ops/__init__.py`.
"""

import inspect
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Annotated, Any, NoReturn, get_args, get_origin, get_type_hints

import cyclopts
from pydantic import BaseModel, ValidationError as PydanticValidationError
from pydantic.fields import FieldInfo

from trails import ops
from trails.errors import TrailsError, UsageError
from trails.ops import context
from trails.ops._marks import FromFile
from trails.ops.types import RenderedResult

USAGE_EXIT = 2
REFUSAL_EXIT = 1


class _App(cyclopts.App):
    """A cyclopts app whose PARSE failures exit 2 rather than 1.

    cyclopts exits 1 for an unknown command, a bad enum value, a missing argument and a
    violated group validator. On this surface exit 1 means the store refused something
    legitimate to ask for, and every one of those is instead a mistake in the invocation —
    which is what exit 2 says. Collapsing them would make an inapplicable flag read as a
    refusal, which is the one distinction an agent driving the CLI has.

    cyclopts has already printed the message by the time it raises, so nothing is reported
    twice here.
    """

    def __call__(self, tokens: Any = None, **kwargs: Any) -> Any:
        kwargs.setdefault("exit_on_error", False)
        try:
            return super().__call__(tokens, **kwargs)
        except cyclopts.CycloptsError:
            raise SystemExit(USAGE_EXIT) from None


def build_cli() -> cyclopts.App:
    """The cyclopts app with every operation in `ops.operations()` registered.

    Synthesizes, per command: `--db`, `--json`, and `--<name>-from-file` for each parameter marked
    FromFile, paired with its inline form under a Group whose validator makes them mutually
    exclusive. That validator is the ONLY use of a cyclopts validator: a cross-parameter rule
    over operation parameters belongs in the operation body, because a validator runs on this
    surface only and the rule would then exist on one surface and not the other.

    Registers each operation through `ops.context.bind` with a `db_path` callable that reads
    the parsed `--db`, so the binding happens per awaited call.

    Prints the result's `render_text()`, or with the synthesized `--json` flag the key-sorted
    JSON of the model, which is byte-comparable with MCP's structured content.

    Translates: UsageError to stderr and exit 2; any other TrailsError to stderr and exit 1.
    A RuntimeError from an unbound store is a binder bug and is NOT translated.
    """
    # Plain text, because the descriptions contain literal "<...>" and "[...]" that markdown
    # and rich markup would strip from the help.
    app = _App(name="trails", result_action=_emit, help_format="plaintext")
    groups: dict[tuple[str, ...], cyclopts.App] = {}
    for operation in ops.operations():
        path = ops.cli_path(operation)
        parent = app
        prefix: tuple[str, ...] = ()
        for segment in path[:-1]:
            prefix = (*prefix, segment)
            if prefix not in groups:
                groups[prefix] = cyclopts.App(name=segment, help_format="plaintext")
                parent.command(groups[prefix])
            parent = groups[prefix]
        parent.command(_command(operation), name=path[-1])
    return app


def _emit(value: BaseModel | None) -> None:
    """The result action: nothing, because each command writes its own output.

    The command writes rather than this action because only the command knows whether
    `--json` was passed. cyclopts still calls the action with whatever the invocation
    produced, and its own `--help` and `--version` paths produce None after printing.
    """
    del value


def _write(value: BaseModel, *, as_json: bool) -> None:
    """Print a result: its `render_text()` by default, or one key-sorted JSON object.

    The JSON form is byte-comparable with MCP's structured content. That holds ONLY because
    every operation returns a top-level BaseModel: fastmcp wraps an optional or union return
    in a synthetic `{"result": ...}` envelope that this binder does not produce. A model that
    is not a RenderedResult has no text form and prints as JSON either way.
    """
    if isinstance(value, RenderedResult) and not as_json:
        sys.stdout.write(value.render_text() + "\n")
        return
    sys.stdout.write(json.dumps(value.model_dump(mode="json"), indent=2, sort_keys=True) + "\n")


def _command(operation: Any) -> Any:
    """One cyclopts command over one operation, with the transport parameters synthesized."""
    hints = get_type_hints(operation, include_extras=True)
    from_file = tuple(
        name
        for name, annotation in hints.items()
        if name != "return" and any(isinstance(item, FromFile) for item in get_args(annotation)[1:])
    )

    # ONE Group object per pair, shared by the inline parameter and its from-file twin:
    # cyclopts refuses two distinct Group objects carrying the same name.
    exclusive = {name: _exclusive_group(name) for name in from_file}

    parameters = []
    for parameter in inspect.signature(operation).parameters.values():
        annotation = hints[parameter.name]
        extra: list[Any] = []
        entry = _list_entry_model(annotation)
        description = _description(annotation)
        if entry is not None:
            # One JSON object per occurrence of the flag, the same shape the MCP surface and the
            # parameter's description use. cyclopts' own form for a list of models is one
            # positional token per field, which no description states.
            extra.append(
                cyclopts.Parameter(
                    converter=_json_models,
                    n_tokens=1,
                    accepts_keys=False,
                    help=_with_entry_format(description, entry),
                )
            )
        elif _is_scalar_or_list(annotation):
            # Cyclopts cannot infer one token count for `str | list[str]`. Consuming the
            # positional remainder lets one signature expose one id or several on both CLI and
            # MCP, and the converter preserves a scalar when only one token was supplied.
            extra.append(
                cyclopts.Parameter(converter=_scalar_or_list, n_tokens=-1, help=description)
            )
        elif _is_dict(annotation):
            # cyclopts raises CoercionError on a bare dict parameter, so one shared converter
            # parses the token as one JSON object. It applies only to dict-typed parameters.
            extra.append(cyclopts.Parameter(converter=_json_object, help=description))
        elif description:
            # cyclopts reads help from a docstring section or a Parameter, never from a
            # pydantic Field, so the operation's description is carried across here.
            extra.append(cyclopts.Parameter(help=description))
        if parameter.name in from_file:
            extra.append(cyclopts.Parameter(group=exclusive[parameter.name]))
        parameters.append(
            parameter.replace(annotation=Annotated[(annotation, *extra)] if extra else annotation)
        )

    synthesized = [
        inspect.Parameter(
            f"{name}_from_file",
            inspect.Parameter.KEYWORD_ONLY,
            default=None,
            annotation=Annotated[
                Path | None, cyclopts.Parameter(group=exclusive[name], help=_FROM_FILE_HELP)
            ],
        )
        for name in from_file
    ] + [
        inspect.Parameter(
            "json",
            inspect.Parameter.KEYWORD_ONLY,
            default=False,
            annotation=Annotated[bool, cyclopts.Parameter(help=_JSON_HELP)],
        ),
        inspect.Parameter(
            "db",
            inspect.Parameter.KEYWORD_ONLY,
            default=None,
            annotation=Annotated[Path | None, cyclopts.Parameter(help=_DB_HELP)],
        ),
    ]
    signature = inspect.Signature([*parameters, *synthesized], return_annotation=hints["return"])

    async def command(*args: Any, **kwargs: Any) -> None:
        # cyclopts passes POSITIONAL_OR_KEYWORD parameters positionally, so the names come back
        # through the signature rather than from **kwargs alone.
        arguments = dict(signature.bind(*args, **kwargs).arguments)
        db = arguments.pop("db", None)
        as_json = bool(arguments.pop("json", False))
        for name in from_file:
            path = arguments.pop(f"{name}_from_file", None)
            if path is not None:
                arguments[name] = _resolved(path, hints[name])
        try:
            result = await context.bind(operation, lambda: db)(**arguments)
        except UsageError as exc:
            _refuse(f"usage: {exc}", USAGE_EXIT)
        except TrailsError as exc:
            _refuse(f"error: {exc}", REFUSAL_EXIT)
        _write(result, as_json=as_json)

    command.__signature__ = signature  # ty: ignore[unresolved-attribute]
    command.__annotations__ = {
        parameter.name: parameter.annotation for parameter in signature.parameters.values()
    }
    command.__annotations__["return"] = hints["return"]
    command.__doc__ = operation.__doc__
    command.__name__ = ops.operation_name(operation)
    return command


_DB_HELP = "Database path (default $TRAILS_DB)."
_JSON_HELP = "Print the result as JSON instead of text."
_FROM_FILE_HELP = "Read this parameter's value from a file instead of the command line."


def _exclusive_group(name: str) -> cyclopts.Group:
    """The group pairing one inline parameter with its from-file twin.

    Mutual exclusion is genuinely a binder concern: the pair exists only on this surface, so
    the rule cannot be moved into the operation body.
    """
    return cyclopts.Group(name, validator=cyclopts.validators.MutuallyExclusive())


def _is_dict(annotation: Any) -> bool:
    """True when the parameter accepts a JSON object, at any depth.

    Recursive because the shapes are nested: an optional marked parameter is
    `Annotated[dict[str, Any] | None, Field(...), FromFile()]`, so the dict sits inside a union
    inside the Annotated. Reading only the outer level left every optional dict parameter
    without its converter, which made the inline form a CoercionError and the from-file form
    deliver raw text where the operation expected an object.
    """
    if annotation is dict or get_origin(annotation) is dict:
        return True
    return any(_is_dict(argument) for argument in get_args(annotation))


def _description(annotation: Any) -> str | None:
    """The `Field(description=...)` text carried in an `Annotated` parameter, if any."""
    for item in get_args(annotation)[1:] if get_origin(annotation) is Annotated else ():
        if isinstance(item, FieldInfo) and item.description:
            return item.description
    return None


def _is_scalar_or_list(annotation: Any) -> bool:
    """True for `T | list[T]`, looking through an outer Annotated."""
    base = get_args(annotation)[0] if get_origin(annotation) is Annotated else annotation
    arguments = get_args(base)
    for argument in arguments:
        if get_origin(argument) is not list:
            continue
        [item] = get_args(argument) or (None,)
        if item in arguments:
            return True
    return False


def _scalar_or_list(_type: Any, tokens: Sequence[Any]) -> Any:
    """One converted token as a scalar, or several as a list, for `T | list[T]`."""
    values = [token.value for token in tokens]
    return values[0] if len(values) == 1 else values


def _list_entry_model(annotation: Any) -> type[BaseModel] | None:
    """The model a `list[Model]` parameter holds, looking through Annotated and Optional."""
    if get_origin(annotation) is list:
        [item] = get_args(annotation) or (None,)
        return item if isinstance(item, type) and issubclass(item, BaseModel) else None
    for argument in get_args(annotation):
        found = _list_entry_model(argument)
        if found is not None:
            return found
    return None


def _with_entry_format(description: str | None, model: type[BaseModel]) -> str:
    """The description plus the JSON form of one entry, generated from the model's fields."""
    keys = ", ".join(
        f'"{name}"' + ("" if field.is_required() else " (optional)")
        for name, field in model.model_fields.items()
    )
    form = f"Each entry is one JSON object with keys {keys}; repeat the flag for more entries."
    return f"{description} {form}" if description else form


def _json_models(type_: Any, tokens: Sequence[Any]) -> list[BaseModel]:
    """Parse each command-line token as one JSON object of the list's model."""
    model = _list_entry_model(type_)
    if model is None:  # pragma: no cover - registered only for list-of-model parameters
        raise TypeError(f"{type_} is not a list of models")
    entries = []
    for token in tokens:
        try:
            entries.append(model.model_validate(_loaded(token.value)))
        except PydanticValidationError as exc:
            _refuse(f"usage: not a valid {model.__name__}: {exc}", USAGE_EXIT)
    return entries


def _json_object(_type: Any, tokens: Sequence[Any]) -> dict[str, Any]:
    """Parse one command-line token as a JSON object."""
    return _loaded(tokens[-1].value)


def _resolved(path: Path, annotation: Any) -> Any:
    """The file's text, parsed as a JSON object when the target parameter takes one."""
    try:
        text = path.expanduser().read_text()
    except OSError as exc:
        _refuse(f"error: could not read {path}: {exc}", REFUSAL_EXIT)
    return _loaded(text) if _is_dict(annotation) else text


def _loaded(text: str) -> dict[str, Any]:
    try:
        value = json.loads(text)
    except json.JSONDecodeError as exc:
        _refuse(f"usage: not a JSON object: {exc}", USAGE_EXIT)
    if not isinstance(value, dict):
        _refuse("usage: not a JSON object", USAGE_EXIT)
    return value


def _refuse(message: str, code: int) -> NoReturn:
    """Report on stderr and exit, keeping exit 2 for an invocation mistake.

    Exit 1 is the store refusing something legitimate to ask for; exit 2 is a flag that does
    not apply. An agent driving the CLI reads the difference.
    """
    sys.stderr.write(message + "\n")
    raise SystemExit(code)
