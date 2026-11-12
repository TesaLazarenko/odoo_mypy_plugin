"""Hooks that check domains: ``search([...])`` and the like, ``domain=`` of fields.

The terms themselves are checked by :mod:`odoo_typing.domains`.
"""

from __future__ import annotations

from functools import partial

from mypy.nodes import CallExpr, Expression, StrExpr
from mypy.plugin import CheckerPluginInterface, MethodContext
from mypy.types import Instance, Type, get_proper_type

from odoo_typing.domains import (
    ODOO_DOMAIN,
    DomainChecker,
    Term,
    domain_term_lists,
    string_domain_paths,
)
from odoo_typing.hooks.models import Hooks, Models
from odoo_typing.hooks.nodes import field_arguments
from odoo_typing.hooks.recordsets import RecordsetHooks
from odoo_typing.methods import is_self_method, keep_receiver

__all__ = ["DomainHooks"]


class DomainHooks(Hooks):
    """Check the domains of method calls and of relational field definitions."""

    def __init__(self, models: Models, recordsets: RecordsetHooks) -> None:
        super().__init__(models)
        self._recordsets = recordsets

    def check(self, method: str, ctx: MethodContext) -> Type:
        """Check the literal ``domain`` of ``search`` / ``search_count`` / ``_read_group``...

        Only on a concrete model: a mixin's domains name fields of the models using it.
        """
        receiver = get_proper_type(ctx.type)
        addon_name = self.registry.addon_name_for_path(ctx.api.path)
        model = self._models.model_of(receiver.type) if isinstance(receiver, Instance) else None
        index = ctx.callee_arg_names.index("domain") if "domain" in ctx.callee_arg_names else 0
        domain = ctx.args[index][0] if len(ctx.args) > index and ctx.args[index] else None
        if (
            domain is not None
            and model is not None
            and addon_name is not None
            and self.registry.concrete(model, addon_name)
        ):
            self._check_domain_expression(domain, model, addon_name, ctx.api)
        if method == "search_read":
            return self._recordsets.read_result(method, ctx)
        if is_self_method(method):
            return keep_receiver(self._profile.base_model, ctx)
        return ctx.default_return_type

    def _domain_checker(self, addon_name: str, api: CheckerPluginInterface) -> DomainChecker:
        return DomainChecker(
            partial(api.fail, code=ODOO_DOMAIN),
            partial(self.registry.fields, addon_name=addon_name),
            self._profile.domain_operators,
            self._field_guarded(api.path),
        )

    def _check_domain_expression(
        self, domain: Expression, model: str, addon_name: str, api: CheckerPluginInterface
    ) -> None:
        """Check the literal terms of a domain expression (:func:`domain_term_lists`)."""
        checker = self._domain_checker(addon_name, api)
        local = self._local_values(api)
        for items in domain_term_lists(domain, self._profile.domain_class, local):
            checker.check(model, items)

    def check_field(
        self, call: CallExpr, field_class: str, addon_name: str, api: CheckerPluginInterface
    ) -> None:
        """Check the ``domain=`` of a relational field against its comodel."""
        arguments = dict(field_arguments(call, field_class))
        comodel = arguments.get("comodel_name")
        domain = arguments.get("domain")
        if not isinstance(comodel, StrExpr) or domain is None:
            return
        if not self.registry.concrete(comodel.value, addon_name):
            return
        if not isinstance(domain, StrExpr):
            self._check_domain_expression(domain, comodel.value, addon_name, api)
            return
        checker = self._domain_checker(addon_name, api)
        for path, operator in string_domain_paths(domain.value):
            path_node = StrExpr(path)
            path_node.set_line(domain)
            operator_node = None
            if operator is not None:
                operator_node = StrExpr(operator)
                operator_node.set_line(domain)
            checker.check_term(comodel.value, Term(path_node, operator_node, None))
