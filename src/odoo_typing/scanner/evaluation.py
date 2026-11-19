"""Values of constant expressions, as far as a static scan can know them.

What model names, ``_inherit`` lists, ``_inherits`` dicts and field arguments are made
of: literals, names bound earlier in the module or the class body, ``+`` and f-strings.
"""

from __future__ import annotations

import ast
from collections import ChainMap
from collections.abc import Mapping, MutableMapping
from typing import Final

__all__ = ["UNKNOWN", "assign", "class_attributes", "evaluate"]

UNKNOWN: Final = object()  # value of an expression the scan cannot evaluate


def evaluate(node: ast.expr, scope: Mapping[str, object]) -> object:
    """Value of a constant expression, or :data:`UNKNOWN`.

    Handles literals, lists, tuples and dicts, names bound earlier, ``+`` of strings,
    lists or tuples, ``*`` of a known list, tuple or dict view in a display, and f-strings
    of known values: what model names, ``_inherit`` lists and ``_inherits`` dicts are
    made of.
    """
    if isinstance(node, ast.Constant):
        return node.value
    if isinstance(node, ast.Dict):
        return _evaluate_dict(node, scope)
    if isinstance(node, ast.Name):
        return scope.get(node.id, UNKNOWN)
    if isinstance(node, ast.List | ast.Tuple):
        items: list[object] = []
        for item in node.elts:
            if not isinstance(item, ast.Starred):
                items.append(evaluate(item, scope))
                continue
            spliced = _spliced(item.value, scope)  # ``*CLOSED_STATES.items()``
            # One unknown item, as before: ``_inherit = [*IMPORTED, "mail.thread"]`` keeps
            # the names the scan knows.
            items.extend([UNKNOWN] if spliced is None else spliced)
        return items if isinstance(node, ast.List) else tuple(items)
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _concatenate(evaluate(node.left, scope), evaluate(node.right, scope))
    if isinstance(node, ast.JoinedStr):
        parts = [_format_part(part, scope) for part in node.values]
        strings = [part for part in parts if isinstance(part, str)]
        return "".join(strings) if len(strings) == len(parts) else UNKNOWN
    return UNKNOWN


def _spliced(node: ast.expr, scope: Mapping[str, object]) -> list[object] | None:
    """The items ``*value`` puts in a display: of a list or tuple, of a dict's view.

    ``*CLOSED_STATES.items()`` in ``fields.Selection([...])`` (``project.task.state``),
    for a dict the scan knows; ``None`` for anything else.
    """
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and not node.args
        and not node.keywords
        and node.func.attr in {"items", "keys", "values"}
    ):
        known = evaluate(node.func.value, scope)
        if not isinstance(known, dict) or UNKNOWN in known.values():
            return None
        views = {"items": list(known.items()), "keys": list(known), "values": list(known.values())}
        return list(views[node.func.attr])
    value = evaluate(node, scope)
    return list(value) if isinstance(value, list | tuple) else None


def _concatenate(left: object, right: object) -> object:
    """``left + right`` of two strings, lists or tuples (``STATES + [("x", "X")]``)."""
    if isinstance(left, str) and isinstance(right, str):
        return left + right
    if isinstance(left, list) and isinstance(right, list):
        return left + right
    if isinstance(left, tuple) and isinstance(right, tuple):
        return left + right
    return UNKNOWN


def _evaluate_dict(node: ast.Dict, scope: Mapping[str, object]) -> object:
    """Value of a dict display whose keys are all known (``**`` unpacking is not)."""
    keys = [evaluate(key, scope) if key is not None else UNKNOWN for key in node.keys]
    if any(key is UNKNOWN or not isinstance(key, str) for key in keys):
        return UNKNOWN
    return {key: evaluate(value, scope) for key, value in zip(keys, node.values, strict=True)}


def _format_part(node: ast.expr, scope: Mapping[str, object]) -> object:
    if isinstance(node, ast.FormattedValue):
        if node.conversion != -1 or node.format_spec is not None:
            return UNKNOWN
        value = evaluate(node.value, scope)
        return value if isinstance(value, str) else UNKNOWN
    return evaluate(node, scope)


def assign(
    statement: ast.Assign | ast.AnnAssign | ast.AugAssign, scope: MutableMapping[str, object]
) -> None:
    """Bind the names assigned by a statement (also ``a = b = value``) in a namespace."""
    if isinstance(statement, ast.Assign):
        targets, value = statement.targets, evaluate(statement.value, scope)
    elif isinstance(statement, ast.AnnAssign) and statement.value is not None:
        targets, value = [statement.target], evaluate(statement.value, scope)
    else:  # augmented, or annotation only
        targets, value = [statement.target], UNKNOWN
    for target in targets:
        names = target.elts if isinstance(target, ast.Tuple | ast.List) else [target]
        for name in names:
            if isinstance(name, ast.Name):
                scope[name.id] = value if name is target else UNKNOWN


def class_attributes(node: ast.ClassDef, module_names: dict[str, object]) -> dict[str, object]:
    """Values of the names a class body assigns, evaluated in order.

    Like Python, the body sees its own earlier assignments first, then the module's
    names: ``_inherit = _name`` and ``_name = PREFIX + ".x"`` work.
    """
    attrs: dict[str, object] = {}
    scope = ChainMap(attrs, module_names)  # assignments go to the first map
    for statement in node.body:
        if isinstance(statement, ast.Assign | ast.AnnAssign | ast.AugAssign):
            assign(statement, scope)
    return attrs
