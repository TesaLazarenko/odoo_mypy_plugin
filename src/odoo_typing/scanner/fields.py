"""The field definitions of a class body: ``name = <field class>(...)`` and its arguments.

Which calls are field classes is the caller's answer
(:mod:`odoo_typing.scanner.field_classes`): ``fields.Char``, a class imported from
``odoo.fields``, a subclass that an addon defines.
"""

from __future__ import annotations

import ast
import sys
from collections.abc import Callable, Mapping
from typing import Final

from odoo_typing.frozen import frozen
from odoo_typing.scanner.evaluation import UNKNOWN, evaluate
from odoo_typing.scanner.field_classes import POSITIONAL, FieldKind
from odoo_typing.scanner.results import FieldInfo
from odoo_typing.scanner.syntax import single_target

__all__ = ["POSITIONAL_PARAMETERS", "FieldClassOf", "declared_fields", "unread_positional"]

# The field class a call in a class body makes a field of: its kind, the dotted name of a
# class of another addon (the registry links it), or ``None`` if the callee is no field class.
FieldClassOf = Callable[[ast.Call], FieldKind | str | None]

# Positional parameters of the field classes (Odoo 18-20); ``string`` for the others.
POSITIONAL_PARAMETERS: Final = {
    "Many2one": ("comodel_name", "string"),
    "One2many": ("comodel_name", "inverse_name", "string"),
    "Many2many": ("comodel_name", "relation", "column1", "column2", "string"),
    "Selection": ("selection", "string"),
    "Reference": ("selection", "string"),
    "Float": ("string", "digits"),
    "Monetary": ("string", "currency_field"),
}
# The ``FieldInfo`` attributes that positional parameters give.
POSITIONAL_ATTRIBUTES: Final = {
    "comodel_name": "comodel",
    "inverse_name": "inverse_name",
    "selection": "selection",
}


def unread_positional(field_type: str) -> frozenset[str]:
    """The attributes that the positional arguments of a field class may give.

    What ``FieldInfo.dynamic`` names for a call whose positional arguments cannot be
    read (the class has an ``__init__`` of its own).
    """
    parameters = POSITIONAL_PARAMETERS.get(field_type, ())
    attributes = (POSITIONAL_ATTRIBUTES.get(name) for name in parameters)
    return frozen(name for name in attributes if name is not None)


def declared_fields(
    node: ast.ClassDef,
    scope: Mapping[str, object],
    methods: frozenset[str],
    class_of: FieldClassOf,
) -> tuple[FieldInfo, ...]:
    """The fields a class body assigns: calls of field classes (also annotated).

    A name assigned twice keeps its last definition, as the class namespace does.
    """
    found: dict[str, FieldInfo] = {}
    for statement in node.body:
        target = single_target(statement)
        value = statement.value if isinstance(statement, ast.Assign | ast.AnnAssign) else None
        if target is None or not isinstance(value, ast.Call):
            continue
        kind = class_of(value)
        if kind is not None:
            found.pop(target, None)
            found[target] = _field_info(target, value, kind, scope, methods)
    return tuple(found.values())


def _field_info(
    name: str,
    call: ast.Call,
    kind: FieldKind | str,
    scope: Mapping[str, object],
    methods: frozenset[str],
) -> FieldInfo:
    """What one call of a field class passes, as far as the scan can evaluate it."""
    dynamic: set[str] = set()
    if isinstance(kind, str):  # a class of another addon: linked by the registry
        field_type, arguments = kind, _field_arguments(None, call)
        if call.args:
            dynamic.add(POSITIONAL)
    elif kind.arguments:  # an ``__init__`` of its own
        field_type, arguments = kind.field_type, _field_arguments(None, call)
        if call.args:
            dynamic.update(unread_positional(kind.base))
    else:
        field_type, arguments = kind.field_type, _field_arguments(kind.base, call)

    def text(key: str) -> str | None:
        return _text_argument(arguments, key, scope, dynamic)

    def method(key: str) -> str | None:
        if key not in arguments:
            return None
        node = arguments[key]
        if isinstance(node, ast.Name) and node.id in methods:
            return node.id
        return _text_argument(arguments, key, scope, dynamic)

    def flag(key: str) -> bool | None:
        if key not in arguments:
            return None
        value = evaluate(arguments[key], scope)
        if isinstance(value, bool):
            return value
        dynamic.add(key)
        return None

    selection, selection_method = _selection(arguments, scope, methods, dynamic)
    if "comodel_name" in dynamic:  # attribute names, not argument names
        dynamic.discard("comodel_name")
        dynamic.add("comodel")
    return FieldInfo(
        name=name,
        field_type=field_type,
        comodel=_interned(text("comodel_name")),
        inverse_name=_interned(text("inverse_name")),
        selection=selection,
        selection_method=selection_method,
        selection_add=_selection_keys(arguments, "selection_add", scope, dynamic),
        related=_interned(text("related")),
        compute=_interned(method("compute")),
        inverse=_interned(method("inverse")),
        search=_interned(method("search")),
        readonly=flag("readonly"),
        required=flag("required"),
        store=flag("store"),
        delegate=flag("delegate"),
        compute_sql=True if "compute_sql" in arguments else None,
        attachment=flag("attachment"),
        dynamic=frozen(dynamic),
    )


def _interned(text: str | None) -> str | None:
    """One object per text: model names and paths repeat in every addon's fields."""
    return sys.intern(text) if text is not None else None


def _field_arguments(field_type: str | None, call: ast.Call) -> dict[str, ast.expr]:
    """Arguments of a field call by parameter name (positional ones up to a ``*args``).

    Args:
        field_type: The Odoo class whose positional parameters the call has; ``None`` if
            they are not known (only the keyword arguments are read).
        call: The call.
    """
    arguments: dict[str, ast.expr] = {}
    parameters = POSITIONAL_PARAMETERS.get(field_type, ("string",)) if field_type else ()
    for parameter, node in zip(parameters, call.args, strict=False):
        if isinstance(node, ast.Starred):
            break
        arguments[parameter] = node
    for keyword in call.keywords:
        if keyword.arg is not None:  # ``**kwargs`` cannot be read
            arguments[keyword.arg] = keyword.value
    return arguments


def _text_argument(
    arguments: Mapping[str, ast.expr], key: str, scope: Mapping[str, object], dynamic: set[str]
) -> str | None:
    if key not in arguments:
        return None
    value = evaluate(arguments[key], scope)
    if isinstance(value, str):
        return value
    dynamic.add(key)
    return None


def _selection(
    arguments: Mapping[str, ast.expr],
    scope: Mapping[str, object],
    methods: frozenset[str],
    dynamic: set[str],
) -> tuple[tuple[str, ...] | None, str | None]:
    """``selection`` as (keys of a list, name of a method); both ``None`` if not given."""
    node = arguments.get("selection")
    if node is None:
        return None, None
    if isinstance(node, ast.Name) and node.id in methods:
        return None, node.id
    value = evaluate(node, scope)
    if isinstance(value, str):
        return None, value
    return _selection_keys(arguments, "selection", scope, dynamic), None


def _selection_keys(
    arguments: Mapping[str, ast.expr], key: str, scope: Mapping[str, object], dynamic: set[str]
) -> tuple[str, ...] | None:
    """Keys of a list of ``(key, label)`` pairs (``selection_add`` also allows ``(key,)``)."""
    if key not in arguments:
        return None
    value = evaluate(arguments[key], scope)
    if isinstance(value, list | tuple):
        keys = [pair[0] if isinstance(pair, list | tuple) and pair else UNKNOWN for pair in value]
        if all(isinstance(item, str) for item in keys):
            return tuple(item for item in keys if isinstance(item, str))
    dynamic.add(key)
    return None
