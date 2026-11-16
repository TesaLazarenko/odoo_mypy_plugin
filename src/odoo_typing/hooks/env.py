"""Hooks of the environment: ``env["model.name"]``, ``env.ref(...)``, ``env.user``, ``request``."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from mypy.lookup import lookup_fully_qualified
from mypy.nodes import MDEF, MemberExpr, MypyFile, StrExpr, SymbolTableNode, TypeInfo, Var
from mypy.plugin import AttributeContext, CheckerPluginInterface, ClassDefContext, MethodContext
from mypy.types import (
    AnyType,
    Instance,
    NoneType,
    Type,
    TypeOfAny,
    TypeType,
    TypeVarType,
    UnionType,
    get_proper_type,
)

# isort: split
# After mypy.types: these modules import each other at import time.
from mypy.checker import TypeChecker

from odoo_typing.classes import instance_of, receiver_type
from odoo_typing.compat import OdooProfile
from odoo_typing.guards import module_guard
from odoo_typing.hooks.models import Hooks
from odoo_typing.hooks.nodes import call_argument, is_false, literal_string, type_checker
from odoo_typing.references import did_you_mean
from odoo_typing.xmlids import ODOO_XMLID, ODOO_XMLID_DEPENDS, automatic_model

__all__ = [
    "GROUPS",
    "REQUEST_ATTRIBUTES",
    "XML_ID_METHODS",
    "EnvHooks",
    "XmlIdUse",
    "add_request_attributes",
    "env_cursor",
    "present_env",
    "request_env",
    "type_request",
]


EXECUTE: Final = "execute"  # what a cursor class has (see env_cursor)


@dataclass(frozen=True, slots=True)
class XmlIdUse:
    """What a method does with the XML ids it takes.

    Attributes:
        model: The model of the records the ids must name (a prefix: ``ir.actions.``).
        what: How a message names such a record.
        several: Whether the string is a comma-separated list of ids, each maybe negated.
        receiver: The model whose method it is (a prefix), ``None`` for any model.
        absent_ok: Whether an id of an addon outside the dependencies is fine (Odoo
            answers ``False`` for a group that does not exist; it raises for an action).
    """

    model: str
    what: str
    several: bool = False
    receiver: str | None = None
    absent_ok: bool = True


GROUPS: Final = XmlIdUse("res.groups", "a group", several=True)
XML_ID_METHODS: Final = {
    "has_group": XmlIdUse("res.groups", "a group", receiver="res.users"),
    "has_groups": XmlIdUse("res.groups", "a group", several=True, receiver="res.users"),
    "user_has_groups": GROUPS,
    "_for_xml_id": XmlIdUse("ir.actions.", "an action", receiver="ir.actions.", absent_ok=False),
}


class EnvHooks(Hooks):
    """Type what an ``Environment`` gives, as the addon of the checked file sees it."""

    def getitem(self, ctx: MethodContext) -> Type:
        """``env["model.name"]``: the model's records as the addon sees them."""
        model = _model_name_argument(ctx)
        addon_name = self.registry.addon_name_for_path(ctx.api.path)
        if addon_name is None:
            return ctx.default_return_type  # outside any addon: see odoo_typing.plugin
        if model is None:
            own = self._records_of_name(ctx)
            if own is not None:
                return own
            # A name known only at run time: ``BaseModel`` would make every field read
            # an error, so ``Any`` unless the user asked for ``dynamic_env = "base"``.
            if self._settings.dynamic_env == "any":
                return AnyType(TypeOfAny.special_form)
            return ctx.default_return_type
        if self.registry.view_class(model, addon_name) is None:
            outside = self._models.unknown_model(model, addon_name, ctx.context, ctx.api)
            return outside or ctx.default_return_type
        return self._models.env_model(model, addon_name, ctx.api) or ctx.default_return_type

    def _records_of_name(self, ctx: MethodContext) -> Type | None:
        """``env[records._name]``: the type of ``records``, when they are records of a model.

        In a mixin, ``self._name`` is the model that uses it at run time, so ``self``'s
        type (also ``Self``, or the class of ``cls``) is the right one.
        """
        argument = ctx.args[0][0] if ctx.args and len(ctx.args[0]) == 1 else None
        if not isinstance(argument, MemberExpr) or argument.name != "_name":
            return None
        owner = type_checker(ctx.api).lookup_type_or_none(argument.expr)
        records = get_proper_type(owner)
        if isinstance(records, TypeType):
            records = records.item
        bound = records.upper_bound if isinstance(records, TypeVarType) else records
        bound = get_proper_type(bound)
        if not isinstance(bound, Instance) or self._models.model_of(bound.type) is None:
            return None
        return records

    def ref(self, ctx: MethodContext) -> Type:
        """``env.ref("module.name")``: the records of the XML id's model.

        The id must be created by the data files of the addon's closure (``odoo-xmlid``;
        ``odoo-xmlid-depends`` when only an addon outside it creates it). With
        ``raise_if_not_found=False`` the result may be ``None``, and an id that is not
        found is not an error (the code expects it to be missing).
        """
        xml_id = call_argument(ctx, "xml_id", 0)
        addon_name = self.registry.addon_name_for_path(ctx.api.path)
        if addon_name is None:
            return ctx.default_return_type
        if not isinstance(xml_id, StrExpr):  # an id made at run time
            return self._of_no_known_model(ctx.default_return_type)
        optional = is_false(call_argument(ctx, "raise_if_not_found", 1))
        model = self._xml_id_model(xml_id, addon_name, optional, ctx.api)
        if model is None:
            return self._of_no_known_model(ctx.default_return_type)
        records = self._models.view_type(model, addon_name, ctx.api)
        if not isinstance(records, Instance):
            return self._of_no_known_model(ctx.default_return_type)
        return UnionType([records, NoneType()]) if optional else records

    def _of_no_known_model(self, declared: Type) -> Type:
        """Records whose model the data files do not say: ``Any`` where Odoo says ``BaseModel``.

        Odoo 19 declares ``ref`` as ``BaseModel`` (``| None``): records, but of no
        model, so reading any field of them was an unknown attribute. They are records
        of some model that is not known here; ``None`` stays in the type. As for
        ``env[name]`` of a name made at run time, ``dynamic_env = "base"`` keeps
        ``BaseModel``.
        """
        if self._settings.dynamic_env != "any":
            return declared
        proper = get_proper_type(declared)
        items = proper.items if isinstance(proper, UnionType) else [proper]
        base = self._profile.base_model
        mapped = [
            AnyType(TypeOfAny.special_form)
            if isinstance(item, Instance) and item.type.fullname == base
            else item
            for item in map(get_proper_type, items)
        ]
        return UnionType.make_union(mapped) if isinstance(proper, UnionType) else mapped[0]

    def xml_id_argument(self, method: str, ctx: MethodContext) -> Type:
        """``user.has_group("base.group_user")``, ``_for_xml_id("sale.action_orders")``.

        The ids must exist as for ``env.ref`` (a group that does not exist is ``False``
        for Odoo, an action raises), and be a group or an action. ``has_groups`` and
        ``user_has_groups`` take several, separated by commas, each maybe negated (``!``).
        """
        argument = ctx.args[0][0] if ctx.args and len(ctx.args[0]) == 1 else None
        addon_name = self.registry.addon_name_for_path(ctx.api.path)
        receiver = receiver_type(ctx.type)
        if not isinstance(argument, StrExpr) or addon_name is None:
            return ctx.default_return_type
        model = self._models.model_of(receiver.type) if isinstance(receiver, Instance) else None
        expected = XML_ID_METHODS[method]
        if model is None or not (expected.receiver is None or model.startswith(expected.receiver)):
            return ctx.default_return_type
        self.check_xml_ids(argument, expected, addon_name, ctx.api)
        return ctx.default_return_type

    def check_xml_ids(
        self, ids: StrExpr, expected: XmlIdUse, addon_name: str, api: CheckerPluginInterface
    ) -> None:
        """Check the XML ids of a string (one, or a comma-separated list) for a use."""
        names = ids.value.split(",") if expected.several else [ids.value]
        closure = self.registry.addons.closure(addon_name)
        for name in names:
            node = StrExpr(name.strip().removeprefix("!") if expected.several else name)
            node.set_line(ids)
            module = node.value.partition(".")[0]
            if not node.value or (expected.absent_ok and module not in closure):
                continue  # a group of an addon that may not be installed: ``False`` for Odoo
            found = self._xml_id_model(node, addon_name, False, api)
            if found is not None and not found.startswith(expected.model):
                message = f'XML id "{node.value}" is a record of Odoo model "{found}", '
                api.fail(message + f"not {expected.what}", ids, code=ODOO_XMLID)

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
            return known[name] or None  # UNKNOWN_MODEL: ``_update_xmlids`` does not say
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

    def named_record(self, model: str, ctx: AttributeContext) -> Type:
        """``env.website`` (Odoo 20): records of the model an attribute is named after.

        Odoo declares the property as ``BaseModel``: records, but of no model, so every
        field read from them was an unknown attribute. Only where the declared type is
        ``BaseModel`` itself and the reading addon sees a model of that name.
        """
        declared = get_proper_type(ctx.default_attr_type)
        if not isinstance(declared, Instance) or declared.type.fullname != self._profile.base_model:
            return ctx.default_attr_type
        return self.record(model, ctx)

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


def present_env(environment: str, ctx: AttributeContext) -> Type:
    """An ``env`` declared ``Environment | None``, read: the ``Environment``.

    Odoo 20 declares ``Controller.env`` that way (``None`` outside a request), like
    ``request.env``: code that handles a route has one, and Odoo's own controllers
    index it without a check. An assignment keeps the declared type. Only for the
    classes of Odoo itself: an addon's own ``env: Environment | None`` stays as its
    author declared it.
    """
    declared = get_proper_type(ctx.default_attr_type)
    if ctx.is_lvalue or not isinstance(declared, UnionType):
        return ctx.default_attr_type
    kept = [item for item in map(get_proper_type, declared.items) if not isinstance(item, NoneType)]
    if len(kept) != 1 or len(declared.items) != 2:  # not ``X | None``
        return ctx.default_attr_type
    present = kept[0]
    if isinstance(present, Instance) and present.type.fullname == environment:
        return present
    return ctx.default_attr_type


def env_cursor(ctx: AttributeContext) -> Type:
    """``env.cr``: the cursor classes of the module, where Odoo declares an abstract base.

    Odoo 18 annotates ``Environment.cr`` with ``BaseCursor``, a base class that cannot
    execute a query: ``execute``, ``fetchall`` and ``dbname`` are those of its
    subclasses (``Cursor``, and ``TestCursor`` in tests), so every
    ``env.cr.execute(...)`` was an ``attr-defined`` error. The attribute is then one of
    the classes that the declared class's module derives from it. A declared class
    that executes queries itself (Odoo 19, 20) stays as declared.
    """
    declared = get_proper_type(ctx.default_attr_type)
    if (
        not isinstance(declared, Instance)
        or not isinstance(ctx.api, TypeChecker)
        or declared.type.get(EXECUTE) is not None
    ):
        return ctx.default_attr_type
    module = ctx.api.modules.get(declared.type.module_name)
    if module is None:
        return ctx.default_attr_type
    concrete = [
        instance_of(node.node)
        for node in module.names.values()
        if isinstance(node.node, TypeInfo)
        and node.node is not declared.type
        and node.node.module_name == module.fullname  # defined there, not imported
        and declared.type in node.node.mro
    ]
    return UnionType.make_union(concrete) if concrete else ctx.default_attr_type


def type_request(profile: OdooProfile, modules: dict[str, MypyFile]) -> bool:
    """Type ``odoo.http.request`` as a ``Request`` where Odoo leaves it ``Any``.

    Odoo 17-19 build it with werkzeug's ``LocalStack()()``, which mypy cannot follow
    (werkzeug is not on the search path), so ``request.env[...]`` in a controller went
    unchecked. It runs once mypy has checked the assignment in ``odoo.http`` (its
    variable has a type then): from the plugin's hook lookups, which mypy makes while
    it checks ``odoo.http`` too, so that the module's cache entry keeps the type, and
    from the class hook of a later module.

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


REQUEST_ATTRIBUTES: Final = frozenset(
    {
        "website",  # website (ir_http._pre_dispatch)
        "website_routing",
        "is_frontend",  # http_routing
        "is_frontend_multilang",
        "lang",
        "allowed_company_ids",  # base (ir_module)
        "cart",  # website_sale, Odoo 19+
        "pricelist",
        "fiscal_position",
    }
)
"""Attributes Odoo's addons set on ``odoo.http.request`` at run time, which ``Request`` does
not declare (``request.website = website``)."""


def add_request_attributes(ctx: ClassDefContext) -> None:
    """Declare :data:`REQUEST_ATTRIBUTES` on ``Request`` as ``Any``.

    Without them every ``request.website`` of a controller is an ``attr-defined`` error
    once ``request`` is typed (:func:`type_request`). A misspelt name stays an error. At
    semantic analysis of ``odoo.http``, so the attributes are part of its cache entry.
    """
    info = ctx.cls.info
    for name in REQUEST_ATTRIBUTES - info.names.keys():
        var = Var(name, AnyType(TypeOfAny.special_form))
        var.info = info
        var._fullname = f"{info.fullname}.{name}"  # pylint: disable=protected-access
        var.is_ready = True
        info.names[name] = SymbolTableNode(MDEF, var, plugin_generated=True)
