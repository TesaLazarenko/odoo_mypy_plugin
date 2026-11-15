"""Hooks of comparisons: the keys a selection field is compared with."""

from __future__ import annotations

from itertools import pairwise

from mypy.nodes import (
    ComparisonExpr,
    Expression,
    ListExpr,
    MemberExpr,
    SetExpr,
    StrExpr,
    TupleExpr,
)
from mypy.plugin import MethodContext
from mypy.types import Instance, Type

from odoo_typing.classes import receiver_type
from odoo_typing.hooks.models import Hooks
from odoo_typing.hooks.nodes import fail_unfiltered, type_checker
from odoo_typing.vals import ODOO_SELECTION, selection_problem

__all__ = ["COMPARISON_HOOKS", "ComparisonHooks"]

# The methods mypy checks ``a == "x"``, ``a != "x"`` and ``a in ("x", "y")`` with.
COMPARISON_HOOKS = frozenset(
    {"builtins.str.__eq__", "builtins.str.__ne__"}
    | {f"builtins.{name}.__contains__" for name in ("tuple", "list", "set", "frozenset")}
)
EQUALITY = frozenset({"==", "!="})
MEMBERSHIP = frozenset({"in", "not in"})


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
