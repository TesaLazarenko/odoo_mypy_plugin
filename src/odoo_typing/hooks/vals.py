"""Hooks that check values written to records: ``create`` / ``write`` vals, assignments.

The values themselves are checked by :mod:`odoo_typing.vals`.
"""

from __future__ import annotations

from functools import partial

from mypy.nodes import CallExpr, MemberExpr, NameExpr, TempNode
from mypy.plugin import CheckerPluginInterface, MethodContext, MethodSigContext
from mypy.types import FunctionLike, Instance, Type, get_proper_type

from odoo_typing.hooks.models import Hooks
from odoo_typing.hooks.nodes import type_checker
from odoo_typing.methods import is_self_method, keep_receiver
from odoo_typing.vals import ValsChecker, ValsTarget, context_defaults, vals_dicts

__all__ = ["ValsHooks"]


class ValsHooks(Hooks):
    """Check literal ``vals`` dicts and the values assigned to fields."""

    def check(self, method: str, ctx: MethodContext) -> Type:
        """Check the literal ``vals`` of ``create`` / ``write`` / ``copy(default=)``."""
        base_model = self._profile.base_model
        receiver = get_proper_type(ctx.type)
        argument = ctx.args[0][0] if ctx.args and ctx.args[0] else None
        addon_name = self.registry.addon_name_for_path(ctx.api.path)
        model = self._models.model_of(receiver.type) if isinstance(receiver, Instance) else None
        local = self._local_values(ctx.api)
        if argument is not None and addon_name is not None and model is not None:
            target = self._vals_target(model, addon_name, ctx.api)
            checker = self._vals_checker(ctx.api, addon_name)
            checker.check_values_model(model, method, argument, method == "create")
            for vals in vals_dicts(argument, method == "create", local, typed=checker.typed):
                checker.check(target, vals)
        call = ctx.context
        if (
            method == "create"
            and addon_name is not None
            and model is not None
            and isinstance(call, CallExpr)
            and isinstance(call.callee, MemberExpr)
        ):
            receiver_node = call.callee.expr  # x.with_context(default_y=...).create
            if isinstance(receiver_node, NameExpr) and local is not None:
                built = local.built(receiver_node, receiver_node.line)  # orders = x.with_context
                receiver_node = built.value if built.value is not None else receiver_node
            defaults = context_defaults(receiver_node, local)
            if defaults:
                target = self._vals_target(model, addon_name, ctx.api)
                self._vals_checker(ctx.api, addon_name).check_defaults(target, defaults)
        if is_self_method(method):
            return keep_receiver(base_model, ctx)
        return ctx.default_return_type

    def field_set(self, ctx: MethodSigContext) -> FunctionLike:
        """Check the value assigned to a field (``record.partner_id = value``).

        mypy types ``record.name = value`` as a call of the descriptor's ``__set__``;
        the signature is kept (Odoo leaves the value unannotated) and the value is
        checked like a ``vals`` value of that field (:mod:`odoo_typing.vals`).
        """
        lvalue = ctx.context
        records = ctx.args[0][0] if len(ctx.args) == 2 and ctx.args[0] else None
        value = ctx.args[1][0] if records is not None and ctx.args[1] else None
        if not isinstance(lvalue, MemberExpr) or not isinstance(records, TempNode):
            return ctx.default_signature
        record = get_proper_type(records.type)
        model = self._models.model_of(record.type) if isinstance(record, Instance) else None
        addon_name = self.registry.addon_name_for_path(ctx.api.path)
        if value is None or model is None or addon_name is None:
            return ctx.default_signature
        info = self._fields_of(addon_name, ctx.api)(model).get(lvalue.name)
        if info is None:
            return ctx.default_signature
        checker = type_checker(ctx.api)
        with checker.msg.filter_errors():  # reported where mypy checks the rvalue
            value_type = checker.expr_checker.accept(value)
        self._vals_checker(ctx.api, addon_name).check_value(model, info, value, value_type)
        return ctx.default_signature

    def _vals_checker(self, api: CheckerPluginInterface, addon_name: str) -> ValsChecker:
        return ValsChecker(
            type_checker(api),
            self._profile.base_model,
            self._profile.new_id,
            self._models.model_of,
            partial(self._concrete, addon_name=addon_name, api=api),
            partial(self._vals_target, addon_name=addon_name, api=api),
            self._field_guarded(api.path),
            partial(self._accepted_values, addon_name=addon_name, api=api),
        )

    def _vals_target(self, model: str, addon_name: str, api: CheckerPluginInterface) -> ValsTarget:
        """The fields the vals of a model are checked against, as an addon sees them."""
        return ValsTarget(
            model,
            self._fields_of(addon_name, api)(model),
            check_keys=self._concrete(model, addon_name, api),
            ignore_keys=self._settings.vals_ignore_keys,
        )

    def _concrete(self, model: str, addon_name: str, api: CheckerPluginInterface) -> bool:
        self._depend_on(model, addon_name, api)
        return self.registry.concrete(model, addon_name)

    def _accepted_values(
        self, model: str, addon_name: str, api: CheckerPluginInterface
    ) -> frozenset[str]:
        self._depend_on(model, addon_name, api)
        return self.registry.accepted_values(model, addon_name)
