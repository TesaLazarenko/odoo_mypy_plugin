"""Hooks of field definitions: the names they refer to, the comodel of relational fields.

``fields.X(related=..., compute=...)`` and ``@api.depends(...)`` in a model's class body
(:mod:`odoo_typing.references`), ``domain=`` and the declared type of
``fields.Many2one("x")``.
"""

from __future__ import annotations

from collections.abc import Callable

from mypy.nodes import CallExpr, StrExpr, TypeInfo
from mypy.plugin import CheckerPluginInterface, FunctionContext
from mypy.types import AnyType, Instance, Type, TypeOfAny, get_proper_type

from odoo_typing.classes import instance_of
from odoo_typing.hooks.domains import DomainHooks
from odoo_typing.hooks.models import Hooks, Models
from odoo_typing.hooks.nodes import class_infos, field_arguments, type_checker
from odoo_typing.references import (
    KNOWN_CLASSES,
    METHOD_ARGUMENTS,
    ODOO_FIELD_REF,
    decorator_problem,
    field_kind,
    missing_method_message,
    resolve_path,
)

__all__ = ["FieldDefinitionHooks"]


class FieldDefinitionHooks(Hooks):
    """Check field definitions and field decorators; bind relational fields to comodels."""

    def __init__(self, models: Models, domains: DomainHooks) -> None:
        super().__init__(models)
        self._domains = domains

    def definition(
        self, then: Callable[[FunctionContext], Type] | None, ctx: FunctionContext
    ) -> Type:
        """Check the names a field definition refers to, then type it (``then``)."""
        owner = self._field_owner(ctx)
        default = get_proper_type(ctx.default_return_type)
        if (
            owner is not None
            and isinstance(ctx.context, CallExpr)
            and isinstance(default, Instance)
        ):
            self._check_field_references(ctx.context, default.type, *owner, ctx.api)
            if default.type.fullname in self._profile.relational_classes:
                self._domains.check_field(ctx.context, default.type.name, owner[1], ctx.api)
        return then(ctx) if then is not None else ctx.default_return_type

    def _field_owner(self, ctx: FunctionContext) -> tuple[str, str, TypeInfo] | None:
        """The model, addon and class whose body a field definition is in, if a model's."""
        checker = type_checker(ctx.api)
        info = checker.scope.active_class()
        addon_name = self.registry.addon_name_for_path(ctx.api.path)
        model = self._models.model_of(info) if info is not None else None
        if info is None or model is None or addon_name is None:
            return None
        return model, addon_name, info

    def _check_field_references(
        self,
        call: CallExpr,
        field_class: TypeInfo,
        model: str,
        addon_name: str,
        owner: TypeInfo,
        api: CheckerPluginInterface,
    ) -> None:
        """Report fields of ``related=`` and methods of ``compute=``... that do not exist.

        Not in abstract models: their fields refer to fields and methods of the models
        that use them.
        """
        if not field_class.has_base(self._profile.field_base) or not self.registry.concrete(
            model, addon_name
        ):
            return
        for argument, value in field_arguments(call, field_class.name):
            if not isinstance(value, StrExpr):
                continue
            if argument == "related":
                self._check_related(value, field_class.name, model, addon_name, api)
            elif argument in METHOD_ARGUMENTS and not (
                argument == "selection" and field_class.name not in {"Selection", "Reference"}
            ):
                known = owner.get(value.value) is not None
                if not known and not self.registry.has_method(model, addon_name, value.value):
                    methods = self.registry.methods(model, addon_name)
                    message = missing_method_message(model, argument, value.value, methods)
                    api.fail(message, value, code=ODOO_FIELD_REF)

    def _check_related(
        self,
        path: StrExpr,
        field_class: str,
        model: str,
        addon_name: str,
        api: CheckerPluginInterface,
    ) -> None:
        fields_of = self._fields_of(addon_name, api)
        result = resolve_path(path.value, model, fields_of, f'related="{path.value}"')
        if result.problem is not None:
            api.fail(result.problem, path, code=ODOO_FIELD_REF)
            return
        target = result.field
        if (
            target is not None
            and field_class in KNOWN_CLASSES
            and target.field_type in KNOWN_CLASSES
            and field_kind(field_class) != field_kind(target.field_type)
        ):
            api.fail(
                f'Related field is a {field_class}, but "{path.value}" is a '
                f'{target.field_type} field of Odoo model "{result.model}"',
                path,
                code=ODOO_FIELD_REF,
            )

    def decorator_fields(
        self,
        decorator: str,
        then: Callable[[FunctionContext], Type] | None,
        ctx: FunctionContext,
    ) -> Type:
        """Check the fields that ``@api.depends(...)``, ``constrains``, ``onchange`` name.

        Only in the body of a concrete model's class, as for field definitions.
        """
        owner = self._field_owner(ctx)
        if owner is not None and self.registry.concrete(owner[0], owner[1]):
            model, addon_name, _ = owner
            fields_of = self._fields_of(addon_name, ctx.api)
            for argument in [item for arguments in ctx.args for item in arguments]:
                if not isinstance(argument, StrExpr):
                    continue  # a lambda, ``*names``
                problem = decorator_problem(decorator, argument.value, model, fields_of)
                if problem is not None:
                    ctx.api.fail(problem, argument, code=ODOO_FIELD_REF)
        return then(ctx) if then is not None else ctx.default_return_type

    def relational(self, ctx: FunctionContext) -> Type:
        """Bind ``fields.Many2one("x")`` to the class that defines model ``x``.

        The declared type is the same for every declaration of a field with that
        comodel, whichever addon declares it; reading the field resolves the comodel
        again from the reading addon (:meth:`FieldValueHooks.get`).
        """
        default = get_proper_type(ctx.default_return_type)
        if not isinstance(default, Instance):
            return ctx.default_return_type
        comodel = _comodel_expression(ctx)
        addon_name = self.registry.addon_name_for_path(ctx.api.path)
        name = comodel.value if comodel is not None else self._related_comodel(ctx)
        if name is None or addon_name is None:
            # A redefinition (``fields.Many2one(ondelete=...)``) keeps the comodel of the
            # earlier declaration; mypy cannot infer ``M`` from such a call.
            unknown = AnyType(TypeOfAny.special_form)
            return default.copy_modified(args=[unknown]) if default.args else default
        definition = self.registry.definition(name, addon_name)
        if definition is None:
            if comodel is not None:
                self._models.unknown_model(name, addon_name, comodel, ctx.api)
            unknown = AnyType(TypeOfAny.from_error)
            return default.copy_modified(args=[unknown]) if default.type.type_vars else default
        if not default.type.type_vars:
            return default  # Odoo 17: not generic; reading the field finds the comodel
        found = class_infos([definition], type_checker(ctx.api).modules).infos
        model_type = instance_of(found[0]) if found else AnyType(TypeOfAny.special_form)
        return default.copy_modified(args=[model_type])

    def _related_comodel(self, ctx: FunctionContext) -> str | None:
        """The comodel of the last field of a ``related=`` path in a field definition."""
        owner = self._field_owner(ctx)
        call = ctx.context
        if owner is None or not isinstance(call, CallExpr):
            return None
        model, addon_name, _ = owner
        path = dict(field_arguments(call, "")).get("related")
        if not isinstance(path, StrExpr):
            return None
        fields_of = self._fields_of(addon_name, ctx.api)
        target = resolve_path(path.value, model, fields_of, "").field
        if target is None or "comodel" in target.dynamic:
            return None
        return target.comodel


def _comodel_expression(ctx: FunctionContext) -> StrExpr | None:
    if "comodel_name" not in ctx.callee_arg_names:
        return None
    actuals = ctx.args[ctx.callee_arg_names.index("comodel_name")]
    if len(actuals) == 1 and isinstance(actuals[0], StrExpr):
        return actuals[0]
    return None
