"""Hooks that check domains: where a domain goes, and what model it must be of.

The consumers of a domain (each checks it with :meth:`DomainHooks._check`):

* ``search([...])`` and the other methods of :data:`~odoo_typing.domains.DOMAIN_METHODS`
  on records, ``name_search`` / ``_name_search`` / ``_read_grouping_sets`` (their domain
  parameter by name): a domain of the receiver's model;
* the wrapper of a ``return`` or an annotated assignment (:mod:`odoo_typing.annotations`):
  a domain of the annotation's model; of a field's search method: of its model;
* an argument of a parameter annotated ``odoo.domain.<model>`` (of a method found in the
  receiver class's MRO, or of a function);
* the value of an ``any`` term: a domain of the field's comodel;
* ``domain=`` of a relational field: a domain of the comodel.

The parts of the domain (:mod:`odoo_typing.domain_parts`) are checked: literal lists by
:class:`~odoo_typing.domains.DomainChecker` (their structure in any addon file, their
terms against a known model), typed parts by their model, which must be the consumer's,
one of its parents or mixins, or a model it delegates to (``odoo-domain-model``).

On Odoo 19+, the parameters Odoo annotates ``DomainType`` take ``list[Any]`` for their
list member (:meth:`DomainHooks.widen_signature`): mypy infers a variable built from a
literal as ``list[tuple[str, str, str]]``, and ``list`` is invariant.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from functools import partial

from mypy.errorcodes import ErrorCode
from mypy.messages import format_type_bare
from mypy.nodes import (
    ARG_POS,
    CallExpr,
    CastExpr,
    Context,
    Expression,
    LambdaExpr,
    MemberExpr,
    NameExpr,
    RefExpr,
    StrExpr,
    Var,
)
from mypy.plugin import (
    CheckerPluginInterface,
    FunctionContext,
    FunctionSigContext,
    MethodContext,
    MethodSigContext,
)
from mypy.types import (
    AnyType,
    CallableType,
    FunctionLike,
    Instance,
    ProperType,
    Type,
    TypeOfAny,
    TypeType,
    TypeVarId,
    TypeVarType,
    UnionType,
    get_proper_type,
)

# isort: split
# After mypy.types: mypy.checker and mypy.types import each other at import time.
from mypy.checker import TypeChecker

from odoo_typing.annotations import ASSIGN, ASSIGN_BUILT, RETURN, SEARCH, SEARCH_ALIAS
from odoo_typing.classes import domain_type_model
from odoo_typing.domain_parts import DomainParts, TypedPart
from odoo_typing.domains import (
    ODOO_DOMAIN_MODEL,
    SEARCH_CONSUMERS,
    DomainChecker,
    Mode,
    Term,
    string_domain_paths,
)
from odoo_typing.hooks.models import Hooks, Models
from odoo_typing.hooks.nodes import field_arguments, type_checker
from odoo_typing.hooks.recordsets import RecordsetHooks
from odoo_typing.methods import is_self_method, keep_receiver
from odoo_typing.references import ORDER_METHODS, READ_GROUP_METHODS
from odoo_typing.scanner import FieldInfo

__all__ = ["DomainHooks", "domain_parameters", "has_domain_type", "wrapper_signature"]

WRAPPED_KINDS = frozenset({RETURN, ASSIGN, ASSIGN_BUILT, SEARCH, SEARCH_ALIAS})
# How many ``any`` sub-domains deep a domain is checked (a sub-domain may be a variable
# whose value holds it again).
MAX_SUBDOMAIN_DEPTH = 5

AnyContext = MethodContext | FunctionContext


@dataclass(frozen=True, slots=True)
class Consumer:
    """Where a domain goes.

    Attributes:
        addon: The addon of the checked file.
        model: The model the domain must be of; ``None`` if not known (an abstract or
            unknown receiver): then only the structure of its literals is checked.
        mode: Which rules its terms are checked with.
        mismatch: The message for a domain of another model (given that model).
        whole: Whether the expression is a whole domain.
    """

    addon: str
    model: str | None
    mode: Mode
    mismatch: Callable[[str], str]
    whole: bool = True


class DomainHooks(Hooks):
    """Check the domains of calls, annotated expressions and relational field definitions."""

    def __init__(self, models: Models, recordsets: RecordsetHooks) -> None:
        super().__init__(models)
        self._recordsets = recordsets

    # -- consumers ---------------------------------------------------------------------------

    def check(self, method: str, ctx: MethodContext) -> Type:
        """Check the ``domain`` of ``search`` / ``search_count`` / ``_read_group``...

        Its terms only on a concrete model: a mixin's domains name fields of the models
        using it; its structure on any.
        """
        index = ctx.callee_arg_names.index("domain") if "domain" in ctx.callee_arg_names else 0
        domain = ctx.args[index][0] if len(ctx.args) > index and ctx.args[index] else None
        if domain is not None:
            self._check_receiver_domain(method, domain, ctx)
        if method in ORDER_METHODS:
            self._recordsets.check_order(method, ctx)
        if method in READ_GROUP_METHODS:
            self._recordsets.check_group_specs(method, ctx)
        if method == "search_fetch":
            self._recordsets.check_field_names(method, "field_names", ctx)
        if method == "search_read":
            return self._recordsets.read_result(method, ctx)
        if is_self_method(method):
            return keep_receiver(self._profile.base_model, ctx)
        return ctx.default_return_type

    def check_parameter(self, method: str, names: tuple[str, ...], ctx: MethodContext) -> Type:
        """``name_search(name, domain)`` and the like: the domain parameter, by its name."""
        for name in names:
            if name in ctx.callee_arg_names:
                index = ctx.callee_arg_names.index(name)
                for argument in ctx.args[index]:
                    self._check_receiver_domain(method, argument, ctx)
                break
        return ctx.default_return_type

    def _check_receiver_domain(self, method: str, domain: Expression, ctx: MethodContext) -> None:
        """Check the domain of a method of records (not of another class's ``search``)."""
        addon_name = self.registry.addon_name_for_path(ctx.api.path)
        if addon_name is None or not self._is_records(ctx):
            return
        model = self._receiver_model(ctx, addon_name)
        concrete = model is not None and self._concrete(model, addon_name, ctx.api)

        def mismatch(other: str) -> str:
            return f'Domain of Odoo model "{other}" given to {method}() of Odoo model "{model}"'

        mode = Mode(search=method in SEARCH_CONSUMERS)
        consumer = Consumer(addon_name, model if concrete else None, mode, mismatch)
        self._check(domain, consumer, ctx.api)

    def wrapped(self, ctx: FunctionContext) -> Type:
        """The wrapper of an annotated expression (``__build_class__(E, kind, alias)``).

        Checks ``E`` as a domain of the annotation's model (a field's search method: of
        its model) and gives ``E``'s type (the signature is an identity, see
        :func:`wrapper_signature`). A call of another form is left alone.
        """
        found = _wrapper_arguments(ctx.args)
        if found is None:
            return ctx.default_return_type
        expression, kind, alias = found
        addon_name = self.registry.addon_name_for_path(ctx.api.path)
        if addon_name is None:
            return ctx.default_return_type
        model = alias if kind == SEARCH else self.registry.resolve_alias(alias, addon_name).model
        place = "assigned to" if kind in {ASSIGN, ASSIGN_BUILT} else "returned as"

        def mismatch(other: str) -> str:
            return f'Domain of Odoo model "{other}" {place} a domain of Odoo model "{model}"'

        search = kind in {SEARCH, SEARCH_ALIAS}
        mode = Mode(search=search, internal=search)
        consumer = Consumer(addon_name, model, mode, mismatch, whole=kind != ASSIGN_BUILT)
        self._check(expression, consumer, ctx.api)
        return ctx.default_return_type

    def annotated_parameters(
        self,
        parameters: Mapping[str, str],
        callee: str,
        then: Callable[[AnyContext], Type] | None,
        ctx: AnyContext,
    ) -> Type:
        """Check the arguments of parameters annotated ``odoo.domain.<model>``.

        A parameter annotated with a model the receiver's model inherits (a mixin's method,
        called on a model that uses the mixin) takes a domain of the receiver's model: at
        run time, ``self`` is that model.
        """
        addon_name = self.registry.addon_name_for_path(ctx.api.path)
        if addon_name is not None:
            receiver = (
                self._receiver_model(ctx, addon_name) if isinstance(ctx, MethodContext) else None
            )
            for name, declared in parameters.items():
                if name not in ctx.callee_arg_names:
                    continue
                expected = self._expected_model(declared, receiver, addon_name, ctx.api)
                consumer = Consumer(
                    addon_name,
                    expected,
                    Mode(search=False),
                    partial(_parameter_mismatch, name, callee, expected),
                )
                for argument in ctx.args[ctx.callee_arg_names.index(name)]:
                    self._check(argument, consumer, ctx.api)
        return then(ctx) if then is not None else ctx.default_return_type

    def _expected_model(
        self, declared: str, receiver: str | None, addon_name: str, api: CheckerPluginInterface
    ) -> str:
        """The model a domain parameter takes: the receiver's if it inherits the declared one.

        At run time ``self`` is the receiver: a method of a mixin, or of a model another
        one inherits (``_name`` with ``_inherit``), runs on the receiver's records.
        """
        if receiver is None or receiver == declared:
            return declared
        self._depend_on(receiver, addon_name, api)
        view = self.registry.view_class(receiver, addon_name)
        if view is not None and any(item.model == declared for item in view.chain):
            return receiver
        return declared

    def check_field(
        self, call: CallExpr, field_class: str, addon_name: str, api: CheckerPluginInterface
    ) -> None:
        """Check the ``domain=`` of a relational field against its comodel."""
        arguments = dict(field_arguments(call, field_class))
        comodel = arguments.get("comodel_name")
        domain = arguments.get("domain")
        if not isinstance(comodel, StrExpr) or domain is None:
            return
        if not isinstance(domain, StrExpr):
            if isinstance(domain, LambdaExpr):
                domain = domain.expr()  # domain=lambda self: [...]
            concrete = self._concrete(comodel.value, addon_name, api)

            def mismatch(other: str) -> str:
                return (
                    f'Domain of Odoo model "{other}" given to domain= of a field with comodel '
                    f'"{comodel.value}"'
                )

            consumer = Consumer(
                addon_name, comodel.value if concrete else None, Mode(search=True), mismatch
            )
            self._check(domain, consumer, api, callable_domain=True)
            return
        if not self._concrete(comodel.value, addon_name, api):
            return
        checker = self._domain_checker(addon_name, api)
        for path, operator in string_domain_paths(domain.value):
            path_node = StrExpr(path)
            path_node.set_line(domain)
            operator_node = None
            if operator is not None:
                operator_node = StrExpr(operator)
                operator_node.set_line(domain)
            checker.check_term(
                comodel.value, Term(path_node, operator_node, None), Mode(search=True)
            )

    # -- Odoo 19+: DomainType parameters ---------------------------------------------------------

    def widen_signature(
        self, then: Callable[[MethodSigContext], FunctionLike] | None, ctx: MethodSigContext
    ) -> FunctionLike:
        """Let a ``DomainType`` parameter (Odoo 19+) take any list, in addon files.

        ``DomainType`` is ``Domain | list[str | tuple[str, str, Any]]``: a variable built
        from a literal (``list[tuple[str, str, str]]``) does not fit it, ``list`` being
        invariant. Its list member becomes ``list[Any]``; the plugin checks the terms. Code
        outside any addon keeps Odoo's signature, as everything there.
        """
        signature = then(ctx) if then is not None else ctx.default_signature
        domain_class = self._profile.domain_class
        if (
            domain_class is None
            or not isinstance(signature, CallableType)
            or self.registry.addon_name_for_path(ctx.api.path) is None
        ):
            return signature
        any_list = ctx.api.named_generic_type("builtins.list", [AnyType(TypeOfAny.special_form)])
        widened = [_widened(arg, domain_class, any_list) for arg in signature.arg_types]
        if all(new is old for new, old in zip(widened, signature.arg_types, strict=True)):
            return signature
        return signature.copy_modified(arg_types=widened)

    # -- the check ---------------------------------------------------------------------------

    def _check(
        self,
        domain: Expression,
        consumer: Consumer,
        api: CheckerPluginInterface,
        callable_domain: bool = False,
        depth: int = 0,
    ) -> None:
        """Check a domain expression for a consumer: its literal and typed parts.

        Args:
            domain: The expression.
            consumer: Where it goes.
            api: mypy's checker.
            callable_domain: The expression may also be a function that returns a domain
                (``domain=_get_domain``).
            depth: How many ``any`` sub-domains deep the expression is.
        """
        if depth > MAX_SUBDOMAIN_DEPTH:
            return
        checker = type_checker(api)
        declared = partial(_declared_model, checker)
        walker = DomainParts(self._profile.domain_class, self._local_values(api), declared)
        parts = walker.parts(domain, consumer.whole)
        if callable_domain and not parts:
            found = _function_domain_model(domain)
            if found is not None:
                parts = [TypedPart(domain, found)]
        domain_checker = self._domain_checker(consumer.addon, api, depth)
        for part in parts:
            if isinstance(part, TypedPart):
                expected = consumer.model
                if expected is not None and not self._accepts(
                    expected, part.model, consumer.addon, api
                ):
                    api.fail(consumer.mismatch(part.model), part.node, code=ODOO_DOMAIN_MODEL)
                continue
            domain_checker.structure(part.items, part.whole)
            model = part.model or consumer.model
            if model is not None:
                domain_checker.check(model, part.items, consumer.mode, part.condition)

    def _domain_checker(
        self, addon_name: str, api: CheckerPluginInterface, depth: int = 0
    ) -> DomainChecker:
        checker = type_checker(api)

        def fail(message: str, context: Context, code: ErrorCode) -> None:
            api.fail(message, context, code=code)

        def type_of(node: Expression) -> ProperType | None:
            found = checker.lookup_type_or_none(node)
            return get_proper_type(found) if found is not None else None

        def describe(typ: ProperType) -> str:
            return format_type_bare(typ, checker.options)

        def subdomain(
            value: Expression, comodel: str, field: FieldInfo, model: str, mode: Mode
        ) -> None:
            def mismatch(other: str) -> str:
                return (
                    f'Domain of Odoo model "{other}" in "any" of field "{field.name}" of Odoo '
                    f'model "{model}", which takes a domain of "{comodel}"'
                )

            consumer = Consumer(addon_name, comodel, mode, mismatch)
            self._check(value, consumer, api, depth=depth + 1)

        return DomainChecker(
            fail,
            self._fields_of(addon_name, api),
            self._profile,
            self._field_guarded(api.path),
            type_of,
            describe,
            subdomain,
        )

    def _accepts(
        self, expected: str, given: str, addon_name: str, api: CheckerPluginInterface
    ) -> bool:
        """Whether a domain of ``given`` fits where one of ``expected`` goes.

        Its own, those of the models its registry class inherits (``_inherit`` parents and
        mixins: their fields are its fields) and of the models it delegates to: the same
        rule as for ``odoo.values.*`` (:meth:`Registry.accepted_values`).
        """
        if given == expected:
            return True
        self._depend_on(expected, addon_name, api)
        return given in self.registry.accepted_values(expected, addon_name)

    def _concrete(self, model: str, addon_name: str, api: CheckerPluginInterface) -> bool:
        self._depend_on(model, addon_name, api)
        return self.registry.concrete(model, addon_name)

    def _is_records(self, ctx: MethodContext) -> bool:
        """Whether a method's receiver is a model's records (or a model class)."""
        receiver = get_proper_type(ctx.type)
        if isinstance(receiver, TypeType):
            receiver = receiver.item
        return isinstance(receiver, Instance) and receiver.type.has_base(self._profile.base_model)

    def _receiver_model(self, ctx: AnyContext, addon_name: str) -> str | None:
        """The model of a method call's receiver (records, or a model class)."""
        if not isinstance(ctx, MethodContext):
            return None
        receiver = get_proper_type(ctx.type)
        if isinstance(receiver, TypeType):
            receiver = receiver.item
        if not isinstance(receiver, Instance):
            return None
        return self._models.model_of(receiver.type)


def _parameter_mismatch(name: str, callee: str, expected: str, other: str) -> str:
    return (
        f'Domain of Odoo model "{other}" given to parameter "{name}" of "{callee}", which takes '
        f'a domain of Odoo model "{expected}"'
    )


def _wrapper_arguments(args: list[list[Expression]]) -> tuple[Expression, str, str] | None:
    """The expression, kind and alias of the plugin's wrapper call, if it is one.

    ``args`` are the actual arguments by formal parameter: of typeshed's signature in the
    signature hook (``func, name, *bases, ...``), of the identity in the function hook.
    """
    actuals = [actual for formal in args for actual in formal]
    if len(actuals) != 3:
        return None
    expression, kind, alias = actuals
    if not isinstance(kind, StrExpr) or not isinstance(alias, StrExpr):
        return None
    if kind.value not in WRAPPED_KINDS:
        return None
    return expression, kind.value, alias.value


def wrapper_signature(ctx: FunctionSigContext) -> FunctionLike:
    """The signature of the plugin's wrapper: ``def [T] (T, str, str) -> T``.

    Another call of ``__build_class__`` keeps typeshed's signature.
    """
    if _wrapper_arguments(ctx.args) is None:
        return ctx.default_signature
    api = ctx.api
    variable = TypeVarType(
        "T",
        "T",
        TypeVarId(-1, namespace="odoo_typing"),
        [],
        api.named_generic_type("builtins.object", []),
        AnyType(TypeOfAny.from_omitted_generics),
    )
    text = api.named_generic_type("builtins.str", [])
    return ctx.default_signature.copy_modified(
        arg_types=[variable, text, text],
        arg_kinds=[ARG_POS] * 3,
        arg_names=["domain", "kind", "alias"],
        ret_type=variable,
        variables=[variable],
    )


def _declared_model(checker: TypeChecker, node: Expression) -> str | None:
    """The model a declaration names a domain of (see :mod:`odoo_typing.domain_parts`)."""
    if isinstance(node, CallExpr) and isinstance(node.analyzed, CastExpr):
        return domain_type_model(node.analyzed.type)  # cast(...)
    if isinstance(node, CallExpr):
        callee = get_proper_type(checker.lookup_type_or_none(node.callee))
        return domain_type_model(callee.ret_type) if isinstance(callee, CallableType) else None
    if isinstance(node, CastExpr):
        return domain_type_model(node.type)
    if isinstance(node, NameExpr):
        return _variable_model(node.node)
    if isinstance(node, MemberExpr):
        if isinstance(node.node, Var):  # a module's or a class's attribute
            return _variable_model(node.node)
        owner = get_proper_type(checker.lookup_type_or_none(node.expr))
        if isinstance(owner, TypeType):
            owner = owner.item
        if isinstance(owner, Instance):
            found = owner.type.get(node.name)
            return _variable_model(found.node) if found is not None else None
    return None


def _variable_model(node: object) -> str | None:
    """The model a variable's annotation names (not a type mypy inferred: ``d = other``).

    A variable without an annotation has the model its bindings agree on
    (:meth:`odoo_typing.domain_parts.DomainParts.agreed_model`).
    """
    if not isinstance(node, Var) or node.is_inferred:
        return None
    return domain_type_model(node.type)


def _function_domain_model(node: Expression) -> str | None:
    """The model a function (not a call of it) returns a domain of: ``domain=_get_domain``."""
    if not isinstance(node, RefExpr):
        return None
    target = node.node
    func = getattr(target, "func", target)
    signature = getattr(func, "type", None)
    return domain_type_model(signature.ret_type) if isinstance(signature, CallableType) else None


def _widened(arg: Type, domain_class: str, any_list: Instance) -> Type:
    """``Domain | list[...]`` (``DomainType``) with ``list[Any]`` for its list member."""
    proper = get_proper_type(arg)
    if not isinstance(proper, UnionType) or not _is_domain_type(proper, domain_class):
        return arg
    items = [any_list if _is_list(get_proper_type(item)) else item for item in proper.items]
    return UnionType.make_union(items, arg.line, arg.column)


def _is_list(typ: ProperType) -> bool:
    return isinstance(typ, Instance) and typ.type.fullname == "builtins.list"


def _is_domain_type(union: UnionType, domain_class: str) -> bool:
    """Whether a union has a ``Domain`` member and a list member that is not ``list[Any]``."""
    items = [get_proper_type(item) for item in union.items]
    has_domain = any(
        isinstance(item, Instance) and item.type.fullname == domain_class for item in items
    )
    precise_list = any(
        _is_list(item)
        and isinstance(item, Instance)
        and not isinstance(get_proper_type(item.args[0]), AnyType)
        for item in items
        if isinstance(item, Instance) and item.args
    )
    return has_domain and precise_list


def has_domain_type(signature: Type | None, domain_class: str | None) -> bool:
    """Whether a declared signature has a ``DomainType`` parameter (Odoo 19+)."""
    if domain_class is None or not isinstance(signature, CallableType):
        return False
    return any(
        isinstance(proper := get_proper_type(arg), UnionType)
        and _is_domain_type(proper, domain_class)
        for arg in signature.arg_types
    )


def domain_parameters(signature: Type | None) -> dict[str, str]:
    """The parameters of a declared signature annotated ``odoo.domain.<model>``, by name."""
    if not isinstance(signature, CallableType):
        return {}
    found: dict[str, str] = {}
    for name, arg in zip(signature.arg_names, signature.arg_types, strict=True):
        model = domain_type_model(arg)
        if name is not None and model is not None:
            found[name] = model
    return found
