"""Hooks of recordset methods: ``read`` / ``search_read``, ``mapped`` and the like, iteration."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Final

from mypy.literals import literal_hash
from mypy.lookup import lookup_fully_qualified
from mypy.nodes import (
    ARG_POS,
    CallExpr,
    CastExpr,
    ComparisonExpr,
    ConditionalExpr,
    Context,
    DictExpr,
    Expression,
    GeneratorExpr,
    IndexExpr,
    LambdaExpr,
    ListComprehension,
    ListExpr,
    MemberExpr,
    NameExpr,
    OpExpr,
    SetComprehension,
    SetExpr,
    SliceExpr,
    StarExpr,
    StrExpr,
    TempNode,
    TupleExpr,
    TypeInfo,
    UnaryExpr,
)
from mypy.plugin import CheckerPluginInterface, MethodContext, MethodSigContext
from mypy.types import (
    AnyType,
    CallableType,
    FunctionLike,
    Instance,
    LiteralType,
    NoneType,
    Overloaded,
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
from mypy.subtypes import is_subtype
from mypy.typeops import make_simplified_union, true_only
from mypy.typevars import fill_typevars_with_any

from odoo_typing.classes import receiver_type
from odoo_typing.hooks.models import Hooks
from odoo_typing.hooks.nodes import (
    call_argument,
    literal_string,
    literal_strings_argument,
    type_checker,
)
from odoo_typing.methods import is_self_method, keep_receiver
from odoo_typing.model_fields import value_required
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
from odoo_typing.searched import domain_argument, restricted_fields, sql_kinds
from odoo_typing.vals import RELATIONAL_CLASSES

__all__ = ["RECORD_FUNCTIONS", "RecordsetHooks"]

ITERATOR = "typing.Iterator"
FIELDS = "_fields"
# The module that names every field class of Odoo (``odoo.fields.Float``).
FIELDS_MODULE = "odoo.fields"
# The recordset methods that take a field name or a function of one record, and the
# parameter: Odoo 17 and 18 leave it unannotated, 19+ declare ``str | Callable[[Self], ...]``.
RECORD_FUNCTIONS: Final = {"mapped": "func", "filtered": "func", "grouped": "key", "sorted": "key"}


class RecordsetHooks(Hooks):
    """Type what recordset methods return; check the field names and paths they are given."""

    def dispatched(self, info: TypeInfo, method: str) -> dict[str, frozenset[str]]:
        """The parameters of a method of a model class that take records of models it tests.

        From the classes of the MRO that define the method
        (:func:`odoo_typing.scanner.class_bodies.record_dispatches`).
        """
        found: dict[str, set[str]] = {}
        for base in info.mro:
            if method not in base.names:
                continue
            contribution = self.registry.contribution(base.fullname)
            for name, parameter, models in contribution.dispatches if contribution else ():
                if name == method:
                    found.setdefault(parameter, set()).update(models)
        return {parameter: frozenset(models) for parameter, models in found.items()}

    def dispatch_signature(self, method: str, ctx: MethodSigContext) -> FunctionLike:
        """A parameter that the method tests against model names takes their records.

        ``message_post_with_source(source_ref: str | IrUiView)`` (Odoo 20) passes
        ``source_ref`` to ``_get_source_from_ref``, which tests ``source_ref._name ==
        "mail.template"``: Odoo's own code passes a template, and every such call was an
        ``arg-type`` error. The parameter also takes the records of the tested models, as
        the reading addon sees them.
        """
        signature = ctx.default_signature
        receiver = receiver_type(ctx.type)
        if not isinstance(signature, CallableType) or not isinstance(receiver, Instance):
            return signature
        accepted = self.dispatched(receiver.type, method)
        reader = self.registry.addon_name_for_path(ctx.api.path)
        if not accepted or reader is None:
            return signature
        arguments = list(signature.arg_types)
        for position, name in enumerate(signature.arg_names):
            models = accepted.get(name or "", frozenset())
            found = (self._models.env_model(model, reader, ctx.api) for model in sorted(models))
            extra: list[Type] = [records for records in found if records is not None]
            if extra:
                arguments[position] = UnionType.make_union([arguments[position], *extra])
        return signature.copy_modified(arg_types=arguments)

    def read_result(self, method: str, ctx: MethodContext) -> Type:
        """``read(["a", "b"])`` / ``search_read(fields=[...])``: a list of TypedDicts.

        Each dict has ``id`` and the literal field names, typed as ``convert_to_read``
        returns them (:func:`_read_value`); a name that is not a field is reported
        (Odoo raises ``ValueError``) on a concrete model. The keys the function around the
        call writes into dicts are keys of the rows (``Any``), and those it removes
        (``pop``, ``del``) are not required (:mod:`odoo_typing.written_keys`): code
        completes the rows it sends to the client.
        """
        receiver = receiver_type(ctx.type)
        names = literal_strings_argument(ctx, "fields", 0 if method == "read" else 1)
        ids_only = _ids_only(call_argument(ctx, "load", 1 if method == "read" else -1))
        if not isinstance(receiver, Instance) or names is None or ids_only is None:
            return ctx.default_return_type
        model = self._models.model_of(receiver.type)
        addon_name = self.registry.addon_name_for_path(ctx.api.path)
        if model is None or addon_name is None:
            return ctx.default_return_type
        fields_of = self._fields_of(addon_name, ctx.api)
        computed = self._computed_of(addon_name, ctx.api)
        fields = fields_of(model)
        checker = type_checker(ctx.api)
        items: dict[str, Type] = {"id": checker.named_type("builtins.int")}
        call = ctx.context
        domain = (
            domain_argument(call)
            if method == "search_read" and isinstance(call, CallExpr)
            else None
        )
        searched = (
            restricted_fields(domain, checker.lookup_type_or_none, sql_kinds(fields))
            if domain is not None
            else frozenset()
        )
        for node in names:
            info = fields.get(node.value)
            if info is None:
                guarded = self._field_guarded(ctx.api)(
                    node.value, node.line, model
                ) or self._field_ignored(node.value, fields)
                if self.registry.concrete(model, addon_name) and not guarded:
                    message = f'Odoo model "{model}" has no field "{node.value}" in {method}()'
                    message += did_you_mean(node.value, fields)
                    ctx.api.fail(message, node, code=ODOO_FIELD_REF)
                return ctx.default_return_type
            required = (
                self._settings.required_values and value_required(info, model, fields_of, computed)
            ) or node.value in searched
            value = _read_value(info, ids_only, checker, required=required)
            items[node.value] = (
                value
                or self._path_type(receiver, node.value, ctx)
                or AnyType(TypeOfAny.special_form)
            )
        # Code completes the rows it sends to the client: the keys the function writes
        # into dicts are keys of them too, and those it removes are not required.
        written = self.registry.written_keys(ctx.api.path, ctx.context.line)
        required_keys = set(items) - written.removed
        anything = AnyType(TypeOfAny.special_form)
        for key in written.added | written.removed:
            items.setdefault(key, anything)
        fallback = checker.named_type("typing._TypedDict")
        row = TypedDictType(items, required_keys, set(), fallback)
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
        if method == "mapped":
            result = self._mapped_values(result)
        receiver = receiver_type(ctx.type)
        argument = (
            literal_string(ctx.args[0][0], ctx.arg_types[0][0])
            if ctx.args and ctx.args[0]
            else None
        )
        if not isinstance(receiver, Instance):
            return result
        if argument is None:
            return self._function_result(method, receiver, result, ctx)
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
        if isinstance(records, AnyType) and self._relational_end(receiver, argument.value, ctx):
            return value  # records of a comodel mypy cannot type, not a list
        if _filtered_by(ctx.context, argument.value):
            value = true_only(value)  # ``filtered("code").mapped("code")``: no empty code
        return checker.named_generic_type("builtins.list", [value])

    def _function_result(
        self, method: str, receiver: Instance, result: Type, ctx: MethodContext
    ) -> Type:
        """``mapped(lambda line: line.qty)`` / ``grouped(lambda line: ...)`` on Odoo 17, 18.

        Odoo declares no result: it follows from what the function gives, as Odoo 19
        declares (:meth:`function_signature`). ``mapped`` gives the function's records,
        or a list of its values; ``grouped`` a dict from its values to records of the
        receiver. The function's type is the one mypy gave it (a lambda's result is the
        type of its body).
        """
        if method not in {"mapped", "grouped"} or not isinstance(get_proper_type(result), AnyType):
            return result
        given = ctx.args[0] if ctx.args else []
        found = (
            get_proper_type(type_checker(ctx.api).lookup_type_or_none(given[0]))
            if len(given) == 1
            else None
        )
        if not isinstance(found, CallableType) or self._models.model_of(receiver.type) is None:
            return result
        value = get_proper_type(found.ret_type)
        checker = type_checker(ctx.api)
        if method == "grouped":
            return checker.named_generic_type("builtins.dict", [value, receiver])
        if isinstance(value, AnyType):
            return result
        if isinstance(value, Instance) and value.type.has_base(self._profile.base_model):
            return value
        return checker.named_generic_type("builtins.list", [value])

    def _mapped_values(self, declared: Type) -> Type:
        """What ``mapped`` returns where Odoo declares a list or records (Odoo 19+).

        ``list[T] | BaseModel`` fits no parameter that takes a list (``sum(...)``,
        ``", ".join(...)``), has no attribute of records, and makes mypy infer ``T`` from
        that parameter instead of from the function. ``mapped`` returns records only
        when the function (or the field) gives records: then they are those records;
        any other value gives the list, and an unknown one is unknown (``Any``, as
        where Odoo declares nothing).
        """
        union = get_proper_type(declared)
        if not isinstance(union, UnionType):
            return declared
        items = [get_proper_type(item) for item in union.items]
        lists = [
            item
            for item in items
            if isinstance(item, Instance) and item.type.fullname == "builtins.list" and item.args
        ]
        if len(lists) != 1 or len(items) != 2:
            return declared
        value = get_proper_type(lists[0].args[0])
        if isinstance(value, AnyType):
            return AnyType(TypeOfAny.special_form)  # a list or records: not known which
        if isinstance(value, Instance) and value.type.has_base(self._profile.base_model):
            return value
        return lists[0]

    def _relational_end(self, receiver: Instance, path: str, ctx: MethodContext) -> bool:
        """Whether a path ends with a relational field (``mapped`` then gives records).

        Its value is ``Any`` when the comodel is not a literal (``fields.Many2one(CONST)``)
        or an addon redefines the field with its attributes only
        (``currency_id = fields.Many2one(tracking=True)``).
        """
        model = self._models.model_of(receiver.type)
        addon_name = self.registry.addon_name_for_path(ctx.api.path)
        if model is None or addon_name is None:
            return False
        found = resolve_path(path, model, self._fields_of(addon_name, ctx.api), "mapped()")
        return found.field is not None and found.field.field_type in RELATIONAL_CLASSES

    def check_order(self, method: str, ctx: MethodContext) -> None:
        """``search(domain, order="date desc, id")``: the order and the fields it names.

        A literal or a ``Final`` / ``Literal`` str. Odoo raises for a string that is not an
        order, and for a name that is not a field of the model (on a concrete model: a
        mixin's order names fields of the models using it).
        """
        if "order" not in ctx.callee_arg_names:
            return
        index = ctx.callee_arg_names.index("order")
        receiver = receiver_type(ctx.type)
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
        guarded = self._field_guarded(ctx.api)
        for name in names:
            if (
                fields
                and name not in fields
                and not guarded(name, argument.line, model)
                and not self._field_ignored(name, fields)
            ):
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
        guarded = self._field_guarded(ctx.api)
        for item in items:
            if (
                item.value not in fields
                and not guarded(item.value, item.line, model)
                and not self._field_ignored(item.value, fields)
            ):
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
        field_guarded = self._field_guarded(ctx.api)
        specs = "aggregates" in ctx.callee_arg_names
        for parameter in ("groupby", "aggregates") if specs else ("groupby",):
            found = self._literal_items(parameter, ctx)
            if found is None:
                continue
            model, fields, items = found

            def guarded(
                name: str, line: int, model: str, fields: Mapping[str, FieldInfo] = fields
            ) -> bool:
                return field_guarded(name, line, model) or self._field_ignored(name, fields)

            for item in items:
                problem = _spec_problem(method, parameter, item, model, fields, guarded, specs)
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
        receiver = receiver_type(ctx.type)
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
        guarded = self._field_guarded(ctx.api)
        valid = True
        for path in paths:
            found = resolve_path(path, model, fields_of, f'{method}("{argument.value}")')
            if found.problem is not None:
                missing = found.missing
                if missing is None or not (
                    guarded(missing, argument.line, found.model or model)
                    or (not found.near and self._field_ignored(missing))
                ):
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

    def function_signature(
        self,
        method: str,
        then: Callable[[MethodSigContext], FunctionLike] | None,
        ctx: MethodSigContext,
    ) -> FunctionLike:
        """The function a recordset method takes: of one record of the receiver.

        Odoo 17 and 18 declare no type for it (``def mapped(self, func)``), so the
        parameter of ``filtered(lambda line: ...)`` was ``Any`` and nothing in the lambda
        was checked. Odoo calls it with each record of ``self`` (``func(rec)``) and takes a
        field name instead: the parameter takes ``str | Callable[[record], object]`` (also
        ``None`` for ``sorted``), as Odoo 19 declares. What ``mapped`` and ``grouped`` give
        then follows from what the function gave (:meth:`record_path`). On Odoo 19+ the
        declaration stands; ``filtered`` there may return any value (see
        :meth:`filtered_signature`).

        Kept as declared (the lambda's parameter ``Any``): records of no known model
        (``BaseModel`` of an ``env[...]`` the addon cannot see, every field would be an
        unknown attribute); a lambda with more parameters (``lambda line, move=move:``);
        and a lambda that reads an attribute the code around it narrowed
        (``if rec.date_from: lines.filtered(lambda l: l.date >= rec.date_from)``): mypy
        checks a lambda that has a type in a scope of its own, without the narrowings of
        attributes around it. For these two, the function takes ``Any`` on Odoo 19+ too.
        """
        signature = then(ctx) if then is not None else ctx.default_signature
        if method == "filtered":
            signature = self.filtered_signature(signature, ctx)
        records = receiver_type(ctx.type)
        name = RECORD_FUNCTIONS[method]
        if not isinstance(records, Instance) or not records.type.has_base(self._profile.base_model):
            return signature
        given = ctx.args[0] if ctx.args else []
        checker = type_checker(ctx.api)
        if any(
            isinstance(item, LambdaExpr)
            and (item.arg_kinds != [ARG_POS] or _reads_narrowed(item, checker))
            for item in given
        ):
            return _untyped_function(signature, name)  # also where Odoo 19+ declares it
        if (
            not isinstance(signature, CallableType)
            or self._models.model_of(records.type) is None  # records of no known model
            or name not in signature.arg_names
        ):
            return signature
        index = signature.arg_names.index(name)
        declared = get_proper_type(signature.arg_types[index])
        if not (isinstance(declared, AnyType) and declared.type_of_any == TypeOfAny.unannotated):
            return signature
        api = ctx.api
        anything = api.named_generic_type("builtins.object", [])
        function = api.named_generic_type("builtins.function", [])
        func = CallableType([ctx.type], [ARG_POS], [None], anything, function)
        items: list[Type] = [
            api.named_generic_type("builtins.str", []),
            func,
            *([NoneType()] if method == "sorted" else []),
        ]
        arg_types = list(signature.arg_types)
        arg_types[index] = UnionType(items)
        return signature.copy_modified(arg_types=arg_types)

    def filtered_signature(self, signature: FunctionLike, ctx: MethodSigContext) -> FunctionLike:
        """``filtered(lambda r: r.partner_id)`` (Odoo 19+): the function may return any value.

        Odoo declares ``func: str | Callable[[Self], bool] | Domain`` but keeps the records
        whose result is truthy, and a field value is the usual result; ``bool`` there made
        two errors of each such call (the lambda's ``return-value``, the ``arg-type``).
        """
        receiver = receiver_type(ctx.type)
        if (
            not isinstance(signature, CallableType)
            or not signature.arg_types
            or not isinstance(receiver, Instance)
            or not receiver.type.has_base(self._profile.base_model)
        ):
            return signature
        anything = ctx.api.named_generic_type("builtins.object", [])
        func = get_proper_type(signature.arg_types[0])
        items = func.items if isinstance(func, UnionType) else [func]
        widened = [_any_result(item, anything) for item in items]
        if all(new is old for new, old in zip(widened, items, strict=True)):
            return signature
        new = UnionType.make_union(widened) if isinstance(func, UnionType) else widened[0]
        return signature.copy_modified(arg_types=[new, *signature.arg_types[1:]])

    @staticmethod
    def _named(fullname: str, api: CheckerPluginInterface) -> Instance | None:
        """A class by full name, if mypy has it (``BaseModel``)."""
        checker = type_checker(api)
        node = lookup_fully_qualified(fullname, checker.modules, raise_on_missing=False)
        info = node.node if node is not None else None
        return Instance(info, []) if isinstance(info, TypeInfo) else None

    def iterate(self, ctx: MethodContext) -> Type:
        """``for record in records`` (Odoo 17): each record has the receiver's type."""
        receiver = receiver_type(ctx.type)
        if not isinstance(receiver, Instance) or not receiver.type.has_base(
            self._profile.base_model
        ):
            return ctx.default_return_type
        return type_checker(ctx.api).named_generic_type("typing.Iterator", [receiver])

    def declared_field(self, ctx: MethodContext) -> Type:
        """``records._fields["price_unit"]``: the class of that field, not ``Field``.

        Odoo declares ``_fields`` as a dict of ``Field``: what a field class has
        (``Float.get_digits()``, ``Selection._description_selection()``) was an unknown
        attribute of the base class. A literal name of a field of the model gives that
        field's class, as the merged definitions of the model leave it.
        """
        default = ctx.default_return_type
        declared = get_proper_type(default)
        if not isinstance(declared, Instance) or declared.type.fallback_to_any:
            return default  # Odoo 17, 18: ``Field`` has a dynamic base, any attribute reads
        index = ctx.context
        if not isinstance(index, IndexExpr) or not isinstance(index.index, StrExpr):
            return default
        base = index.base
        if not isinstance(base, MemberExpr) or base.name != FIELDS:
            return default
        records = receiver_type(type_checker(ctx.api).lookup_type_or_none(base.expr))
        model = self._models.model_of(records.type) if isinstance(records, Instance) else None
        reader = self.registry.addon_name_for_path(ctx.api.path)
        if model is None or reader is None:
            return default
        info = self.registry.fields(model, reader).get(index.index.value)
        if info is None:
            return default
        name = info.field_type if "." in info.field_type else f"{FIELDS_MODULE}.{info.field_type}"
        checker = type_checker(ctx.api)
        found = lookup_fully_qualified(name, checker.modules, raise_on_missing=False)
        node = found.node if found is not None else None
        if not isinstance(node, TypeInfo):
            return default
        field = fill_typevars_with_any(node)
        if not is_subtype(field, default):
            return default
        if info.selection is not None or info.selection_method is not None:
            # Odoo declares ``selection`` as a list, a method name or a callable, and a
            # field of literal keys holds the list: ``dict(field.selection)`` was an
            # error. What the field holds is not in its class.
            return AnyType(TypeOfAny.special_form)
        return field

    def item(
        self,
        view: Callable[[Callable[[MethodContext], Type] | None, MethodContext], Type] | None,
        hook: Callable[[MethodContext], Type] | None,
        ctx: MethodContext,
    ) -> Type:
        """``records[key]`` with a key of type ``Any`` is ``Any``.

        Odoo declares ``__getitem__(int | slice) -> Self`` and ``(str) -> Any``; mypy takes
        the first for an ``Any`` key, since it sees ``Any`` as the same result, and
        ``record[record._sequence_field]`` (a private attribute the plugin reads as
        ``Any``, see :mod:`odoo_typing.hooks.meta_attributes`) was records, not the value
        of the field it names.
        """
        key = ctx.arg_types[0] if ctx.arg_types else []
        if len(key) == 1 and isinstance(get_proper_type(key[0]), AnyType):
            return AnyType(TypeOfAny.special_form)
        if view is not None:
            return view(hook, ctx)
        return hook(ctx) if hook is not None else ctx.default_return_type

    def own_view(self, hook: Callable[[MethodContext], Type] | None, ctx: MethodContext) -> Type:
        """Records of a class of the reading addon's model as the addon sees the model.

        ``self`` in a method is the class that defines it; the addon's records of the
        model (``env["account.move"]``, a field's comodel) are its last class, which an
        addon with several classes of the model (``account.move`` in two files) defines
        later. At run time both are the registry class. ``self.filtered(...)``,
        ``self.with_context(...)`` and ``for line in self`` gave the defining class:
        a method of a later class of the addon was an unknown attribute, and such
        records did not fit a dict keyed by the addon's records or ``concat()`` of them.
        What a ``-> Self`` method returns and what iterating gives is the addon's view
        when that view extends the receiver's class.
        """
        result = hook(ctx) if hook is not None else ctx.default_return_type
        reader = self.registry.addon_name_for_path(ctx.api.path)
        if reader is None:
            return result
        return self._viewed(get_proper_type(result), reader, ctx, outer=True)

    def _viewed(self, typ: Type, reader: str, ctx: MethodContext, outer: bool) -> Type:
        proper = get_proper_type(typ)
        if isinstance(proper, UnionType):
            items = [self._viewed(item, reader, ctx, outer) for item in proper.items]
            return UnionType.make_union(items, proper.line, proper.column)
        if not isinstance(proper, Instance):
            return typ
        model = self._models.model_of(proper.type)
        if model is None:
            if outer and proper.type.fullname == ITERATOR and len(proper.args) == 1:
                return proper.copy_modified(
                    args=[self._viewed(proper.args[0], reader, ctx, outer=False)]
                )
            return typ
        view = self._models.view_type(model, reader, ctx.api)
        if (
            isinstance(view, Instance)
            and view.type is not proper.type
            and view.type.has_base(proper.type.fullname)
        ):
            return view
        return typ


def _any_result(item: Type, anything: Type) -> Type:
    """A ``Callable[..., bool]`` as ``Callable[..., object]``; another type as it is."""
    proper = get_proper_type(item)
    if not isinstance(proper, CallableType):
        return item
    result = get_proper_type(proper.ret_type)
    if isinstance(result, Instance) and result.type.fullname == "builtins.bool":
        return proper.copy_modified(ret_type=anything)
    return item


def _spec_problem(
    method: str,
    parameter: str,
    item: StrExpr,
    model: str,
    fields: Mapping[str, FieldInfo],
    guarded: Callable[[str, int, str], bool],
    specs: bool,
) -> str | None:
    """Why one ``groupby`` / ``aggregates`` item of ``_read_group`` is wrong, if it is.

    ``specs``: the method takes ``aggregates`` (the part after ``:`` is then checked).
    """
    if parameter == "aggregates" and item.value == "__count":
        return None
    spec = group_spec(item.value)
    if spec is None:
        return f'"{item.value}" in {method}({parameter}=...) is not a specification'
    if spec.field not in fields:
        if guarded(spec.field, item.line, model):
            return None
        message = f'Odoo model "{model}" has no field "{spec.field}" in {method}({parameter}=...)'
        return message + did_you_mean(spec.field, fields)
    return _function_problem(parameter, spec.function, fields[spec.field]) if specs else None


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


def _read_value(
    info: FieldInfo, ids_only: bool, checker: TypeChecker, *, required: bool = False
) -> Type | None:
    """The type ``read()`` gives a field's value, if it differs from reading the field.

    ``Many2one`` is ``(id, display_name)`` (an id with ``load=None``), x2many fields a list
    of ids, ``Reference`` a ``"model,id"`` string; ``Json`` / ``Properties`` / custom
    classes are ``Any``; ``None`` means the type of ``record.field``. A ``required`` field
    (``required_values``) is never ``False``, as reading it is not.
    """
    false = LiteralType(False, checker.named_type("builtins.bool"))
    integer = checker.named_type("builtins.int")
    if info.field_type == "Many2one":
        if ids_only:
            # Not simplified: a union would absorb ``False`` in ``int``.
            return integer if required else UnionType([integer, false])
        pair = TupleType(
            [integer, checker.named_type("builtins.str")], checker.named_type("builtins.tuple")
        )
        return pair if required else make_simplified_union([pair, false])
    if info.field_type in {"One2many", "Many2many"}:
        return checker.named_generic_type("builtins.list", [integer])
    if info.field_type == "Reference":
        text = checker.named_type("builtins.str")
        return text if required else make_simplified_union([text, false])
    if info.field_type in {"Json", "Properties", "PropertiesDefinition"}:
        return AnyType(TypeOfAny.special_form)
    if info.field_type not in KNOWN_CLASSES:
        return AnyType(TypeOfAny.special_form)
    return None


def _filtered_by(call: Context, field: str) -> bool:
    """Whether a call is made on ``records.filtered(field)`` of one field (no path).

    ``filtered("default_code")`` keeps the records whose field is set, so ``mapped`` of the
    same field gives no empty value. Through a relational field the values of the other
    records it holds are mapped too, so a path does not count.
    """
    if "." in field or not isinstance(call, CallExpr) or not isinstance(call.callee, MemberExpr):
        return False
    receiver = call.callee.expr
    return (
        isinstance(receiver, CallExpr)
        and isinstance(receiver.callee, MemberExpr)
        and receiver.callee.name == "filtered"
        and len(receiver.args) == 1
        and isinstance(receiver.args[0], StrExpr)
        and receiver.args[0].value == field
    )


def _untyped_function(signature: FunctionLike, name: str) -> FunctionLike:
    """A signature whose parameter ``name`` takes anything (``Any``), in each overload.

    mypy then checks a lambda given to it where it is written, with the narrowings of
    the code around it (a lambda with a type is checked in a scope of its own).
    """
    items = signature.items if isinstance(signature, Overloaded) else [signature]
    found: list[CallableType] = []
    for item in items:
        if not isinstance(item, CallableType):
            return signature
        if name not in item.arg_names:
            found.append(item)
            continue
        arg_types = list(item.arg_types)
        arg_types[item.arg_names.index(name)] = AnyType(TypeOfAny.special_form)
        found.append(item.copy_modified(arg_types=arg_types))
    return Overloaded(found) if isinstance(signature, Overloaded) else found[0]


def _reads_narrowed(function: LambdaExpr, checker: TypeChecker) -> bool:
    """Whether a lambda reads an attribute of a name of its scope that the code narrowed.

    ``if record.vat: ...filtered(lambda c: c.code == record.vat[:2])``: mypy keeps the
    narrowing of ``record.vat`` in a lambda it checks without a type context, not in one
    it checks with one (a scope of its own).
    """
    own = set(function.arg_names)
    pending: list[Expression] = [function.expr()]
    while pending:
        node = pending.pop()
        if isinstance(node, MemberExpr):
            root: Expression = node
            while isinstance(root, MemberExpr):
                root = root.expr
            if (
                isinstance(root, NameExpr)
                and root.name not in own
                and literal_hash(node) is not None
                and checker.binder.get(node) is not None
            ):
                return True
            pending.append(node.expr)
        else:
            pending.extend(_children(node))
    return False


def _children(node: Expression) -> list[Expression]:
    """The expressions directly in an expression (the forms a lambda's body usually has)."""
    if isinstance(node, CallExpr):
        return [node.callee, *node.args]
    if isinstance(node, OpExpr):
        return [node.left, node.right]
    if isinstance(node, ComparisonExpr):
        return list(node.operands)
    if isinstance(node, UnaryExpr | StarExpr | CastExpr):
        return [node.expr]
    if isinstance(node, IndexExpr):
        return [node.base, node.index]
    if isinstance(node, ConditionalExpr):
        return [node.cond, node.if_expr, node.else_expr]
    if isinstance(node, ListExpr | TupleExpr | SetExpr):
        return list(node.items)
    if isinstance(node, DictExpr):
        return [part for key, value in node.items for part in (key, value) if part is not None]
    if isinstance(node, SliceExpr):
        return [part for part in (node.begin_index, node.end_index, node.stride) if part]
    if isinstance(node, GeneratorExpr | ListComprehension | SetComprehension):
        generator = node if isinstance(node, GeneratorExpr) else node.generator
        return [
            generator.left_expr,
            *generator.sequences,
            *(condition for conditions in generator.condlists for condition in conditions),
        ]
    if isinstance(node, LambdaExpr):
        return [node.expr()]
    return []
