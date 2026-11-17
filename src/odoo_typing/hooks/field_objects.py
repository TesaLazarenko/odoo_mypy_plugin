"""Hooks of the attributes of field objects (``records._fields["name"].size``)."""

from __future__ import annotations

from mypy.nodes import Expression, IndexExpr, MemberExpr, NameExpr, StrExpr, TypeInfo, Var
from mypy.plugin import AttributeContext, CheckerPluginInterface
from mypy.types import Instance, NoneType, Type, UnionType, get_proper_type

# isort: split
# After mypy.types: these modules import each other at import time.
from mypy.subtypes import is_subtype
from mypy.typeops import make_simplified_union

from odoo_typing.classes import receiver_type
from odoo_typing.field_setup import FieldSetup
from odoo_typing.hooks.models import Hooks, Models
from odoo_typing.hooks.nodes import checked_function, type_checker
from odoo_typing.model_fields import literal_argument
from odoo_typing.rebound import function_bindings
from odoo_typing.scanner import FieldInfo

__all__ = ["FieldObjectHooks"]

FIELDS = "_fields"
BUILTINS = "builtins"


class FieldObjectHooks(Hooks):
    """Type the attributes of a model's field objects by the definitions of the field."""

    def __init__(self, models: Models) -> None:
        super().__init__(models)
        self._setup = FieldSetup()

    def attribute(self, ctx: AttributeContext) -> Type:
        """An attribute that holds an argument of the field's definition, without ``None``.

        ``journal._fields["code"].size`` of ``code = fields.Char(size=5)``: Odoo declares
        ``size`` with ``None``, the value of a field that passes none, and the field
        object holds the arguments of its definitions (:mod:`odoo_typing.field_setup`).
        Where the merged definitions of the field, as the reading addon sees them, pass
        the argument of the attribute's name as a true literal, the attribute has the
        literal's type: ``int`` for a declared ``None`` or ``int | None``. The field is
        named by a literal (``_fields["code"]``), also through a local bound to it once.
        """
        default = ctx.default_attr_type
        member = ctx.context
        holder = get_proper_type(ctx.type)
        declared = get_proper_type(default)
        if (
            ctx.is_lvalue
            or not isinstance(member, MemberExpr)
            or not isinstance(holder, Instance)
            or not holder.type.has_base(self._profile.field_base)
            or not _holds_none(declared)
        ):
            return default
        info = self._field_read(member.expr, ctx.api)
        kind = literal_argument(info, member.name) if info is not None else None
        if kind is None or member.name in self._rewritten(holder.type, ctx.api):
            return default
        literal = type_checker(ctx.api).named_type(f"{BUILTINS}.{kind}")
        if isinstance(declared, NoneType):
            return literal  # ``size = None`` without an annotation (Odoo 17-19)
        assert isinstance(declared, UnionType)
        kept = [item for item in declared.items if not _is_none(item)]
        if not any(is_subtype(literal, item) for item in kept):
            return default  # a literal of another type than the attribute is declared with
        return make_simplified_union(kept)

    def _rewritten(self, field_class: TypeInfo, api: CheckerPluginInterface) -> frozenset[str]:
        """The attributes that the setup of a field class and of its bases rewrites."""
        modules = type_checker(api).modules
        base = self._profile.field_base
        names: set[str] = set()
        for module in {item.module_name for item in field_class.mro if item.has_base(base)}:
            tree = modules.get(module)
            if tree is not None:
                names |= self._setup.rewritten(tree.path)
        return frozenset(names)

    def _field_read(self, node: Expression, api: CheckerPluginInterface) -> FieldInfo | None:
        """The field of ``records._fields["name"]``, also of a local bound to it once."""
        if isinstance(node, NameExpr) and isinstance(node.node, Var):
            function = checked_function(api)
            bindings = function_bindings(function).names.get(node.node, []) if function else []
            if len(bindings) != 1 or bindings[0][1] is None:
                return None
            node = bindings[0][1]
        if not isinstance(node, IndexExpr) or not isinstance(node.index, StrExpr):
            return None
        base = node.base
        if not isinstance(base, MemberExpr) or base.name != FIELDS:
            return None
        records = receiver_type(type_checker(api).lookup_type_or_none(base.expr))
        model = self._models.model_of(records.type) if isinstance(records, Instance) else None
        reader = self.registry.addon_name_for_path(api.path)
        if model is None or reader is None:
            return None
        return self._fields_of(reader, api)(model).get(node.index.value)


def _is_none(typ: Type) -> bool:
    return isinstance(get_proper_type(typ), NoneType)


def _holds_none(declared: Type) -> bool:
    """``None`` or a union with it."""
    if isinstance(declared, UnionType):
        return any(_is_none(item) for item in declared.items)
    return isinstance(declared, NoneType)
