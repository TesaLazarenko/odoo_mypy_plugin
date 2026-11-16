"""The private class attributes of model classes (``_inherit``, ``_order``, ``_name``...).

Semantic analysis: their declared types are relaxed so that the overrides Odoo classes
make freely are not errors once ``_inherit`` parents are real bases.
"""

from __future__ import annotations

from mypy.nodes import (
    MDEF,
    AssignmentStmt,
    CallExpr,
    Expression,
    NameExpr,
    RefExpr,
    SymbolTableNode,
    TypeInfo,
    Var,
)
from mypy.plugin import ClassDefContext
from mypy.types import (
    AnyType,
    Instance,
    LiteralType,
    Type,
    TypeOfAny,
    UnionType,
    get_proper_type,
)

from odoo_typing.classes import instance_of
from odoo_typing.compat import OdooProfile

__all__ = ["normalize_meta_attributes"]

DICT = "builtins.dict"


def normalize_meta_attributes(ctx: ClassDefContext, profile: OdooProfile) -> None:
    """Relax the private class attributes of model classes (``_inherit``, ``_order``...).

    Odoo classes override such configuration attributes freely: ``_inherit = "x"``
    where a base used a list, ``_sequence_index = "journal_id"`` where
    ``sequence.mixin`` has ``False``, or a property where a mixin has a plain string.
    Once ``_inherit`` parents are real bases, mypy would report each of these. So a
    private attribute assigned without annotation gets the type a model base class
    declares for it explicitly (``BaseModel._inherit: str | list[str] | ...``), and
    ``Any`` otherwise. ``_name`` is a ``str`` in every model class. Private fields
    (``_x = fields.Char()``) keep their type unless ``BaseModel`` declares the name.
    """
    info = ctx.cls.info
    if info.fullname == profile.base_model or not info.has_base(profile.base_model):
        return
    if profile.untyped_fields:
        _relax_untyped_base_attributes(info, profile)
        _declare_untyped_attributes(ctx, profile)
    for statement in ctx.cls.defs.body:
        target = _private_attribute(statement)
        if target is None or not isinstance(statement, AssignmentStmt):
            continue
        declared = _meta_attribute_type(info, target.name, statement.rvalue, profile)
        if target.name == "_name":
            # Only BaseModel itself has ``_name = None``; in a model class the name is
            # a str, and ``str | None`` would reject ``_inherit = _name``. Every class
            # gets the same narrowed type, so the classes stay compatible.
            declared = ctx.api.named_type("builtins.str")
        if declared is None:
            continue
        # Pretend the user wrote ``_inherit: <declared type> = ...``; otherwise the
        # checker re-infers the type from the right-hand side.
        statement.type = declared
        target.is_inferred_def = False
        if isinstance(target.node, Var):
            target.node.type = declared
            target.node.is_inferred = False


def _relax_untyped_base_attributes(info: TypeInfo, profile: OdooProfile) -> None:
    """Odoo 17: make the unannotated private attributes of Odoo's model classes ``Any``.

    ``BaseModel._name = None`` is inferred as ``None`` there (18+ declare
    ``_name: str | None``), so every ``_name = "x"`` would be an incompatible override.
    """
    module = profile.base_model.rpartition(".")[0]
    for base in info.mro[1:]:
        if base.module_name != module:
            continue
        for name, node in base.names.items():
            var = node.node
            private = name.startswith("_") and not name.startswith("__")
            if private and isinstance(var, Var) and var.is_inferred:
                var.type = AnyType(TypeOfAny.special_form)


def _declare_untyped_attributes(ctx: ClassDefContext, profile: OdooProfile) -> None:
    """Odoo 17: declare what Odoo 18 annotates and 17 only sets at run time.

    ``BaseModel.id`` / ``display_name`` / ``_fields`` (added by the metaclass) and the
    attributes ``Environment.__new__`` assigns (``cr``, ``uid``, ``context``...), with
    Odoo 18's types where they are simple, else ``Any``.
    """
    api = ctx.api
    false = LiteralType(False, api.named_type("builtins.bool"))
    text = api.named_type("builtins.str")
    anything = AnyType(TypeOfAny.special_form)
    new_id = api.lookup_fully_qualified_or_none(profile.new_id)
    if new_id is None or not isinstance(new_id.node, TypeInfo):
        return
    record_id = UnionType([api.named_type("builtins.int"), instance_of(new_id.node), false])
    declarations: dict[str, dict[str, Type]] = {
        profile.base_model: {
            "id": record_id,
            "display_name": UnionType([text, false]),
            "_fields": api.named_type("builtins.dict", [text, anything]),
        },
        profile.environment: {
            "cr": anything,
            "uid": api.named_type("builtins.int"),
            "context": api.named_type("builtins.dict", [text, anything]),
            "su": api.named_type("builtins.bool"),
            "registry": anything,
            "cache": anything,
            "transaction": anything,
        },
    }
    for owner, attributes in declarations.items():
        node = api.lookup_fully_qualified_or_none(owner)
        if node is None or not isinstance(node.node, TypeInfo):
            continue
        for name, value in attributes.items():
            if name in node.node.names:
                continue
            var = Var(name, value)
            var.info = node.node
            var._fullname = f"{owner}.{name}"
            node.node.names[name] = SymbolTableNode(MDEF, var, plugin_generated=True)


def _private_attribute(statement: object) -> NameExpr | None:
    """The target of ``_x = value`` (single name, no annotation, not a dunder)."""
    # ``unanalyzed_type``, not ``type``: semantic analysis already sets ``type`` for
    # literals (``_x = False`` is ``bool``) where no base class defines the name.
    if not isinstance(statement, AssignmentStmt) or statement.unanalyzed_type is not None:
        return None
    if len(statement.lvalues) != 1 or not isinstance(statement.lvalues[0], NameExpr):
        return None
    target = statement.lvalues[0]
    private = target.name.startswith("_") and not target.name.startswith("__")
    return target if private else None


def _meta_attribute_type(
    info: TypeInfo, name: str, value: Expression, profile: OdooProfile
) -> Type | None:
    """The type a private class attribute gets (see normalize_meta_attributes).

    Returns:
        ``None`` to leave the attribute alone: a base defines it as a method or
        property, or the value is a field that no ``BaseModel`` declaration covers.
    """
    for base in info.mro[1:]:
        node = base.names.get(name)
        if node is None:
            continue
        if not isinstance(node.node, Var):
            return None
        if node.node.type is not None and not node.node.is_inferred:
            return _given(node.node.type)  # annotated (or normalized) on a base class
        if base.fullname == profile.base_model:
            return AnyType(TypeOfAny.special_form)
        break
    return None if _is_field_definition(value, profile) else AnyType(TypeOfAny.special_form)


def _given(declared: Type) -> Type:
    """What a model class may give for an attribute a base class declares.

    Odoo 19 declares ``_inherits: frozendict[str, str]``, the type Odoo builds from
    what the class gives: a plain ``{"res.partner": "partner_id"}``, which is no
    ``frozendict``. An attribute declared with a subclass of ``dict`` takes anything.
    """
    proper = get_proper_type(declared)
    if isinstance(proper, Instance) and proper.type.fullname != DICT and proper.type.has_base(DICT):
        return AnyType(TypeOfAny.special_form)
    return declared


def _is_field_definition(value: Expression, profile: OdooProfile) -> bool:
    """``fields.Char(...)`` and the like (f-strings are calls too, in mypy's tree)."""
    callee = value.callee if isinstance(value, CallExpr) else None
    return isinstance(callee, RefExpr) and profile.is_field_class(callee.fullname)
