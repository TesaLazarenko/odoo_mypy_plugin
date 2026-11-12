"""Hooks of the environment: ``env["model.name"]``, ``env.ref(...)``, ``env.user``, ``request``."""

from __future__ import annotations

from mypy.lookup import lookup_fully_qualified
from mypy.nodes import MypyFile, StrExpr, TypeInfo, Var
from mypy.plugin import AttributeContext, CheckerPluginInterface, MethodContext
from mypy.types import (
    AnyType,
    Instance,
    NoneType,
    Type,
    TypeOfAny,
    UnionType,
    get_proper_type,
)

# isort: split
# After mypy.types: these modules import each other at import time.
from mypy.checker import TypeChecker

from odoo_typing.classes import instance_of
from odoo_typing.compat import OdooProfile
from odoo_typing.guards import module_guard
from odoo_typing.hooks.models import Hooks
from odoo_typing.hooks.nodes import call_argument, is_false, literal_string
from odoo_typing.references import did_you_mean
from odoo_typing.xmlids import ODOO_XMLID, ODOO_XMLID_DEPENDS, automatic_model

__all__ = ["EnvHooks", "request_env", "type_request"]


class EnvHooks(Hooks):
    """Type what an ``Environment`` gives, as the addon of the checked file sees it."""

    def getitem(self, ctx: MethodContext) -> Type:
        """``env["model.name"]``: the model's records as the addon sees them."""
        model = _model_name_argument(ctx)
        addon_name = self.registry.addon_name_for_path(ctx.api.path)
        if addon_name is None:
            return ctx.default_return_type  # outside any addon: see odoo_typing.plugin
        if model is None:
            # A name known only at run time: ``BaseModel`` would make every field read
            # an error, so ``Any`` unless the user asked for ``dynamic_env = "base"``.
            if self._settings.dynamic_env == "any":
                return AnyType(TypeOfAny.special_form)
            return ctx.default_return_type
        if self.registry.view_class(model, addon_name) is None:
            outside = self._models.unknown_model(model, addon_name, ctx.context, ctx.api)
            return outside or ctx.default_return_type
        return self._models.env_model(model, addon_name, ctx.api) or ctx.default_return_type

    def ref(self, ctx: MethodContext) -> Type:
        """``env.ref("module.name")``: the records of the XML id's model.

        The id must be created by the data files of the addon's closure (``odoo-xmlid``;
        ``odoo-xmlid-depends`` when only an addon outside it creates it). With
        ``raise_if_not_found=False`` the result may be ``None``, and an id that is not
        found is not an error (the code expects it to be missing).
        """
        xml_id = call_argument(ctx, "xml_id", 0)
        addon_name = self.registry.addon_name_for_path(ctx.api.path)
        if not isinstance(xml_id, StrExpr) or addon_name is None:
            return ctx.default_return_type
        optional = is_false(call_argument(ctx, "raise_if_not_found", 1))
        model = self._xml_id_model(xml_id, addon_name, optional, ctx.api)
        if model is None:
            return ctx.default_return_type
        records = self._models.view_type(model, addon_name, ctx.api)
        if not isinstance(records, Instance):
            return ctx.default_return_type
        return UnionType([records, NoneType()]) if optional else records

    def _xml_id_model(
        self, xml_id: StrExpr, addon_name: str, optional: bool, api: CheckerPluginInterface
    ) -> str | None:
        """The model of an XML id as an addon sees it; report it if it is not created."""
        name = xml_id.value
        module, _, local = name.partition(".")
        if not local:
            api.fail(f'XML id "{name}" has no module ("module.name")', xml_id, code=ODOO_XMLID)
            return None
        known = self.registry.xml_ids(addon_name)
        if name in known:
            return known[name]
        closure = self.registry.addons.closure(addon_name)
        if module in closure and (automatic := automatic_model(local)) is not None:
            return automatic
        if optional or self.registry.guarded(api.path, module_guard(module), xml_id.line):
            return None
        outside = self.registry.xml_id_outside(name, addon_name)
        if outside is not None:
            message = (
                f'XML id "{name}" is created by addon "{outside}", which is not in the '
                f'dependencies of "{addon_name}"'
            )
            api.fail(message, xml_id, code=ODOO_XMLID_DEPENDS)
            return None
        if module not in closure:
            return None  # created by code (``local_es.x``), or by an addon not on disk
        same_module = [item for item in known if item.startswith(f"{module}.")]
        message = f'Unknown XML id "{name}"' + did_you_mean(name, same_module)
        api.fail(message, xml_id, code=ODOO_XMLID)
        return None

    def record(self, model: str, ctx: AttributeContext) -> Type:
        """``env.user`` and the like: a record of a model, as the reading addon sees it."""
        addon_name = self.registry.addon_name_for_path(ctx.api.path)
        if ctx.is_lvalue or addon_name is None:
            return ctx.default_attr_type
        resolved = self._models.env_model(model, addon_name, ctx.api)
        return resolved if isinstance(resolved, Instance) else ctx.default_attr_type


def _model_name_argument(ctx: MethodContext) -> str | None:
    """The model name in ``env[...]``: a string literal, or a ``Final`` / ``Literal`` str."""
    if not ctx.args or len(ctx.args[0]) != 1:
        return None
    found = literal_string(ctx.args[0][0], ctx.arg_types[0][0])
    return None if found is None else found.value


def request_env(environment: str, ctx: AttributeContext) -> Type:
    """``request.env`` / ``self.env`` (Odoo 17): an ``Environment``.

    Odoo sets ``request.env`` to ``None`` before dispatch, and Odoo 17 declares
    ``BaseModel.env`` as an unannotated property.
    """
    if not isinstance(ctx.api, TypeChecker):
        return ctx.default_attr_type
    node = lookup_fully_qualified(environment, ctx.api.modules)
    if node is None or not isinstance(node.node, TypeInfo):
        return ctx.default_attr_type
    return instance_of(node.node)


def type_request(profile: OdooProfile, modules: dict[str, MypyFile]) -> bool:
    """Type ``odoo.http.request`` as a ``Request`` where Odoo leaves it ``Any``.

    Odoo 18-19 build it with werkzeug's ``LocalStack()()``, which mypy cannot follow
    (werkzeug is not on the search path), so ``request.env[...]`` in a controller went
    unchecked. It runs from the class hook of a later module, once ``odoo.http`` is
    type-checked (its variable has a type then).

    Returns:
        Whether nothing is left to do (typed now, or typed by Odoo).
    """
    if profile.request_var is None:
        return True
    if "odoo.http" not in modules:
        return False
    variable = lookup_fully_qualified(profile.request_var, modules)
    request = lookup_fully_qualified(profile.request_class, modules)
    if (
        variable is None
        or request is None
        or not isinstance(variable.node, Var)
        or not isinstance(request.node, TypeInfo)
        or variable.node.type is None
    ):
        return False  # odoo.http is not analyzed (or type-checked) yet: a later class
    if isinstance(get_proper_type(variable.node.type), AnyType):
        variable.node.type = instance_of(request.node)
    return True
