"""Hooks of reading a field (``record.name``): comodel records and value types."""

from __future__ import annotations

from typing import TypeGuard

from mypy.lookup import lookup_fully_qualified
from mypy.nodes import (
    Context,
    Expression,
    FuncDef,
    MemberExpr,
    MypyFile,
    NameExpr,
    TypeInfo,
    Var,
)
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
    TypeType,
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
from odoo_typing.hooks.nodes import checked_function, record_type, type_checker
from odoo_typing.model_fields import value_required
from odoo_typing.references import resolve_path
from odoo_typing.registry.scans import IdOriginReads
from odoo_typing.scanner import FieldInfo
from odoo_typing.searched import filtered_fields, restricted_fields, searched_domain
from odoo_typing.vals import RECORDS, VALUE_TYPES, stored, value_type_named

__all__ = ["FILTER", "FieldValueHooks", "falsy_filter_signature"]

FILTER = "builtins.filter"

# Field classes whose reading ``_value_get`` types (``Image`` is a ``Binary``).
VALUE_KINDS = frozenset({"Binary", "Reference", "Selection"})


class FieldValueHooks(Hooks):
    """Type what reading a field gives, as the reading addon sees the models."""

    # Where the module checked last reads ``x.id.origin`` (see saved_id).
    _origin_reads: tuple[MypyFile | None, IdOriginReads] = (None, IdOriginReads())

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
            found = self._required_value(found, ctx)
            return self._expected_keys(found, ctx)
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
        """A field that is set on the records read, without ``False`` (``None`` of a ``Reference``).

        ``required_values``: a required field (and ``display_name``). Odoo reads
        ``False`` for a required field of a new record or of a form whose value is not set
        yet; code that reads saved records does not see it. Also a field the domain of the
        search that found the records sets (:mod:`odoo_typing.searched`). A value that
        types a variable whose later values are checked against it keeps the declared
        type (see :meth:`_stored`).
        """
        member = ctx.context
        records = record_type(ctx)
        if not isinstance(signature, CallableType) or not isinstance(records, Instance):
            return signature
        info = self._read_field(records, ctx)
        name = member.name if isinstance(member, MemberExpr) else None
        required = self._settings.required_values and (
            name == "display_name" or self._value_required(info, records, ctx.api)
        )
        if not required and not self._searched(member, ctx.api):
            return signature
        value = get_proper_type(signature.ret_type)
        if not isinstance(value, UnionType):
            return signature
        reference = info is not None and info.field_type == "Reference"
        kept = [item for item in value.items if not _is_empty(item, reference)]
        if len(kept) == len(value.items) or self._stored(member, ctx.api):
            return signature
        return signature.copy_modified(ret_type=make_simplified_union(kept))

    def _expected_keys(self, signature: FunctionLike, ctx: MethodSigContext) -> FunctionLike:
        """A ``Selection`` read where literal strings that hold its keys are expected.

        ``float_round(amount, precision_rounding=..., rounding_method=self.rounding_method)``:
        Odoo 19+ declares ``rounding_method: Literal["UP", "DOWN", "HALF-UP", ...]``, and the
        field's keys are ``UP``, ``DOWN``, ``HALF-UP``. Read as ``str`` (without
        ``literal_selection``), the value fitted no overload. Where the expression's type
        context is a union of string literals that holds every key of the field, the value
        reads as its keys (and ``False`` where the field may be empty, which is then
        reported); elsewhere it stays ``str``.
        """
        if self._settings.literal_selection or not isinstance(signature, CallableType):
            return signature
        expected = type_checker(ctx.api).expr_checker.type_context[-1]
        accepted = _string_literals(expected)
        records = record_type(ctx)
        if not accepted or not isinstance(records, Instance):
            return signature
        info = self._read_field(records, ctx)
        if info is None or not info.selection or "selection" in info.dynamic:
            return signature
        if not set(info.selection) <= accepted:
            return signature
        value = get_proper_type(signature.ret_type)
        items = [
            get_proper_type(item)
            for item in (value.items if isinstance(value, UnionType) else [value])
        ]
        string = next((item for item in items if _is_str(item)), None)
        if string is None:
            return signature
        keys = [LiteralType(key, string) for key in info.selection]
        kept = [item for item in items if item is not string]
        return signature.copy_modified(ret_type=make_simplified_union([*keys, *kept]))

    def active_name(self, ctx: AttributeContext) -> Type:
        """``records._active_name`` (Odoo 18 leaves ``_active_name = None`` unannotated).

        mypy reads ``None``: ``self.filtered(self._active_name)`` was an error and the
        body of ``if self._active_name:`` unreachable. Odoo sets it up as ``"active"`` or
        ``"x_active"`` when the model has such a field (``_setup_fields``): a ``str`` on
        such a model, ``str | None`` on another one (what Odoo 19 declares).
        """
        default = get_proper_type(ctx.default_attr_type)
        if ctx.is_lvalue or isinstance(ctx.context, NameExpr) or not isinstance(default, NoneType):
            return ctx.default_attr_type
        checker = type_checker(ctx.api)
        text = checker.named_type("builtins.str")
        records = receiver_type(ctx.type)
        if isinstance(records, TypeType):
            records = receiver_type(records.item)
        model = self._models.model_of(records.type) if isinstance(records, Instance) else None
        addon_name = self.registry.addon_name_for_path(ctx.api.path)
        if model is not None and addon_name is not None:
            fields = self._fields_of(addon_name, ctx.api)(model)
            if "active" in fields or "x_active" in fields:
                return text
        return make_simplified_union([text, NoneType()])

    def display_name(self, ctx: AttributeContext) -> Type:
        """``required_values``: ``display_name`` (annotated on ``BaseModel``) without ``False``.

        Not where mypy compares a class's own ``display_name = fields.Char(...)`` with
        ``BaseModel``'s (a name, not an attribute read): the field is as Odoo declares it.
        """
        value = get_proper_type(ctx.default_attr_type)
        if (
            ctx.is_lvalue
            or isinstance(ctx.context, NameExpr)
            or not isinstance(value, UnionType)
            or self._stored(ctx.context, ctx.api)
        ):
            return ctx.default_attr_type
        return make_simplified_union([item for item in value.items if not _is_false(item)])

    def saved_id(self, ctx: AttributeContext) -> Type:
        """``record.id`` without ``NewId``, unless ``new_record_ids``: the id of a saved record.

        Odoo 18 declares ``id: int | NewId | Literal[False]`` (the plugin declares the
        same for Odoo 17): the ``NewId`` is that of a record of ``new()`` or of a form's
        onchange, and with it every ``values[record.id]`` of a ``dict[int, ...]`` and
        every ``id: int`` parameter is an error for the records of ``search``,
        ``browse`` and ``create`` too. Like ``required_values``, the default reads the
        value a saved record has. ``record.id.origin`` is read where the record is a new
        one (a form's record in an onchange, only a ``NewId`` has ``origin``): there the
        id is the ``NewId``.
        """
        value = get_proper_type(ctx.default_attr_type)
        if not isinstance(value, UnionType) or isinstance(ctx.context, NameExpr):
            return ctx.default_attr_type  # a class's own ``id`` compared with BaseModel's
        new_id = self._profile.new_id
        if self._reads_origin(ctx):
            ids = [
                item
                for item in value.items
                if isinstance(proper := get_proper_type(item), Instance)
                and proper.type.fullname == new_id
            ]
            return ids[0] if ids else ctx.default_attr_type
        kept = [
            item
            for item in value.items
            if not (
                isinstance(proper := get_proper_type(item), Instance)
                and proper.type.fullname == new_id
            )
        ]
        if len(kept) == len(value.items):
            return ctx.default_attr_type
        return make_simplified_union(kept)

    def _reads_origin(self, ctx: AttributeContext) -> bool:
        """Whether the ``record.id`` being read is that of ``record.id.origin``."""
        member = ctx.context
        if not isinstance(member, MemberExpr):
            return False
        tree = type_checker(ctx.api).tree
        if self._origin_reads[0] is not tree:
            self._origin_reads = (tree, self.registry.id_origin_reads(tree.path))
        found = self._origin_reads[1]
        position = (member.line, member.column, member.end_column or 0)
        return position in found.positions or member.line in found.lines

    def forget_trees(self) -> None:
        """Forget what was read from a module's statements (each daemon update)."""
        self._origin_reads = (None, IdOriginReads())

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
        of a literal ``selection`` (``BaseModel`` otherwise) or ``None``, not as
        Selection's ``str | Literal[False]``;
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
            # Empty, it reads ``None``, not ``False`` (``Reference.convert_to_record``).
            models = self._reference_types(info, records, ctx.api)
            return signature.copy_modified(ret_type=make_simplified_union([*models, NoneType()]))
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

    def _searched(self, member: Context, api: CheckerPluginInterface) -> bool:
        """Whether the search or the ``filtered("f")`` that gave the records read sets the field."""
        function = checked_function(api)
        if not isinstance(member, MemberExpr) or function is None:
            return False
        if member.name in filtered_fields(function, member.expr):
            return True
        domain = searched_domain(function, member.expr)
        checker = type_checker(api)
        return member.name in restricted_fields(domain, checker.lookup_type_or_none)

    def _value_required(
        self, info: FieldInfo | None, records: Instance, api: CheckerPluginInterface
    ) -> bool:
        """Whether a field read always has a value (:func:`value_required`).

        Not a computed field read in its own compute method, where it reads ``False``
        until the method assigns it, nor in its inverse method, where it holds the value
        written (:mod:`odoo_typing.computed`).
        """
        model = self._models.model_of(records.type)
        addon_name = self.registry.addon_name_for_path(api.path)
        if info is None or model is None or addon_name is None:
            return False
        computed = None if self._computing(info, model, api) else self._computed_of(addon_name, api)
        return value_required(info, model, self._fields_of(addon_name, api), computed)

    def _computing(self, info: FieldInfo, model: str, api: CheckerPluginInterface) -> bool:
        """Whether the function being checked computes or inverses a field of a model."""
        function = checked_function(api)
        if not isinstance(function, FuncDef) or function.name not in {info.compute, info.inverse}:
            return False
        # ``info`` of a function that is no method is mypy's false placeholder.
        return not function.info or self._models.model_of(function.info) in {model, None}

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
        """The record types a ``Reference`` field holds: its selection's models.

        A selection the scan cannot read (a method, ``_selection_target_model``) names
        the models at run time: the records are of a model not known here, ``Any`` as
        for ``env[name]`` of a name made at run time (``dynamic_env = "base"`` keeps
        ``BaseModel``, records of no model, whose every field is an unknown attribute).
        """
        found_base = next(
            (item for item in records.type.mro if item.fullname == self._profile.base_model), None
        )
        base: Type = (
            instance_of(found_base)
            if found_base is not None and self._settings.dynamic_env != "any"
            else AnyType(TypeOfAny.special_form)
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


def _is_empty(item: Type, reference: bool) -> bool:
    """Whether a type is the empty value of a field: ``False``, ``None`` of a ``Reference``."""
    return _is_false(item) or (reference and isinstance(get_proper_type(item), NoneType))


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


def _string_literals(expected: Type | None) -> frozenset[str]:
    """The string literals a type context is a union of (empty if it holds anything else)."""
    proper = get_proper_type(expected) if expected is not None else None
    items = proper.items if isinstance(proper, UnionType) else [proper]
    found: set[str] = set()
    for item in items:
        literal = get_proper_type(item)
        if isinstance(literal, LiteralType) and isinstance(literal.value, str):
            found.add(literal.value)
        elif not isinstance(literal, NoneType):
            return frozenset()
    return frozenset(found)


def _is_str(typ: ProperType) -> TypeGuard[Instance]:
    return isinstance(typ, Instance) and typ.type.fullname == "builtins.str"
