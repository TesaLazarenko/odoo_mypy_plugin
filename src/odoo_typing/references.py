"""Names in field definitions: ``related="a.b.c"``, ``compute="_compute_x"``...

Odoo resolves these strings when it sets up the fields of the registry (``_setup_related``,
``_setup_regular_full``): a field of a related path that does not exist, a path that
goes through a non-relational field, a related field whose class differs from the
target's, or a method name that the model does not have, stop the registry from
loading. They are checked against the field index of the declaring addon's view
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
    "PathResult",
    "field_kind",
    "missing_method_message",
    "resolve_path",
]

ODOO_FIELD_REF = ErrorCode(
    "odoo-field-ref", "Check field and method names in Odoo field definitions", "Odoo"
)

# Field arguments that name a method of the model (``selection`` only as a string).
METHOD_ARGUMENTS: Final = ("compute", "inverse", "search", "selection")
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
    path: str, model: str, fields_of: Callable[[str], Mapping[str, FieldInfo]], argument: str
) -> PathResult:
    """Follow a dotted path of fields (``partner_id.country_id.code``) from a model.

    Args:
        path: The path.
        model: The model its first field belongs to.
        fields_of: The fields of a model (empty if the model is unknown).
        argument: How the message names the path (``related="..."``).

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
            return PathResult(problem=message + _did_you_mean(name, fields))
        if position == len(names) - 1:
            break
        if info.field_type not in RELATIONAL:
            if info.field_type not in KNOWN_CLASSES or info.field_type in REFERENCE:
                return PathResult()  # a custom class, or a reference: model unknown
            return PathResult(
                problem=f'Field "{name}" of Odoo model "{model}" in {argument} is not relational'
            )
        if info.comodel is None or "comodel" in info.dynamic:
            return PathResult()
        model = info.comodel
    return PathResult(field=info, model=model)


def missing_method_message(model: str, argument: str, name: str, methods: Collection[str]) -> str:
    """The message for a method name that the model does not have."""
    message = f'Odoo model "{model}" has no method "{name}" ({argument}="{name}")'
    return message + _did_you_mean(name, methods)


def _did_you_mean(name: str, candidates: Collection[str]) -> str:
    matches = get_close_matches(name, list(candidates), n=1, cutoff=0.6)
    return f'; did you mean "{matches[0]}"?' if matches else ""
