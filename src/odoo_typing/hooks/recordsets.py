"""Hooks of recordset methods: ``read`` / ``search_read``, ``mapped`` and the like, iteration."""

from __future__ import annotations

from collections.abc import Mapping

from mypy.nodes import (
    Expression,
    ListExpr,
    MemberExpr,
    NameExpr,
    SetExpr,
    StrExpr,
    TempNode,
    TupleExpr,
)
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
    AGGREGATES,
    DATE_CLASSES,
    GRANULARITIES,
    KNOWN_CLASSES,
    ODOO_FIELD_REF,
    did_you_mean,
    group_spec,
    order_fields,
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
        fields = self._fields_of(addon_name, ctx.api)(model)
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

    def check_order(self, method: str, ctx: MethodContext) -> None:
        """``search(domain, order="date desc, id")``: the order and the fields it names.

        A literal or a ``Final`` / ``Literal`` str. Odoo raises for a string that is not an
        order, and for a name that is not a field of the model (on a concrete model: a
        mixin's order names fields of the models using it).
        """
        if "order" not in ctx.callee_arg_names:
            return
        index = ctx.callee_arg_names.index("order")
        receiver = get_proper_type(ctx.type)
        if not ctx.args[index] or not isinstance(receiver, Instance):
            return
        argument = literal_string(ctx.args[index][0], ctx.arg_types[index][0])
        model = self._models.model_of(receiver.type)
        addon_name = self.registry.addon_name_for_path(ctx.api.path)
        if argument is None or not argument.value.strip() or model is None or addon_name is None:
            return
        names = order_fields(argument.value)
        if names is None:
            message = f'{method}(order="{argument.value}") is not an order: Odoo raises'
            ctx.api.fail(message, argument, code=ODOO_FIELD_REF)
            return
        if not self.registry.concrete(model, addon_name):
            return
        fields = self._fields_of(addon_name, ctx.api)(model)
        guarded = self._field_guarded(ctx.api.path)
        for name in names:
            if fields and name not in fields and not guarded(name, argument.line):
                message = f'Odoo model "{model}" has no field "{name}" in {method}(order=...)'
                ctx.api.fail(message + did_you_mean(name, fields), argument, code=ODOO_FIELD_REF)

    def check_field_names(self, method: str, parameter: str, ctx: MethodContext) -> None:
        """``fetch(["a", "b"])`` / ``search_fetch(domain, [...])``: the names are fields.

        The literal strings of a literal list, tuple or set; on a concrete model.
        """
        found = self._literal_items(parameter, ctx)
        if found is None:
            return
        model, fields, items = found
        guarded = self._field_guarded(ctx.api.path)
        for item in items:
            if item.value not in fields and not guarded(item.value, item.line):
                message = f'Odoo model "{model}" has no field "{item.value}" in {method}()'
                ctx.api.fail(message + did_you_mean(item.value, fields), item, code=ODOO_FIELD_REF)

    def check_group_specs(self, method: str, ctx: MethodContext) -> None:
        """``_read_group(domain, ["date:month"], ["amount:sum"])``: the specs and their fields.

        ``groupby`` items are ``field`` or ``field:granularity`` (of a date field);
        ``aggregates`` items are ``__count`` or ``field:function``. Odoo raises for a name
        that is not a field, a granularity or an aggregate function it does not know. The
        ``read_group`` of Odoo 17-19 takes other forms (``"total:sum(amount)"``): only its
        ``groupby`` is checked.
        """
        guarded = self._field_guarded(ctx.api.path)
        specs = "aggregates" in ctx.callee_arg_names
        for parameter in ("groupby", "aggregates") if specs else ("groupby",):
            found = self._literal_items(parameter, ctx)
            if found is None:
                continue
            model, fields, items = found
            for item in items:
                if parameter == "aggregates" and item.value == "__count":
                    continue
                spec = group_spec(item.value)
                problem = None
                if spec is None:
                    problem = f'"{item.value}" in {method}({parameter}=...) is not a specification'
                elif spec.field not in fields:
                    if guarded(spec.field, item.line):
                        continue
                    problem = f'Odoo model "{model}" has no field "{spec.field}" in '
                    problem += f"{method}({parameter}=...)" + did_you_mean(spec.field, fields)
                elif specs:
                    problem = _function_problem(parameter, spec.function, fields[spec.field])
                if problem is not None:
                    ctx.api.fail(problem, item, code=ODOO_FIELD_REF)

    def _literal_items(
        self, parameter: str, ctx: MethodContext
    ) -> tuple[str, Mapping[str, FieldInfo], list[StrExpr]] | None:
        """The model, its fields and the literal strings given for a parameter of a method.

        ``None`` unless the receiver is a concrete model whose fields are known and the
        argument is a literal string, list, tuple or set.
        """
        if parameter not in ctx.callee_arg_names:
            return None
        actuals = ctx.args[ctx.callee_arg_names.index(parameter)]
        receiver = get_proper_type(ctx.type)
        if len(actuals) != 1 or not isinstance(receiver, Instance):
            return None
        value = actuals[0]
        nodes = value.items if isinstance(value, ListExpr | TupleExpr | SetExpr) else [value]
        items = [node for node in nodes if isinstance(node, StrExpr)]
        model = self._models.model_of(receiver.type)
        addon_name = self.registry.addon_name_for_path(ctx.api.path)
        if not items or model is None or addon_name is None:
            return None
        if not self.registry.concrete(model, addon_name):
            return None
        fields = self._fields_of(addon_name, ctx.api)(model)
        return (model, fields, items) if fields else None

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
        fields_of = self._fields_of(addon_name, ctx.api)
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


def _function_problem(parameter: str, function: str | None, field: FieldInfo) -> str | None:
    """Why the part after ``:`` of a ``_read_group`` spec is wrong for its field, if it is."""
    if parameter == "aggregates":
        if function is None:
            return f'Aggregate "{field.name}" has no function ("{field.name}:sum")'
        if function not in AGGREGATES:
            message = f'"{function}" is not an aggregate function'
            return message + did_you_mean(function, AGGREGATES)
        return None
    if function is None or field.field_type not in DATE_CLASSES:
        return None  # a property's own granularity, a custom field class
    if function not in GRANULARITIES:
        message = f'"{function}" is not a granularity of date field "{field.name}"'
        return message + did_you_mean(function, GRANULARITIES)
    return None


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
