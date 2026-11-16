"""Hooks that check values written to records: ``create`` / ``write`` vals, assignments.

The values themselves are checked by :mod:`odoo_typing.vals`.
"""

from __future__ import annotations

from functools import partial

from mypy.lookup import lookup_fully_qualified
from mypy.nodes import CallExpr, MemberExpr, NameExpr, TempNode, TypeInfo
from mypy.plugin import CheckerPluginInterface, MethodContext, MethodSigContext
from mypy.types import AnyType, FunctionLike, Instance, Type, get_proper_type, has_type_vars

# isort: split
# After mypy.types: these modules import each other at import time.
from mypy.checker import TypeChecker
from mypy.maptype import map_instance_to_supertype
from mypy.typevars import fill_typevars_with_any

from odoo_typing.classes import receiver_type
from odoo_typing.hooks.models import Hooks, errors_hidden
from odoo_typing.hooks.nodes import type_checker
from odoo_typing.methods import is_self_method, keep_receiver
from odoo_typing.vals import ValsChecker, ValsTarget, context_defaults, vals_dicts

__all__ = ["ValsHooks"]


class ValsHooks(Hooks):
    """Check literal ``vals`` dicts and the values assigned to fields."""

    def check(self, method: str, ctx: MethodContext) -> Type:
        """Check the literal ``vals`` of ``create`` / ``write`` / ``copy(default=)``."""
        base_model = self._profile.base_model
        receiver = receiver_type(ctx.type)
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
        if (
            not isinstance(lvalue, MemberExpr)
            or not isinstance(records, TempNode)
            or errors_hidden(ctx.api)
        ):
            return ctx.default_signature
        record = receiver_type(records.type)
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
            self._field_guarded(api),
            partial(self._accepted_values, addon_name=addon_name, api=api),
            self._selection_elsewhere(addon_name),
            partial(self._read_type, type_checker(api)),
        )

    def _read_type(self, checker: TypeChecker, field_class: str) -> Type | None:
        """What reading a field of an Odoo field class gives (the ``T`` of ``Field[T]``)."""
        profile = self._profile
        # ``odoo.fields`` has every field class, wherever Odoo defines it (19+: odoo.orm).
        fields_module = f"{self._settings.odoo_path.name}.fields"
        found = lookup_fully_qualified(f"{fields_module}.{field_class}", checker.modules)
        base = lookup_fully_qualified(profile.field_base, checker.modules)
        info = found.node if found is not None else None
        field = base.node if base is not None else None
        if (
            not isinstance(info, TypeInfo)
            or not isinstance(field, TypeInfo)
            or not field.defn.type_vars
            or field not in info.mro
        ):
            return None
        own = fill_typevars_with_any(info)
        if not isinstance(own, Instance):
            return None
        read = map_instance_to_supertype(own, field).args[0]
        return None if has_type_vars(read) or isinstance(get_proper_type(read), AnyType) else read

    def _vals_target(self, model: str, addon_name: str, api: CheckerPluginInterface) -> ValsTarget:
        """The fields the vals of a model are checked against, as an addon sees them."""
        return ValsTarget(
            model,
            self._fields_of(addon_name, api)(model),
            check_keys=self._concrete(model, addon_name, api),
            ignore_keys=self._settings.vals_ignore_keys,
            taken_keys=partial(self.registry.value_keys, model, addon_name),
        )

    def _concrete(self, model: str, addon_name: str, api: CheckerPluginInterface) -> bool:
        self._depend_on(model, addon_name, api)
        return self.registry.concrete(model, addon_name)

    def _accepted_values(
        self, model: str, addon_name: str, api: CheckerPluginInterface
    ) -> frozenset[str]:
        self._depend_on(model, addon_name, api)
        return self.registry.accepted_values(model, addon_name)
