"""Hooks of reading a field (``record.name``): comodel records and value types."""

from __future__ import annotations

from mypy.lookup import lookup_fully_qualified
from mypy.nodes import Context, Expression, MemberExpr, TypeInfo, Var
from mypy.plugin import (
    AttributeContext,
    CheckerPluginInterface,
    FunctionSigContext,
    MethodSigContext,
)
from mypy.types import (
    AnyType,
    CallableType,
    FunctionLike,
    Instance,
    LiteralType,
    NoneType,
    ProperType,
    TupleType,
    Type,
    TypeOfAny,
    UnionType,
    get_proper_type,
)

# isort: split
# After mypy.types: these modules import each other at import time.
from mypy.checker import TypeChecker
from mypy.maptype import map_instance_to_supertype
from mypy.typeops import make_simplified_union

from odoo_typing.classes import instance_of, receiver_type
from odoo_typing.compat import OdooProfile
from odoo_typing.hooks.models import Hooks
from odoo_typing.hooks.nodes import record_type, type_checker
from odoo_typing.references import resolve_path
from odoo_typing.scanner import FieldInfo
from odoo_typing.vals import RECORDS, VALUE_TYPES, stored, value_type_named

__all__ = ["FILTER", "FieldValueHooks", "falsy_filter_signature"]

FILTER = "builtins.filter"

# Field classes whose reading ``_value_get`` types (``Image`` is a ``Binary``).
VALUE_KINDS = frozenset({"Binary", "Reference", "Selection"})


class FieldValueHooks(Hooks):
    """Type what reading a field gives, as the reading addon sees the models."""

    def get(self, ctx: MethodSigContext) -> FunctionLike:
        """Type reading a field: a relational one gives its comodel's records."""
        descriptor = get_proper_type(ctx.type)
        signature = ctx.default_signature
        if not isinstance(descriptor, Instance) or not isinstance(signature, CallableType):
            return signature
        holder = get_proper_type(record_type(ctx))
        if isinstance(holder, TupleType) or (
            isinstance(holder, Instance) and holder.type.tuple_type is not None
        ):
            # A field class annotating a ``NamedTuple`` item (Odoo 19's ``Procurement``)
            # stands for the value it holds: no descriptor of records there.
            anything = AnyType(TypeOfAny.special_form)
            return signature.copy_modified(
                arg_types=[anything] * len(signature.arg_types), ret_type=anything
            )
        mro = descriptor.type.mro
        relational = self._profile.relational_base
        base = next((item for item in mro if item.fullname == relational), None)
        if base is None:
            found = self._value_get(descriptor, signature, ctx)
            return self._required_value(found, ctx) if self._settings.required_values else found
        records = record_type(ctx)
        if isinstance(records, NoneType):
            # Model.field: the field (Odoo 19 declares ``records: BaseModel``, not None).
            arg_types = [records, *signature.arg_types[1:]] if signature.arg_types else []
            return signature.copy_modified(arg_types=arg_types, ret_type=descriptor)
        args = map_instance_to_supertype(descriptor, base).args
        comodel = get_proper_type(args[0]) if args else None  # Odoo 19: not generic
        if not isinstance(comodel, Instance) and self._profile.untyped_fields:
            if not isinstance(records, Instance):
                return signature
            return self._indexed_comodel_get(records, signature, ctx)  # Odoo 17
        if not isinstance(comodel, Instance):
            return signature
        owner = records.type if isinstance(records, Instance) else None
        resolved = self._models.comodel_at_use_site(comodel, owner, ctx.api)
        return signature.copy_modified(ret_type=resolved or comodel)

    def _required_value(self, signature: FunctionLike, ctx: MethodSigContext) -> FunctionLike:
        """``required_values``: a required field (and ``display_name``) without ``False``.

        Odoo reads ``False`` for a required field of a new record or of a form whose
        value is not set yet; code that reads saved records does not see it. A value
        that types a variable whose later values are checked against it keeps the
        declared type (see :meth:`_stored`).
        """
        member = ctx.context
        records = record_type(ctx)
        if not isinstance(signature, CallableType) or not isinstance(records, Instance):
            return signature
        info = self._read_field(records, ctx)
        name = member.name if isinstance(member, MemberExpr) else None
        if name != "display_name" and (info is None or info.required is not True):
            return signature
        value = get_proper_type(signature.ret_type)
        if not isinstance(value, UnionType):
            return signature
        kept = [item for item in value.items if not _is_false(item)]
        if len(kept) == len(value.items) or self._stored(member, ctx.api):
            return signature
        return signature.copy_modified(ret_type=make_simplified_union(kept))

    def display_name(self, ctx: AttributeContext) -> Type:
        """``required_values``: ``display_name`` (annotated on ``BaseModel``) without ``False``."""
        value = get_proper_type(ctx.default_attr_type)
        if ctx.is_lvalue or not isinstance(value, UnionType) or self._stored(ctx.context, ctx.api):
            return ctx.default_attr_type
        return make_simplified_union([item for item in value.items if not _is_false(item)])

    def _stored(self, expression: Context, api: CheckerPluginInterface) -> bool:
        """Whether a value read types a variable that later values are checked against.

        mypy takes a variable's type from its first value: ``date = move.date`` (a
        required field) would make ``date = line.date`` (one that is not) an error in
        the other arm of an ``if``, and ``rows.append((tag.display_name, ...))`` would
        make ``rows.append((False, ...))`` one. There the field keeps the type Odoo
        declares; see :meth:`LocalValues.stored` for the variables mypy does not check so.
        """
        if not isinstance(expression, Expression):
            return False
        local = self._local_values(api)
        return local is not None and local.stored(expression)

    def _value_get(
        self, descriptor: Instance, signature: CallableType, ctx: MethodSigContext
    ) -> FunctionLike:
        """Type reading the fields that Odoo leaves untyped or types too loosely.

        ``Binary`` / ``Image`` (``Field`` without a value type, so ``Any``) read as
        ``bytes | Literal[False]``; ``Reference`` reads as a record of one of the models
        of a literal ``selection`` (``BaseModel`` otherwise), not as Selection's ``str``;
        with ``literal_selection``, a ``Selection`` with literal keys reads as
        ``Literal[<keys>] | Literal[False]``.
        """
        kind = next(
            (
                item.name
                for item in descriptor.type.mro
                if item.name in VALUE_KINDS and self._profile.is_field_class(item.fullname)
            ),
            None,
        )
        records = record_type(ctx)
        if not isinstance(records, Instance):
            return signature
        checker = type_checker(ctx.api)
        if kind is None or (kind == "Selection" and not self._settings.literal_selection):
            if self._profile.untyped_fields:  # Odoo 17
                return _class_value_get(descriptor, signature, self._profile, checker)
            return signature
        false = LiteralType(False, checker.named_type("builtins.bool"))
        info = self._read_field(records, ctx)
        if kind == "Binary":
            if not isinstance(get_proper_type(signature.ret_type), AnyType):
                return signature  # Odoo 20 types it (BinaryValue)
            if info is None or not stored(info):
                return signature  # a computed Binary holds any value (widget data)
            value: Type = make_simplified_union([checker.named_type("builtins.bytes"), false])
            return signature.copy_modified(ret_type=value)
        if kind == "Reference":
            models = self._reference_types(info, records, ctx.api)
            return signature.copy_modified(ret_type=make_simplified_union([*models, false]))
        if info is None or not info.selection or "selection" in info.dynamic:
            if self._profile.untyped_fields:
                return _class_value_get(descriptor, signature, self._profile, checker)
            return signature
        keys = [LiteralType(key, checker.named_type("builtins.str")) for key in info.selection]
        return signature.copy_modified(ret_type=make_simplified_union([*keys, false]))

    def _indexed_comodel_get(
        self, records: Instance, signature: CallableType, ctx: MethodSigContext
    ) -> FunctionLike:
        """Odoo 17: a relational field reads as its comodel's records, from the field index."""
        info = self._read_field(records, ctx)
        addon_name = self.registry.addon_name_for_path(ctx.api.path)
        model = self._models.model_of(records.type)
        if info is None or not addon_name or model is None:
            return signature
        comodel = info.comodel if "comodel" not in info.dynamic else None
        if comodel is None and info.related:  # fields.Many2one(related="a.b")
            fields_of = self._fields_of(addon_name, ctx.api)
            target = resolve_path(info.related, model, fields_of, "").field
            comodel = target.comodel if target is not None else None
        if comodel is None:
            return signature
        view = self._models.view_type(comodel, addon_name, ctx.api)
        return signature.copy_modified(ret_type=view) if isinstance(view, Instance) else signature

    def _read_field(self, records: Instance, ctx: MethodSigContext) -> FieldInfo | None:
        """The field read by ``record.name`` (``ctx.context``), as the reading addon sees it."""
        member = ctx.context
        model = self._models.model_of(records.type)
        addon_name = self.registry.addon_name_for_path(ctx.api.path)
        if not isinstance(member, MemberExpr) or model is None or addon_name is None:
            return None
        return self._fields_of(addon_name, ctx.api)(model).get(member.name)

    def _reference_types(
        self, info: FieldInfo | None, records: Instance, api: CheckerPluginInterface
    ) -> list[Type]:
        """The record types a ``Reference`` field holds: its selection's models."""
        found_base = next(
            (item for item in records.type.mro if item.fullname == self._profile.base_model), None
        )
        base: Type = (
            instance_of(found_base) if found_base is not None else AnyType(TypeOfAny.special_form)
        )
        addon_name = self.registry.addon_name_for_path(api.path)
        if info is None or not info.selection or "selection" in info.dynamic or not addon_name:
            return [base]
        found: list[Type] = []
        for model in info.selection:
            view = self._models.view_type(model, addon_name, api)
            if not isinstance(view, Instance):
                return [base]
            found.append(view)
        return found

    def record_attribute(self, ctx: AttributeContext) -> Type:
        """Re-resolve an attribute whose type is a model class from the reading addon.

        For instance base's annotated ``parent_id: Partner = fields.Many2one(...)``: the
        declaring addon's class has fewer extensions than the reading addon sees.
        Assignments keep the declared type.
        """
        declared = get_proper_type(ctx.default_attr_type)
        if not isinstance(declared, Instance):
            return ctx.default_attr_type
        owner = receiver_type(ctx.type)
        owner_info = owner.type if isinstance(owner, Instance) else None
        if ctx.is_lvalue:
            return self._assigned(declared, owner_info, ctx)
        return (
            self._models.comodel_at_use_site(declared, owner_info, ctx.api) or ctx.default_attr_type
        )

    def kept_record(self, ctx: AttributeContext) -> Type:
        """Records kept on an object that is no model, as the reading addon sees their model.

        A test case keeps the records it creates (``cls.product = ...`` in the
        ``setUpClass`` of ``product``'s common test class), and the tests of other addons
        read them: Odoo gives them the registry class, with the fields of every installed
        addon, so a test of ``sale`` reads ``self.product.invoice_policy``.

        An assignment takes the same class: the records a module gets from ``env`` are
        of the class that module sees, which may be another class of the same view than
        the one the attribute was first given (a merged class is made per module).
        """
        declared = get_proper_type(ctx.default_attr_type)
        if isinstance(declared, UnionType):  # ``cls.company = found or cls.env.company``
            items = [self._kept(get_proper_type(item), ctx) or item for item in declared.items]
            return make_simplified_union(items)
        return self._kept(declared, ctx) or ctx.default_attr_type

    def _kept(self, kept: ProperType, ctx: AttributeContext) -> Type | None:
        """Kept records as the reading addon sees them; ``None`` for anything else."""
        if not isinstance(kept, Instance) or self._models.model_of(kept.type) is None:
            return None
        return self._models.comodel_at_use_site(kept, None, ctx.api)

    def keeps_record(self, variable: Var) -> bool | None:
        """Whether an attribute holds records of a model; ``None`` while mypy infers its type."""
        if variable.type is None:
            return None
        kept = get_proper_type(variable.type)
        items = kept.items if isinstance(kept, UnionType) else [kept]
        return any(
            isinstance(item, Instance) and self._models.model_of(item.type) is not None
            for item in map(get_proper_type, items)
        )

    def _assigned(self, declared: Instance, owner: TypeInfo | None, ctx: AttributeContext) -> Type:
        """What an annotated relational field takes (``partner.country_id = False``).

        The annotation (``country_id: Country = fields.Many2one(...)``) is a variable of
        the comodel's records, but Odoo takes what ``write`` takes: ``False``, ``None``,
        an id, commands. The records keep the declared comodel.
        """
        member = ctx.context
        model = self._models.model_of(owner) if owner is not None else None
        addon_name = self.registry.addon_name_for_path(ctx.api.path)
        if not isinstance(member, MemberExpr) or model is None or addon_name is None:
            return ctx.default_attr_type
        info = self._fields_of(addon_name, ctx.api)(model).get(member.name)
        names = VALUE_TYPES.get(info.field_type, ()) if info is not None else ()
        if info is None or info.comodel is None or not names:
            return ctx.default_attr_type
        checker = type_checker(ctx.api)
        items: list[Type] = [declared]
        for name in names:
            if name == RECORDS:
                continue  # the declared comodel's
            item = value_type_named(name, checker, self._profile.base_model, self._profile.new_id)
            if item is None:
                return ctx.default_attr_type
            items.append(item)
        return make_simplified_union(items)


def _is_false(item: Type) -> bool:
    """Whether a type is ``Literal[False]``."""
    proper = get_proper_type(item)
    return isinstance(proper, LiteralType) and proper.value is False


# Odoo 17: what reading a field gives, by field class (as Odoo 18 declares them).
CLASS_VALUES: dict[str, tuple[str | bool, ...]] = {
    "Boolean": ("builtins.bool",),
    "Integer": ("builtins.int",),
    "Float": ("builtins.float",),
    "Monetary": ("builtins.float",),
    "_String": ("builtins.str", False),
    "Selection": ("builtins.str", False),
    "Date": ("datetime.date", False),
    "Datetime": ("datetime.datetime", False),
    "Id": ("builtins.int", "new_id", False),
}


def _class_value_get(
    descriptor: Instance, signature: CallableType, profile: OdooProfile, checker: TypeChecker
) -> FunctionLike:
    """The value type of a field class Odoo does not type (17), by the first known class."""
    if not isinstance(get_proper_type(signature.ret_type), AnyType):
        return signature
    found = next(
        (CLASS_VALUES[item.name] for item in descriptor.type.mro if item.name in CLASS_VALUES), None
    )
    if found is None:
        return signature
    items: list[Type] = []
    for part in found:
        if not isinstance(part, str):  # False
            items.append(LiteralType(False, checker.named_type("builtins.bool")))
            continue
        node = lookup_fully_qualified(profile.new_id if part == "new_id" else part, checker.modules)
        if node is None or not isinstance(node.node, TypeInfo):
            return signature
        items.append(instance_of(node.node))
    value = items[0] if len(items) == 1 else UnionType(items)
    return signature.copy_modified(ret_type=value)


def falsy_filter_signature(ctx: FunctionSigContext) -> FunctionLike:
    """``filter(None, [prefix, group.name])`` drops every falsy item, ``False`` too.

    typeshed's overload of a ``None`` function takes ``Iterable[T | None]``: an Odoo value
    that may be ``False`` (``str | Literal[False]``) did not fit when ``T`` comes from
    the context (``" ".join(filter(None, ...))``). That overload takes ``False`` too.
    """
    signature = ctx.default_signature
    if len(signature.arg_types) != 2:
        return signature
    function, iterable = map(get_proper_type, signature.arg_types)
    if not isinstance(function, NoneType) or not isinstance(iterable, Instance):
        return signature
    if len(iterable.args) != 1:
        return signature
    false = LiteralType(False, type_checker(ctx.api).named_type("builtins.bool"))
    items = UnionType.make_union([iterable.args[0], false])
    widened = iterable.copy_modified(args=[items])
    return signature.copy_modified(arg_types=[signature.arg_types[0], widened])
