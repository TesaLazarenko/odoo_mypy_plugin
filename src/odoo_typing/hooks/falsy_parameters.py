"""Calls of Odoo functions that handle their empty arguments (odoo_typing.falsy_guards)."""

from __future__ import annotations

from collections.abc import Callable

from mypy.nodes import Expression
from mypy.plugin import (
    CheckerPluginInterface,
    FunctionContext,
    FunctionSigContext,
    MethodContext,
    MethodSigContext,
)
from mypy.types import (
    CallableType,
    FunctionLike,
    LiteralType,
    NoneType,
    Type,
    UnionType,
    get_proper_type,
)

# isort: split
# After mypy.types: these modules import each other at import time.
from mypy.typeops import make_simplified_union

from odoo_typing.falsy_guards import Guard
from odoo_typing.hooks.nodes import type_checker

__all__ = ["guarded_result", "guarded_signature"]


def guarded_signature(guard: Guard, ctx: FunctionSigContext | MethodSigContext) -> FunctionLike:
    """A parameter the function returns early for when empty takes ``False`` and ``None``.

    ``format_date(env, line.date_maturity)``: Odoo declares ``value: datetime | date |
    str`` and starts with ``if not value: return ''``; ``str2bool(config.value)`` starts
    with ``if type(s) is bool: return s``.
    """
    signature = ctx.default_signature
    if not isinstance(signature, CallableType):
        return signature
    checker = type_checker(ctx.api)
    boolean = checker.named_type("builtins.bool")
    empty: list[Type] = [LiteralType(False, boolean), NoneType()]
    arg_types = [
        UnionType.make_union([typ, *empty])
        if name in guard.falsy
        else UnionType.make_union([typ, boolean])
        if name in guard.booleans
        else typ
        for name, typ in zip(signature.arg_names, signature.arg_types, strict=True)
    ]
    return signature.copy_modified(arg_types=arg_types)


def guarded_result(
    guard: Guard,
    proven: Callable[[Expression, CheckerPluginInterface], bool],
    ctx: FunctionContext | MethodContext,
) -> Type:
    """No ``None`` / ``False`` from a function that gives it only for an empty argument.

    ``fields.Date.to_date(value)`` (also ``from_string``) returns ``None`` only from
    ``if not value: return None``: for an argument that cannot be false, or a read of the
    context the function has found true (``if self.env.context.get("k"):``, then
    ``from_string(self.env.context["k"])``), the result is a ``date``.
    """
    for index, name in enumerate(ctx.callee_arg_names):
        if name not in guard.none_only_for or index >= len(ctx.args):
            continue
        given = list(zip(ctx.args[index], ctx.arg_types[index], strict=True))
        if given and all(
            not get_proper_type(typ).can_be_false or proven(node, ctx.api) for node, typ in given
        ):
            return _without_empty(ctx.default_return_type)
    return ctx.default_return_type


def _without_empty(found: Type) -> Type:
    proper = get_proper_type(found)
    if not isinstance(proper, UnionType):
        return found
    kept = [
        item
        for item in proper.items
        if not isinstance(get_proper_type(item), NoneType)
        and not (
            isinstance(literal := get_proper_type(item), LiteralType) and literal.value is False
        )
    ]
    return make_simplified_union(kept) if kept else found
