"""Hooks of recordset methods: ``read`` / ``search_read``, ``mapped`` and the like, iteration."""

from __future__ import annotations

from functools import partial

from mypy.nodes import Expression, MemberExpr, NameExpr, StrExpr, TempNode
from mypy.plugin import MethodContext
from mypy.types import (
    AnyType,
    Instance,
    LiteralType,
    TupleType,
    Type,
    TypedDictType,
    TypeOfAny,
    UnionType,
    get_proper_type,
)

# isort: split
# After mypy.types: these modules import each other at import time.
from mypy.checker import TypeChecker
from mypy.typeops import make_simplified_union

from odoo_typing.hooks.models import Hooks
from odoo_typing.hooks.nodes import (
    call_argument,
    literal_string,
    literal_strings_argument,
    type_checker,
)
from odoo_typing.methods import is_self_method, keep_receiver
from odoo_typing.references import (
    KNOWN_CLASSES,
    ODOO_FIELD_REF,
    did_you_mean,
    record_paths,
    resolve_path,
)
from odoo_typing.scanner import FieldInfo

__all__ = ["RecordsetHooks"]


class RecordsetHooks(Hooks):
    """Type what recordset methods return; check the field names and paths they are given."""

    def read_result(self, method: str, ctx: MethodContext) -> Type:
        """``read(["a", "b"])`` / ``search_read(fields=[...])``: a list of TypedDicts.

        Each dict has ``id`` and the literal field names, typed as ``convert_to_read``
        returns them (:func:`_read_value`); a name that is not a field is reported
        (Odoo raises ``ValueError``) on a concrete model.
        """
        receiver = get_proper_type(ctx.type)
        names = literal_strings_argument(ctx, "fields", 0 if method == "read" else 1)
        ids_only = _ids_only(call_argument(ctx, "load", 1 if method == "read" else -1))
        if not isinstance(receiver, Instance) or names is None or ids_only is None:
            return ctx.default_return_type
        model = self._models.model_of(receiver.type)
        addon_name = self.registry.addon_name_for_path(ctx.api.path)
        if model is None or addon_name is None:
            return ctx.default_return_type
        fields = self.registry.fields(model, addon_name)
        checker = type_checker(ctx.api)
        items: dict[str, Type] = {"id": checker.named_type("builtins.int")}
        for node in names:
            info = fields.get(node.value)
            if info is None:
                guarded = self._field_guarded(ctx.api.path)(node.value, node.line)
                if self.registry.concrete(model, addon_name) and not guarded:
                    message = f'Odoo model "{model}" has no field "{node.value}" in {method}()'
                    message += did_you_mean(node.value, fields)
                    ctx.api.fail(message, node, code=ODOO_FIELD_REF)
                return ctx.default_return_type
            value = _read_value(info, ids_only, checker)
            items[node.value] = (
                value
                or self._path_type(receiver, node.value, ctx)
                or AnyType(TypeOfAny.special_form)
            )
        fallback = checker.named_type("typing._TypedDict")
        row = TypedDictType(items, set(items), set(), fallback)
        return checker.named_generic_type("builtins.list", [row])

    def record_path(self, method: str, ctx: MethodContext) -> Type:
        """``mapped("a.b")``, ``filtered("a.b")``, ``sorted("a")``, ``grouped("a")``.

        The string is a literal or a ``Final`` / ``Literal`` str (:func:`literal_string`).
        The fields of the string must exist (``odoo-field-ref``, on a concrete model);
        ``mapped`` then returns the records of the last field's comodel, or a list of its
        values, and ``grouped`` a dict from its values to records of the receiver.
        """
        result = (
            keep_receiver(self._profile.base_model, ctx)
            if is_self_method(method)
            else ctx.default_return_type
        )
        receiver = get_proper_type(ctx.type)
        argument = (
            literal_string(ctx.args[0][0], ctx.arg_types[0][0])
            if ctx.args and ctx.args[0]
            else None
        )
        if not isinstance(receiver, Instance) or argument is None:
            return result
        if self._models.model_of(receiver.type) is None:
            return result  # a ``sorted`` / ``mapped`` method of another class
        if not argument.value or not self._check_record_paths(method, receiver, argument, ctx):
            return result
        if method not in {"mapped", "grouped"}:
            return result
        value = self._path_type(receiver, argument.value, ctx)
        if value is None:
            return result
        checker = type_checker(ctx.api)
        if method == "grouped":
            return checker.named_generic_type("builtins.dict", [value, receiver])
        records = get_proper_type(value)
        if isinstance(records, Instance) and records.type.has_base(self._profile.base_model):
            return records
        return checker.named_generic_type("builtins.list", [value])

    def _check_record_paths(
        self, method: str, receiver: Instance, argument: StrExpr, ctx: MethodContext
    ) -> bool:
        """Report the fields of a path argument that do not exist; whether all do."""
        paths, problem = record_paths(method, argument.value, self._profile.sorted_order)
        if problem is not None:
            ctx.api.fail(problem, argument, code=ODOO_FIELD_REF)
            return False
        model = self._models.model_of(receiver.type)
        addon_name = self.registry.addon_name_for_path(ctx.api.path)
        if model is None or addon_name is None or not self.registry.concrete(model, addon_name):
            return True
        fields_of = partial(self.registry.fields, addon_name=addon_name)
        guarded = self._field_guarded(ctx.api.path)
        valid = True
        for path in paths:
            found = resolve_path(path, model, fields_of, f'{method}("{argument.value}")')
            if found.problem is not None:
                if found.missing is None or not guarded(found.missing, argument.line):
                    ctx.api.fail(found.problem, argument, code=ODOO_FIELD_REF)
                valid = False
        return valid

    def _path_type(self, receiver: Instance, path: str, ctx: MethodContext) -> Type | None:
        """The type of ``record.a.b`` for the path ``a.b``; ``None`` if it has errors."""
        expression: Expression = TempNode(receiver, context=ctx.context)
        for name in path.split("."):
            expression = MemberExpr(expression, name)
            expression.set_line(ctx.context)
        checker = type_checker(ctx.api)
        with checker.msg.filter_errors() as errors:
            value = checker.expr_checker.accept(expression)
        return None if errors.has_new_errors() else value

    def iterate(self, ctx: MethodContext) -> Type:
        """``for record in records`` (Odoo 17): each record has the receiver's type."""
        receiver = get_proper_type(ctx.type)
        if not isinstance(receiver, Instance) or not receiver.type.has_base(
            self._profile.base_model
        ):
            return ctx.default_return_type
        return type_checker(ctx.api).named_generic_type("typing.Iterator", [receiver])


def _ids_only(load: Expression | None) -> bool | None:
    """Whether ``read(load=...)`` gives ids only; ``None`` if ``load`` is not literal.

    Odoo reads display names only for ``load="_classic_read"`` (the default).
    """
    if load is None:
        return False
    if isinstance(load, StrExpr):
        return load.value != "_classic_read"
    if isinstance(load, NameExpr) and load.name in {"None", "False"}:
        return True
    return None


def _read_value(info: FieldInfo, ids_only: bool, checker: TypeChecker) -> Type | None:
    """The type ``read()`` gives a field's value, if it differs from reading the field.

    ``Many2one`` is ``(id, display_name)`` (an id with ``load=None``), x2many fields a list
    of ids, ``Reference`` a ``"model,id"`` string; ``Json`` / ``Properties`` / custom
    classes are ``Any``; ``None`` means the type of ``record.field``.
    """
    false = LiteralType(False, checker.named_type("builtins.bool"))
    integer = checker.named_type("builtins.int")
    if info.field_type == "Many2one":
        if ids_only:
            return UnionType([integer, false])  # simplifying would absorb False in int
        pair = TupleType(
            [integer, checker.named_type("builtins.str")], checker.named_type("builtins.tuple")
        )
        return make_simplified_union([pair, false])
    if info.field_type in {"One2many", "Many2many"}:
        return checker.named_generic_type("builtins.list", [integer])
    if info.field_type == "Reference":
        return make_simplified_union([checker.named_type("builtins.str"), false])
    if info.field_type in {"Json", "Properties", "PropertiesDefinition"}:
        return AnyType(TypeOfAny.special_form)
    if info.field_type not in KNOWN_CLASSES:
        return AnyType(TypeOfAny.special_form)
    return None
