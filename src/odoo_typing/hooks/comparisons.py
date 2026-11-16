"""Hooks of operators: the keys a selection field is compared with, ``False`` operands."""

from __future__ import annotations

from itertools import pairwise
from typing import Final

from mypy.nodes import (
    ComparisonExpr,
    Expression,
    ListExpr,
    MemberExpr,
    SetExpr,
    StrExpr,
    TupleExpr,
)
from mypy.operators import op_methods
from mypy.plugin import MethodContext
from mypy.subtypes import is_subtype
from mypy.types import (
    AnyType,
    FunctionLike,
    Instance,
    LiteralType,
    Type,
    TypeOfAny,
    get_proper_type,
)

from odoo_typing.classes import receiver_type
from odoo_typing.hooks.models import Hooks
from odoo_typing.hooks.nodes import fail_unfiltered, type_checker
from odoo_typing.vals import ODOO_SELECTION, selection_problem

__all__ = ["COMPARISON_HOOKS", "FALSE_OPERATORS", "ComparisonHooks", "false_operand"]

# The methods mypy checks ``a == "x"``, ``a != "x"`` and ``a in ("x", "y")`` with.
COMPARISON_HOOKS = frozenset(
    {"builtins.str.__eq__", "builtins.str.__ne__"}
    | {f"builtins.{name}.__contains__" for name in ("tuple", "list", "set", "frozenset")}
)
EQUALITY = frozenset({"==", "!="})
MEMBERSHIP = frozenset({"in", "not in"})
# The methods mypy checks an operator of ``False`` with: mypy names the hook of a literal's
# method after the class that defines it, and ``bool`` has most of its operators from ``int``.
FALSE_OPERATORS: Final = frozenset(
    f"builtins.{owner}.{name}" for owner in ("int", "bool") for name in op_methods.values()
)


def false_operand(name: str, ctx: MethodContext) -> Type:
    """An operator that the ``False`` of an empty value does not support gives no type.

    ``abs(picking.scheduled_date - out_date) <= delta``: for the ``False`` of ``datetime |
    Literal[False]`` mypy checks ``int.__sub__``, reports the operand, and goes on with what
    that method returns. ``timedelta | int`` is an ``object`` for ``abs``, and the comparison
    after it was a second error on the line, about a type that no value has. The operand is
    still reported; what the operation gives is not known.
    """
    receiver = get_proper_type(ctx.type)
    if not isinstance(receiver, LiteralType) or receiver.value is not False:
        return ctx.default_return_type
    method = receiver.fallback.type.get_method(name)
    declared = get_proper_type(method.type) if method is not None else None
    given = ctx.arg_types[0][0] if ctx.arg_types and ctx.arg_types[0] else None
    if given is None or not isinstance(declared, FunctionLike):
        return ctx.default_return_type
    taken = [item.arg_types[1] for item in declared.items if len(item.arg_types) > 1]
    if any(is_subtype(given, operand) for operand in taken):
        return ctx.default_return_type
    return AnyType(TypeOfAny.from_error)


class ComparisonHooks(Hooks):
    """Check what a field's value is compared with."""

    def selection_keys(self, ctx: MethodContext) -> Type:
        """``record.state == "draft"``, ``record.state in ("draft", "done")``.

        A literal string that is not a key of the ``Selection`` field never matches: the
        condition is silently always false (or true, for ``!=`` / ``not in``).
        """
        comparison = ctx.context
        if isinstance(comparison, ComparisonExpr):
            operands = comparison.operands
            pairs = pairwise(operands)
            for operator, (left, right) in zip(comparison.operators, pairs, strict=True):
                if operator in EQUALITY:
                    self._check_keys(left, [right], ctx)
                    self._check_keys(right, [left], ctx)
                elif operator in MEMBERSHIP and isinstance(right, ListExpr | TupleExpr | SetExpr):
                    self._check_keys(left, right.items, ctx)
        return ctx.default_return_type

    def _check_keys(self, field: Expression, values: list[Expression], ctx: MethodContext) -> None:
        """Report the literal strings that are not keys of the selection field read."""
        keys = [value for value in values if isinstance(value, StrExpr) and value.value]
        if not keys or not isinstance(field, MemberExpr):
            return
        addon_name = self.registry.addon_name_for_path(ctx.api.path)
        records = receiver_type(type_checker(ctx.api).lookup_type_or_none(field.expr))
        if addon_name is None or not isinstance(records, Instance):
            return
        model = self._models.model_of(records.type)
        if model is None:
            return
        info = self._fields_of(addon_name, ctx.api)(model).get(field.name)
        if info is None or info.field_type != "Selection":
            return
        elsewhere: frozenset[str] | None = None
        for key in keys:
            problem = selection_problem(model, info, key.value)
            if problem is None:
                continue
            if elsewhere is None:  # the keys of addons outside the view: not misspelled
                elsewhere = self._selection_elsewhere(addon_name)(model, field.name)
            if key.value not in elsewhere:
                fail_unfiltered(ctx.api, problem, key, ODOO_SELECTION)
