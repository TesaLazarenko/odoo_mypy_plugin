"""Names in field definitions and in ``@api.depends`` / ``constrains`` / ``onchange``.

Odoo resolves these strings when it sets up the fields of the registry (``_setup_related``,
``_setup_regular_full``, ``resolve_depends``): a field of a related or depends path that
does not exist, a path that goes through a non-relational field, a related field whose
class differs from the target's, or a method name that the model does not have, stop the
registry from loading. ``constrains`` and ``onchange`` take field names only: Odoo logs a
warning for any other name and never calls the method for it. They are checked against
the field index of the declaring addon's view
(:meth:`odoo_typing.registry.Registry.fields` / ``methods``).

This module has the pure parts: resolving a path and the messages.
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass
from difflib import get_close_matches
from typing import Final

from mypy.errorcodes import ErrorCode

from odoo_typing.scanner import FieldInfo

__all__ = [
    "KNOWN_CLASSES",
    "METHOD_ARGUMENTS",
    "ODOO_FIELD_REF",
    "RECORD_PATH_METHODS",
    "PathResult",
    "decorator_problem",
    "did_you_mean",
    "field_kind",
    "missing_method_message",
    "record_paths",
    "resolve_path",
]

ODOO_FIELD_REF = ErrorCode(
    "odoo-field-ref", "Check field and method names in Odoo field definitions", "Odoo"
)

# Field arguments that name a method of the model (``selection`` only as a string).
METHOD_ARGUMENTS: Final = ("compute", "inverse", "search", "selection")
# Recordset methods that take a field path (or name) as a string.
RECORD_PATH_METHODS: Final = frozenset({"mapped", "filtered", "sorted", "grouped"})
RELATIONAL: Final = frozenset({"Many2one", "One2many", "Many2many"})
REFERENCE: Final = frozenset({"Reference", "Many2oneReference"})
# Odoo's own field classes (18-20); a custom subclass is not compared by class.
KNOWN_CLASSES: Final = (
    RELATIONAL
    | REFERENCE
    | {
        "Boolean",
        "Integer",
        "Float",
        "Monetary",
        "Char",
        "Text",
        "Html",
        "Selection",
        "Date",
        "Datetime",
        "Binary",
        "Image",
        "Json",
        "Properties",
        "PropertiesDefinition",
        "Id",
    }
)
# Steps a path does not go through by comodel: the model is not known statically
# (a reference), or the next name is a property (``properties.key``).
OPAQUE: Final = REFERENCE | {"Properties"}
DATE_CLASSES: Final = frozenset({"Date", "Datetime"})
# Field classes that share their ``type`` with a parent class (``Image`` is ``binary``).
SAME_KIND: Final = {"Image": "Binary"}


def field_kind(field_type: str) -> str:
    """The Odoo field ``type`` a field class has, by class name."""
    return SAME_KIND.get(field_type, field_type)


@dataclass(frozen=True, slots=True)
class PathResult:
    """What a dotted field path resolves to.

    Attributes:
        field: The last field, if every step resolved.
        model: The model of the last field.
        problem: Why the path is wrong (a message), if it is.
    """

    field: FieldInfo | None = None
    model: str | None = None
    problem: str | None = None


def resolve_path(
    path: str,
    model: str,
    fields_of: Callable[[str], Mapping[str, FieldInfo]],
    argument: str,
    date_parts: Collection[str] = (),
) -> PathResult:
    """Follow a dotted path of fields (``partner_id.country_id.code``) from a model.

    Args:
        path: The path.
        model: The model its first field belongs to.
        fields_of: The fields of a model (empty if the model is unknown).
        argument: How the message names the path (``related="..."``).
        date_parts: Names that may follow a date field as the last step (a domain's
            ``date.year_number``).

    Returns:
        The last field; a problem; or neither, when a step cannot be known (a comodel
        the scan does not know, a model without fields).
    """
    names = path.split(".")
    info: FieldInfo | None = None
    for position, name in enumerate(names):
        fields = fields_of(model)
        if not fields:
            return PathResult()
        info = fields.get(name)
        if info is None:
            message = f'Odoo model "{model}" has no field "{name}" in {argument}'
            return PathResult(problem=message + did_you_mean(name, fields))
        if position == len(names) - 1:
            break
        if date_parts and info.field_type in DATE_CLASSES and position == len(names) - 2:
            return _date_part(names[-1], name, model, argument, date_parts)
        if info.field_type not in RELATIONAL:
            if info.field_type not in KNOWN_CLASSES or info.field_type in OPAQUE:
                return PathResult()  # a custom class, a reference: model unknown
            return PathResult(
                problem=f'Field "{name}" of Odoo model "{model}" in {argument} is not relational'
            )
        if info.comodel is None or "comodel" in info.dynamic:
            return PathResult()
        model = info.comodel
    return PathResult(field=info, model=model)


def _date_part(
    part: str, name: str, model: str, argument: str, date_parts: Collection[str]
) -> PathResult:
    if part in date_parts:
        return PathResult()
    message = (
        f'"{part}" of date field "{name}" of Odoo model "{model}" in {argument} is not a date part'
    )
    return PathResult(problem=message + did_you_mean(part, date_parts))


def missing_method_message(model: str, argument: str, name: str, methods: Collection[str]) -> str:
    """The message for a method name that the model does not have."""
    message = f'Odoo model "{model}" has no method "{name}" ({argument}="{name}")'
    return message + did_you_mean(name, methods)


def decorator_problem(
    decorator: str, name: str, model: str, fields_of: Callable[[str], Mapping[str, FieldInfo]]
) -> str | None:
    """Why an argument of ``@api.depends`` / ``constrains`` / ``onchange`` is wrong, if it is.

    Args:
        decorator: The short name of the decorator (``depends``).
        name: The argument: a path for ``depends``, a field name for the others.
        model: The model of the class whose method is decorated.
        fields_of: The fields of a model (empty if the model is unknown).
    """
    argument = f'@api.{decorator}("{name}")'
    if decorator != "depends" and "." in name:
        return f"{argument} takes field names, not paths: Odoo ignores it"
    return resolve_path(name, model, fields_of, argument).problem


def record_paths(method: str, argument: str, sorted_order: bool) -> tuple[list[str], str | None]:
    """The field paths a string argument of ``mapped`` / ``filtered``... names.

    ``mapped`` and ``filtered`` take a dotted path; ``grouped`` (and ``sorted`` before
    Odoo 19) reads ``record[name]``, which takes one field name; ``sorted`` in 19+ takes
    an order (``"partner_id.name desc, id"``), whose field parts are returned.

    Returns:
        The paths, and a problem if the argument is not of the form the method takes.
    """
    if method in {"mapped", "filtered"}:
        return [argument], None
    if method == "sorted" and sorted_order:
        parts = [part.split() for part in argument.split(",")]
        return [part[0].split(".")[0] for part in parts if part], None
    if "." in argument or " " in argument.strip():
        return [], f'{method}("{argument}") takes a field name'
    return [argument.strip()], None


def did_you_mean(name: str, candidates: Collection[str]) -> str:
    """``; did you mean "x"?`` with the closest candidate, or nothing."""
    matches = get_close_matches(name, list(candidates), n=1, cutoff=0.6)
    return f'; did you mean "{matches[0]}"?' if matches else ""
