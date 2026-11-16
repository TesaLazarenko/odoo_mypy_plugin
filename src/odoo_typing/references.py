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

import re
from collections.abc import Callable, Collection, Iterable, Mapping
from dataclasses import dataclass
from difflib import get_close_matches
from typing import Final

from mypy.errorcodes import ErrorCode

from odoo_typing.scanner import FieldInfo

__all__ = [
    "AGGREGATES",
    "DATE_CLASSES",
    "GRANULARITIES",
    "KNOWN_CLASSES",
    "META_FIELD_ATTRIBUTES",
    "METHOD_ARGUMENTS",
    "ODOO_FIELD_REF",
    "ORDER_METHODS",
    "READ_GROUP_METHODS",
    "RECORD_PATH_METHODS",
    "PathResult",
    "decorator_problem",
    "did_you_mean",
    "field_kind",
    "group_spec",
    "missing_method_message",
    "order_fields",
    "record_paths",
    "resolve_path",
]

ODOO_FIELD_REF = ErrorCode(
    "odoo-field-ref", "Check field and method names in Odoo field definitions", "Odoo"
)

# Field arguments that name a method of the model (``selection`` only as a string).
METHOD_ARGUMENTS: Final = ("compute", "inverse", "search", "selection", "group_expand")
# Methods whose ``order`` argument is an order of the receiver's model.
ORDER_METHODS: Final = frozenset({"search", "search_read", "search_fetch", "_search"})
# Class attributes of a model that name one of its fields (``_rec_names_search``: a list).
META_FIELD_ATTRIBUTES: Final = frozenset(
    {"_rec_name", "_parent_name", "_fold_name", "_active_name", "_rec_names_search"}
)
# ``_read_group`` and the ``read_group`` of Odoo 20 take ``groupby`` and ``aggregates`` specs.
READ_GROUP_METHODS: Final = frozenset({"_read_group", "read_group"})
# As Odoo's ``regex_read_group_spec`` reads a spec.
GROUP_SPEC: Final = re.compile(r"(?P<field>\w+)(\.[\w.]+)?(:(?P<function>\w+))?")
# What may follow a date field in ``groupby`` and any field in ``aggregates``, in any
# supported version (Odoo 19 added the ``*_number`` parts and ``array_agg_distinct``).
GRANULARITIES: Final = frozenset(
    {"hour", "day", "week", "month", "quarter", "year", "year_number", "quarter_number"}
    | {"month_number", "iso_week_number", "day_of_year", "day_of_month", "day_of_week"}
    | {"hour_number"}
)
AGGREGATES: Final = frozenset(
    {"sum", "avg", "max", "min", "bool_and", "bool_or", "array_agg", "array_agg_distinct"}
    | {"recordset", "count", "count_distinct"}
)
# One term of an order, as Odoo's ``regex_order`` reads it (17-20): a field, a property
# or a sub-field after a dot, an aggregate, a direction and where nulls go.
ORDER_TERM: Final = re.compile(
    r"""\s*"?(?P<field>[a-z0-9_]+)"?(\.[a-z0-9_]+)?(:[a-z_]+)?
        (\s+(desc|asc))?(\s+nulls\s(first|last))?\s*""",
    re.IGNORECASE | re.VERBOSE,
)
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


NEAR_MISS: Final = 0.8  # how close a name must be to a field to be taken for a typo of it


@dataclass(frozen=True, slots=True)
class PathResult:
    """What a dotted field path resolves to.

    Attributes:
        field: The last field, if every step resolved.
        model: The model of the last field, or of the step that is not a field.
        problem: Why the path is wrong (a message), if it is.
        missing: The name of the step that is not a field, if that is the problem.
        near: Whether that name is a near miss of a field of its model (a typo).
    """

    field: FieldInfo | None = None
    model: str | None = None
    problem: str | None = None
    missing: str | None = None
    near: bool = False


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
            return PathResult(
                model=model,
                problem=message + did_you_mean(name, fields),
                missing=name,
                near=near_miss(name, fields),
            )
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


def near_miss(name: str, names: Iterable[str]) -> bool:
    """Whether a name is close to one of the names (a typo of it, not another field)."""
    return bool(get_close_matches(name, list(names), n=1, cutoff=NEAR_MISS))


def decorator_problem(
    decorator: str,
    name: str,
    model: str,
    fields_of: Callable[[str], Mapping[str, FieldInfo]],
    ignored: Callable[[str], bool] = lambda _name: False,
) -> str | None:
    """Why an argument of ``@api.depends`` / ``constrains`` / ``onchange`` is wrong, if it is.

    Args:
        decorator: The short name of the decorator (``depends``).
        name: The argument: a path for ``depends``, a field name for the others.
        model: The model of the class whose method is decorated.
        fields_of: The fields of a model (empty if the model is unknown).
        ignored: Whether a name that is no field is not reported (``vals_ignore_keys``),
            unless it is a near miss of a field.
    """
    argument = f'@api.{decorator}("{name}")'
    if decorator != "depends" and "." in name:
        return f"{argument} takes field names, not paths: Odoo ignores it"
    found = resolve_path(name, model, fields_of, argument)
    if found.missing is not None and not found.near and ignored(found.missing):
        return None  # a field made in the database (``vals_ignore_keys``)
    return found.problem


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


@dataclass(frozen=True, slots=True)
class GroupSpec:
    """One ``groupby`` or ``aggregates`` item of ``_read_group``: ``field[.path][:function]``."""

    field: str
    function: str | None


def group_spec(spec: str) -> GroupSpec | None:
    """Read ``"date:month"`` / ``"amount:sum"`` / ``"partner_id"``; ``None`` if it is not one."""
    match = GROUP_SPEC.fullmatch(spec)
    return None if match is None else GroupSpec(match["field"], match["function"])


def order_fields(order: str) -> list[str] | None:
    """The fields an order names (``"date desc, partner_id.name, id"``), first names only.

    Returns:
        ``None`` if the string is not an order (Odoo raises "Invalid order").
    """
    found: list[str] = []
    for term in order.split(","):
        match = ORDER_TERM.fullmatch(term)
        if match is None:
            return None
        found.append(match["field"])
    return found


def did_you_mean(name: str, candidates: Collection[str]) -> str:
    """``; did you mean "x"?`` with the closest candidate, or nothing."""
    matches = get_close_matches(name, list(candidates), n=1, cutoff=0.6)
    return f'; did you mean "{matches[0]}"?' if matches else ""
