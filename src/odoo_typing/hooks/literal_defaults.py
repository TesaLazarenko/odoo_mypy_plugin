"""``max(dates, default=False)``: the default is ``False``, not any ``bool``.

mypy solves the type variable of ``default`` with ``bool``, so the call gives ``datetime |
bool``, and ``if latest:`` leaves ``datetime | Literal[True]``: every use of the date in
the branch was an error (``format_date(env, latest)``, ``latest > move.date``), though the
only ``bool`` the call can give is the ``False`` it was passed. While the values of the
fields were ``Any`` the call was ``Any``; the types the plugin gives them made it a union.
The result keeps the literal: ``datetime | Literal[False]``, which the test narrows.
"""

from __future__ import annotations

from typing import Final

from mypy.nodes import NameExpr
from mypy.plugin import FunctionContext
from mypy.types import Instance, LiteralType, TupleType, Type, UnionType, get_proper_type

__all__ = ["DEFAULTED_CALLS", "literal_default"]

DEFAULTED_CALLS: Final = frozenset({"builtins.max", "builtins.min"})
BOOL: Final = "builtins.bool"
FALSE: Final = "builtins.False"


def literal_default(ctx: FunctionContext) -> Type:
    """The result of ``max(..., default=False)`` with ``False`` in place of ``bool``.

    Only for a default written ``False`` and items that hold no ``bool`` themselves: of
    ``max(flags, default=False)`` or of items ``float | bool`` the result may be ``True``.
    """
    result = get_proper_type(ctx.default_return_type)
    if not isinstance(result, UnionType) or "default" not in ctx.callee_arg_names:
        return ctx.default_return_type
    index = ctx.callee_arg_names.index("default")
    passed = ctx.args[index]
    if len(passed) != 1 or not isinstance(passed[0], NameExpr) or passed[0].fullname != FALSE:
        return ctx.default_return_type
    others = [typ for at, group in enumerate(ctx.arg_types) if at != index for typ in group]
    if any(_holds_bool(typ) for typ in others):
        return ctx.default_return_type  # the items may be booleans themselves
    items: list[Type] = []
    for item in result.items:
        proper = get_proper_type(item)
        if isinstance(proper, Instance) and proper.type.fullname == BOOL:
            item = LiteralType(False, proper)
        items.append(item)
    return UnionType.make_union(items, result.line, result.column)


def _holds_bool(typ: Type) -> bool:
    """Whether a type is ``bool`` or ``True``, or holds one: in a union, a list, a tuple.

    Not ``Literal[False]``: the items of a field that may be empty.
    """
    proper = get_proper_type(typ)
    if isinstance(proper, LiteralType):
        return proper.value is True
    if isinstance(proper, UnionType | TupleType):
        return any(_holds_bool(item) for item in proper.items)
    if isinstance(proper, Instance):
        return proper.type.fullname == BOOL or any(_holds_bool(item) for item in proper.args)
    return False
