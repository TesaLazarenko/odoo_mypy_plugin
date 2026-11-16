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
from mypy.operators import op_methods, reverse_op_methods
from mypy.plugin import MethodContext
from mypy.subtypes import is_subtype
from mypy.types import (
    AnyType,
    FunctionLike,
    Instance,
    LiteralType,
    NoneType,
    ProperType,
    Type,
    TypeOfAny,
    get_proper_type,
)

from odoo_typing.classes import receiver_type
from odoo_typing.hooks.models import Hooks
from odoo_typing.hooks.nodes import fail_unfiltered, type_checker
from odoo_typing.vals import ODOO_SELECTION, selection_problem

__all__ = [
    "COMPARISON_HOOKS",
    "FALSE_OPERATORS",
    "REVERSED_OPERATORS",
    "ComparisonHooks",
    "empty_operand",
    "false_operand",
]

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


# ``x.__radd__`` and the like: the method mypy checks ``None + x`` with (``None`` has none).
REVERSED_OPERATORS: Final = frozenset(
    name for name in reverse_op_methods.values() if name.startswith("__r")
)


def empty_operand(name: str, ctx: MethodContext) -> Type:
    """A reversed operator that the ``None`` or ``False`` of an empty value does not fit.

    ``fields.Datetime.from_string(order.date_order) + timedelta(days=2)``: Odoo declares
    ``datetime | None``, and for ``None`` mypy checks ``timedelta.__radd__``, reports the
    operand and goes on with ``timedelta``, what that method returns. The variable was
    then ``datetime | timedelta``, and ``abs(min_date - out_date)`` a second error about a
    ``datetime`` no value is. The operand is still reported; the result is not known.
    """
    given = get_proper_type(ctx.arg_types[0][0]) if ctx.arg_types and ctx.arg_types[0] else None
    if not isinstance(given, NoneType) and not (
        isinstance(given, LiteralType) and given.value is False
    ):
        return ctx.default_return_type
    return _unfit(name, get_proper_type(ctx.type), given, ctx)


def _unfit(name: str, receiver: ProperType, given: ProperType, ctx: MethodContext) -> Type:
    """``Any`` if no signature of a method of the receiver takes the operand."""
    info = receiver.type if isinstance(receiver, Instance) else None
    method = info.get_method(name) if info is not None else None
    declared = get_proper_type(method.type) if method is not None else None
    if not isinstance(declared, FunctionLike):
        return ctx.default_return_type
    taken = [item.arg_types[1] for item in declared.items if len(item.arg_types) > 1]
    if any(is_subtype(given, operand) for operand in taken):
        return ctx.default_return_type
    return AnyType(TypeOfAny.from_error)


def false_operand(name: str, ctx: MethodContext) -> Type:
    """An operator that the ``False`` of an empty value does not support gives no type.

    ``abs(picking.scheduled_date - out_date) <= delta``: for the ``False`` of ``datetime |
    Literal[False]`` mypy checks ``int.__sub__``, reports the operand, and goes on with what
    that method returns. ``timedelta | int`` is an ``object`` for ``abs``, and the comparison
    after it was a second error on the line, about a type that no value has. The operand is
    still reported; what the operation gives is not known.
    """
    receiver = get_proper_type(ctx.type)
    given = get_proper_type(ctx.arg_types[0][0]) if ctx.arg_types and ctx.arg_types[0] else None
    if not isinstance(receiver, LiteralType) or receiver.value is not False or given is None:
        return ctx.default_return_type
    return _unfit(name, receiver.fallback, given, ctx)


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
