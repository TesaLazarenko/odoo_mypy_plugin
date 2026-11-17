"""Recordset methods: keep the model type through Odoo's decorators and overrides.

Odoo 18 declares ``sudo``, ``with_context``, ``filtered``, ``search``, ``copy``... as
``-> Self``, but wraps many of them in decorators without annotations
(``api.private``, ``api.returns``), so mypy types the decorated methods as ``Any``.
Addons then override ``create``, ``copy``, ``search``... without annotations, which
also gives ``Any``. These hooks restore the declared signatures and the receiver type,
and an override that only passes its ``super()`` call on returns what the overridden
method declares (:func:`annotate_inherited_returns`).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Final, TypeGuard

from mypy.lookup import lookup_fully_qualified
from mypy.maptype import map_instance_to_supertype
from mypy.nodes import (
    ARG_POS,
    CallExpr,
    ConditionalExpr,
    Decorator,
    DictExpr,
    DictionaryComprehension,
    Expression,
    FuncDef,
    IndexExpr,
    ListComprehension,
    ListExpr,
    MemberExpr,
    MypyFile,
    NameExpr,
    OpExpr,
    RefExpr,
    SuperExpr,
    SymbolNode,
    TypeInfo,
)
from mypy.plugin import ClassDefContext, FunctionContext, MethodContext, MethodSigContext
from mypy.types import (
    AnyType,
    CallableType,
    FunctionLike,
    Instance,
    LiteralType,
    NoneType,
    TupleType,
    Type,
    TypeAliasType,
    TypedDictType,
    TypeOfAny,
    TypeVarId,
    TypeVarType,
    UnboundType,
    UnionType,
    get_proper_type,
    has_type_vars,
)
from mypy.typevars import fill_typevars

# isort: split
# After mypy.types: mypy.checker and mypy.types import each other at import time.
from mypy.checker import TypeChecker
from mypy.expandtype import expand_type_by_instance
from mypy.semanal_shared import has_placeholder
from mypy.typeops import callable_type, make_simplified_union

from odoo_typing.classes import receiver_type
from odoo_typing.local_values import MAX_DEPTH, LocalValues
from odoo_typing.vals import values_model_of

__all__ = [
    "COMMAND_ID_METHODS",
    "SELF_METHODS",
    "annotate_inherited_returns",
    "annotate_self_overrides",
    "browse_signature",
    "command_id_signature",
    "create_signature",
    "identity_decorator",
    "identity_factory",
    "is_browse",
    "is_command_id_method",
    "is_create",
    "is_identity_decorator",
    "is_self_method",
    "keep_receiver",
    "overridden_return",
    "returns_own_records",
]

ITERABLE: Final = "typing.Iterable"

# ``Command.link(id)`` and the like: Odoo declares ``id: int``.
COMMAND_ID_METHODS: Final = frozenset({"link", "update", "delete", "unlink"})

# ``a | b``, ``a & b``, ``a - b``, ``a + b`` of records of one model are records of it.
RECORD_SET_OPERATORS: Final = frozenset({"|", "&", "-", "+"})

# BaseModel methods declared ``-> Self``: an unannotated override returns the receiver.
SELF_METHODS: Final = frozenset(
    {
        "browse",
        "copy",
        "create",
        "ensure_one",
        "exists",
        "filtered",
        "filtered_domain",
        "search",
        "sorted",
        "sudo",
        "with_company",
        "with_context",
        "with_env",
        "with_prefetch",
        "with_user",
    }
)


def identity_decorator(ctx: FunctionContext) -> Type:
    """``@api.private`` and the like: the decorated method keeps its own signature."""
    if len(ctx.arg_types) == 1 and len(ctx.arg_types[0]) == 1:
        return ctx.arg_types[0][0]
    return ctx.default_return_type


def identity_factory(template: str | None, ctx: FunctionContext) -> Type:
    """``@api.returns("self")``: a decorator that keeps the method's own signature.

    ``template`` is the full name of a typed ``def [T] (T) -> T`` (``api.model``); with
    ``None`` (Odoo 17 types none) such a function type is built here.
    """
    if not isinstance(ctx.api, TypeChecker):
        return ctx.default_return_type
    if template is None:
        return _identity_type(ctx.api)
    node = lookup_fully_qualified(template, ctx.api.modules)
    func = node.node if node is not None else None
    if isinstance(func, FuncDef) and isinstance(func.type, CallableType):
        return func.type
    return ctx.default_return_type


def _identity_type(checker: TypeChecker) -> CallableType:
    """``def [T] (T) -> T``."""
    variable = TypeVarType(
        "T",
        "T",
        id=TypeVarId(-1),
        values=[],
        upper_bound=checker.named_type("builtins.object"),
        default=AnyType(TypeOfAny.from_omitted_generics),
    )
    return CallableType(
        [variable],
        [ARG_POS],
        [None],
        variable,
        checker.named_type("builtins.function"),
        variables=[variable],
    )


def is_self_method(fullname: str) -> bool:
    """Whether a method's full name (``<receiver class>.<name>``) is a Self method."""
    return fullname.rpartition(".")[2] in SELF_METHODS


def is_create(fullname: str) -> bool:
    """Whether a method's full name is ``<receiver class>.create``."""
    return fullname.rpartition(".")[2] == "create"


def is_command_id_method(fullname: str) -> bool:
    """Whether a method is ``Command.link`` / ``update`` / ``delete`` / ``unlink`` of Odoo."""
    owner, _, name = fullname.rpartition(".")
    return name in COMMAND_ID_METHODS and owner.startswith("odoo.") and owner.endswith(".Command")


def is_command_set(fullname: str) -> bool:
    """Whether a method is ``Command.set`` of Odoo."""
    owner, _, name = fullname.rpartition(".")
    return name == "set" and owner.startswith("odoo.") and owner.endswith(".Command")


def command_id_signature(new_id: str, ctx: MethodSigContext) -> FunctionLike:
    """``Command.link(record.id)``: the ``id`` may also be a ``NewId`` (Odoo 17, 18).

    ``record.id`` is ``int | NewId`` there (a record of ``new()`` or of an onchange has a
    ``NewId``), and Odoo accepts one in a command; ``id: int`` made each such command
    an ``arg-type`` error (most of them in tests).
    """
    signature = ctx.default_signature
    if not isinstance(signature, CallableType) or "id" not in signature.arg_names:
        return signature
    index = signature.arg_names.index("id")
    declared = get_proper_type(signature.arg_types[index])
    node = lookup_fully_qualified(new_id, type_checker_modules(ctx))
    if (
        not isinstance(declared, Instance)
        or declared.type.fullname != "builtins.int"
        or node is None
        or not isinstance(node.node, TypeInfo)
    ):
        return signature
    widened = UnionType([declared, Instance(node.node, [])])
    arg_types = [*signature.arg_types]
    arg_types[index] = widened
    return signature.copy_modified(arg_types=arg_types)


def is_browse(fullname: str) -> bool:
    """Whether a method's full name is ``<receiver class>.browse``."""
    return fullname.rpartition(".")[2] == "browse"


def browse_signature(base_model: str, ctx: MethodSigContext) -> FunctionLike:
    """``browse(record.id)`` and ``browse(context.get("active_id"))``.

    Odoo declares ``ids: int | Iterable[IdType]``, but ``browse`` takes more: its
    docstring says "int or iterable(int) or None", and any falsy argument gives no
    records. So ``None`` and ``False`` fit, and so does one id of any kind the iterable
    may hold: on Odoo 18 ``record.id`` is ``int | NewId``, which a list of ids may hold
    but the id alone may not (a ``NewId`` alone is falsy: no records, no error).

    For the same reason ``browse(request and request.pricelist.id)`` takes the operands of
    ``and`` before the last one: only their falsy values get there (mypy takes a
    ``Request`` for one that may be falsy).
    """
    signature = ctx.default_signature
    receiver = get_proper_type(ctx.type)
    if (
        not isinstance(signature, CallableType)
        or not signature.arg_types
        or not isinstance(receiver, Instance)
        or not receiver.type.has_base(base_model)
    ):
        return signature
    declared = get_proper_type(signature.arg_types[0])
    if not isinstance(declared, UnionType):
        return signature
    held = [
        item.args[0]
        for item in map(get_proper_type, declared.items)
        if isinstance(item, Instance) and item.type.fullname == ITERABLE and item.args
    ]
    if len(held) != 1:
        return signature
    false = LiteralType(False, ctx.api.named_generic_type("builtins.bool", []))
    falsy = _falsy_operands(ctx.args[0][0], ctx) if ctx.args and len(ctx.args[0]) == 1 else []
    arg_types = [*signature.arg_types]
    arg_types[0] = make_simplified_union([*declared.items, held[0], NoneType(), false, *falsy])
    return signature.copy_modified(arg_types=arg_types)


def _falsy_operands(argument: Expression, ctx: MethodSigContext) -> list[Type]:
    """The types of the operands of ``a and b and c`` but the last: only falsy ones get through."""
    # ``a and b and c`` is ``(a and b) and c``: its left operand gets through when falsy.
    operands: list[Expression] = []
    while isinstance(argument, OpExpr) and argument.op == "and":
        operands.append(argument.left)
        argument = argument.right
    checker = ctx.api
    if not operands or not isinstance(checker, TypeChecker):
        return []
    with checker.msg.filter_errors(), checker.local_type_map:
        return [checker.get_expression_type(operand) for operand in operands]


def type_checker_modules(ctx: MethodSigContext) -> dict[str, MypyFile]:
    """The modules mypy has loaded, as the checker of a signature hook sees them."""
    return ctx.api.modules if isinstance(ctx.api, TypeChecker) else {}


def create_signature(base_model: str, ctx: MethodSigContext, untyped: bool = False) -> FunctionLike:
    """``create(vals_list: list[X])`` also accepts one ``X`` and any iterable of them.

    ``BaseModel.create`` is declared with a list (Odoo 18; Odoo 19+ declares both), but
    its decorator wraps a single dict into a list at runtime, and most code passes one
    dict; the list is read once, so any iterable of values does (``create({...} for i
    in ...)``). With ``untyped`` (Odoo 17, where ``vals_list`` is unannotated) the
    argument gets Odoo 18's type: ``dict[str, Any] | Iterable[dict[str, Any]]``. A list of
    ``odoo.values.*`` is accepted too (:func:`_values_list`).
    """
    signature = ctx.default_signature
    receiver = receiver_type(ctx.type)
    if (
        not isinstance(signature, CallableType)
        or not isinstance(receiver, Instance)
        or not receiver.type.has_base(base_model)
        or not signature.arg_types
    ):
        return signature
    vals = get_proper_type(signature.arg_types[0])
    if untyped and isinstance(vals, AnyType) and vals.type_of_any == TypeOfAny.unannotated:
        any_type = AnyType(TypeOfAny.special_form)
        text = ctx.api.named_generic_type("builtins.str", [])
        vals = ctx.api.named_generic_type(
            "builtins.list", [ctx.api.named_generic_type("builtins.dict", [text, any_type])]
        )
    # ``create({...} for i in range(8))``: Odoo reads the list once (``_prepare_create_values``),
    # so any iterable of values does.
    if _is_list(vals):
        accepted = [vals.args[0], _iterable(vals, ctx)]  # Odoo 17 and 18
    elif isinstance(vals, UnionType) and any(_is_list(item) for item in vals.items):
        accepted = [_iterable(item, ctx) for item in vals.items]
    else:
        return signature
    if (listed := _values_list(ctx)) is not None:
        accepted.append(listed)  # list is invariant: list[values] is no list[dict[str, Any]]
    union = UnionType.make_union(accepted)
    return signature.copy_modified(arg_types=[union, *signature.arg_types[1:]])


def _iterable(typ: Type, ctx: MethodSigContext) -> Type:
    """``Iterable[X]`` for ``list[X]``; another type as it is."""
    if not _is_list(typ):
        return typ
    return ctx.api.named_generic_type("typing.Iterable", [typ.args[0]])


def _is_list(typ: Type) -> TypeGuard[Instance]:
    proper = get_proper_type(typ)
    return isinstance(proper, Instance) and proper.type.fullname == "builtins.list"


# Arguments mypy infers in the context of the parameter's type: a list display of values
# fits ``list[dict[str, Any]]`` there (the type of a variable or a call does not).
IN_CONTEXT = (DictExpr, ListExpr, ListComprehension, DictionaryComprehension)


def _values_list(ctx: MethodSigContext) -> Type | None:
    """The type of a first argument that is a list of ``odoo.values.*``, if it is one.

    Inferred ahead of mypy, without its errors or recorded types (as mypy tries the
    items of an overload); displays and comprehensions are left to mypy.
    """
    first = ctx.args[0] if ctx.args else []
    checker = ctx.api
    if len(first) != 1 or isinstance(first[0], IN_CONTEXT) or not isinstance(checker, TypeChecker):
        return None
    with checker.msg.filter_errors(), checker.local_type_map:
        found = checker.get_expression_type(first[0])
    return found if _is_list(found) and values_model_of(found, many=True) is not None else None


def keep_receiver(base_model: str, ctx: MethodContext) -> Type:
    """Return the receiver from an unannotated override of a ``-> Self`` method.

    Only when the method's return type is the implicit ``Any`` of a def without
    annotations, and the receiver is a model; ``-> Any`` written out is kept.
    """
    default = get_proper_type(ctx.default_return_type)
    receiver = receiver_type(ctx.type)
    if (
        isinstance(default, AnyType)
        and default.type_of_any == TypeOfAny.unannotated
        and isinstance(receiver, Instance)
        and receiver.type.has_base(base_model)
    ):
        return ctx.type  # ``Self`` stays ``Self`` (an override's body)
    return ctx.default_return_type


def annotate_self_overrides(ctx: ClassDefContext) -> None:
    """Type unannotated overrides of ``-> Self`` methods as ``(self: Self, ...) -> Self``.

    ``super().create(vals_list)`` in an override returns what the next class's
    ``create`` returns; when that is another unannotated override, mypy says ``Any``.
    mypy calls no plugin hook for ``super()`` calls (the callee has no full name), so
    the overrides themselves get the type the ``BaseModel`` method declares: their
    arguments stay ``Any`` as in any unannotated def. Runs after the class body.

    Only an override that returns its own records (:func:`returns_own_records`) is
    annotated: ``return self.env["account.account"]...`` would be a ``return-value``
    error against ``Self``. Another one stays unannotated; calls of it still keep the
    receiver (:func:`keep_receiver`), only ``super()`` through it is ``Any``.
    """
    info = ctx.cls.info
    for statement in ctx.cls.defs.body:
        func = statement.func if isinstance(statement, Decorator) else statement
        if not isinstance(func, FuncDef) or func.type is not None or func.name not in SELF_METHODS:
            continue
        if func.is_static or func.is_class or not func.arg_names:
            continue
        if not returns_own_records(func):
            continue
        func.type = _self_signature(func, info, ctx.api.named_type("builtins.function"))


def returns_own_records(func: FuncDef) -> bool:
    """Whether every ``return`` of a method gives records of its receiver, by syntax.

    ``self``, ``super().m(...)`` and ``x.m(...)`` of a ``-> Self`` method on such records,
    ``self.env[self._name]``, ``a | b`` of such records, a conditional of them, and a
    variable every binding of which is one of these. A bare ``return`` is not.
    """
    local = LocalValues(func)
    receiver = func.arg_names[0]
    return all(
        statement.expr is not None and _own_records(statement.expr, receiver, local, 0)
        for statement in local.returns
    )


def _own_records(node: Expression, receiver: str | None, local: LocalValues, depth: int) -> bool:
    if depth > MAX_DEPTH:
        return False
    if isinstance(node, NameExpr):
        if node.name == receiver:
            return True
        values = local.bindings(node)
        return bool(values) and all(
            value is not None and _own_records(value, receiver, local, depth + 1)
            for value in values
        )
    if isinstance(node, CallExpr):
        callee = node.callee
        if isinstance(callee, SuperExpr):
            return callee.name in SELF_METHODS
        return (
            isinstance(callee, MemberExpr)
            and callee.name in SELF_METHODS
            and _own_records(callee.expr, receiver, local, depth + 1)
        )
    if isinstance(node, OpExpr) and node.op in RECORD_SET_OPERATORS:
        return _own_records(node.left, receiver, local, depth + 1) and _own_records(
            node.right, receiver, local, depth + 1
        )
    if isinstance(node, ConditionalExpr):
        return _own_records(node.if_expr, receiver, local, depth + 1) and _own_records(
            node.else_expr, receiver, local, depth + 1
        )
    return _own_name_records(node, receiver)


def _own_name_records(node: Expression, receiver: str | None) -> bool:
    """``self.env[self._name]``."""
    return (
        isinstance(node, IndexExpr)
        and isinstance(node.base, MemberExpr)
        and node.base.name == "env"
        and isinstance(node.index, MemberExpr)
        and node.index.name == "_name"
        and isinstance(node.index.expr, NameExpr)
        and node.index.expr.name == receiver
    )


def _self_signature(func: FuncDef, info: TypeInfo, fallback: Instance) -> CallableType:
    if info.self_type is None:
        # As mypy's own ``SemanticAnalyzer.setup_self_type`` (id 0 marks a self type).
        info.self_type = TypeVarType(
            "Self",
            f"{info.fullname}.Self",
            id=TypeVarId(0),
            values=[],
            upper_bound=fill_typevars(info),
            default=AnyType(TypeOfAny.from_omitted_generics),
        )
    self_type = info.self_type
    others = [AnyType(TypeOfAny.unannotated)] * (len(func.arg_names) - 1)
    return CallableType(
        [self_type, *others],
        func.arg_kinds,
        func.arg_names,
        self_type,
        fallback,
        name=func.name,
        variables=[self_type],
        line=func.line,
        column=func.column,
        definition=func,
    )


def annotate_inherited_returns(
    ctx: ClassDefContext, names: frozenset[str], identity: Callable[[Expression], bool]
) -> None:
    """Give an override that passes its ``super()`` call on the return type it overrides.

    ``product.product`` overrides ``_search`` without annotations and returns
    ``super()._search(domain, ...)``, so mypy typed every ``_search`` of products ``Any``
    where Odoo declares ``Query`` (and so on for ``write``, ``name_search``...). An
    unannotated method every ``return`` of which gives ``super().<the method>(...)``
    (``names``: the scan reads them, :mod:`odoo_typing.scanner.super_returns`, as mypy
    keeps no bodies of the modules it does not report on) gets the return type of the
    definition its ``super()`` call reaches: the next class of the MRO that has the
    method (:func:`overridden_return`). That definition may be such an override of an
    earlier class, typed so when its class was analyzed: a chain of them returns the
    type declared at its end.

    The type is the implicit signature mypy gives an unannotated def
    (``callable_type``) with that return type: the parameters stay ``Any`` and the
    function stays dynamic (``FuncItem.is_dynamic``), so its body is checked as before
    (an empty ``[]`` stays ``list[Any]``), and its returns, checked against the type,
    give it by construction. ``-> Self`` methods are typed by
    :func:`annotate_self_overrides`.

    Runs after the class body, once the class has the MRO of its registry chain
    (``identity`` tells the decorators that keep a method's signature); a class whose
    MRO misses classes of the chain (``fallback_to_any``) is left alone.
    """
    info = ctx.cls.info
    if not names or info.fallback_to_any:
        return
    fallback: Instance | None = None
    for statement in ctx.cls.defs.body:
        func = statement.func if isinstance(statement, Decorator) else statement
        if (
            not isinstance(func, FuncDef)
            or func.name not in names
            or func.name in SELF_METHODS
            or func.type is not None
            or func.is_property
            or func.is_static
            or func.is_class
            or not func.arg_names
        ):
            continue
        returned = overridden_return(info, func.name, identity)
        if returned is not None:
            fallback = fallback or ctx.api.named_type("builtins.function")
            func.type = callable_type(func, fallback, returned)


def overridden_return(
    info: TypeInfo, name: str, identity: Callable[[Expression], bool]
) -> Type | None:
    """The return type of the method a ``super().<name>(...)`` call of a class reaches.

    The first class after it in its MRO that defines the name: a method annotated
    there (or typed by :func:`annotate_inherited_returns` when that class was
    analyzed), undecorated or decorated only by decorators that keep its signature
    (``identity``). ``None`` for anything else: an unannotated method, an overload, a
    property, an attribute, a return type that is ``Any`` or ``None`` (no use to a
    caller), or one that holds type variables (``Self``: see
    :func:`annotate_self_overrides`).
    """
    for base in info.mro[1:]:
        symbol = base.names.get(name)
        if symbol is not None:
            return _declared_return(symbol.node, info, base, identity)
    return None


def _declared_return(
    node: SymbolNode | None,
    info: TypeInfo,
    base: TypeInfo,
    identity: Callable[[Expression], bool],
) -> Type | None:
    if isinstance(node, Decorator):
        if node.var.is_property or not all(map(identity, node.decorators)):
            return None
        node = node.func
    if not isinstance(node, FuncDef) or node.is_property or node.is_static or node.is_class:
        return None
    declared = node.type
    if not isinstance(declared, CallableType):
        return None
    returned = declared.ret_type
    if base.type_vars:  # a generic base: its type arguments in the class
        returned = expand_type_by_instance(returned, map_instance_to_supertype(_filled(info), base))
    if (
        isinstance(get_proper_type(returned), AnyType | NoneType)
        or has_type_vars(returned)
        or has_placeholder(returned)
        or _unanalyzed(returned)
    ):
        return None
    return returned


def _filled(info: TypeInfo) -> Instance:
    """The instance type of a class, its own type variables as arguments."""
    filled = fill_typevars(info)
    return filled if isinstance(filled, Instance) else Instance(info, [])


def _unanalyzed(typ: Type) -> bool:
    """Whether a type still holds names semantic analysis has not resolved."""
    if isinstance(typ, UnboundType):
        return True
    items: list[Type]
    if isinstance(typ, Instance | TypeAliasType):
        items = list(typ.args)
    elif isinstance(typ, UnionType | TupleType):
        items = list(typ.items)
    elif isinstance(typ, CallableType):
        items = [*typ.arg_types, typ.ret_type]
    elif isinstance(typ, TypedDictType):
        items = list(typ.items.values())
    else:
        return False
    return any(map(_unanalyzed, items))


def is_identity_decorator(decorator: Expression, names: frozenset[str]) -> bool:
    """Whether a decorator returns the method it decorates: by name, or typed ``(T) -> T``.

    ``names`` are the full names of such decorators and decorator factories (Odoo 18's
    untyped ``api.model``, ``api.returns(...)``); Odoo 19's ``api.model`` is declared
    ``def [C] (method: C) -> C``.
    """
    if isinstance(decorator, CallExpr):
        callee = decorator.callee
        return isinstance(callee, RefExpr) and callee.fullname in names
    if not isinstance(decorator, RefExpr):
        return False
    if decorator.fullname in names:
        return True
    node = decorator.node
    typ = node.type if isinstance(node, FuncDef) else None
    if not isinstance(typ, CallableType) or typ.arg_kinds != [ARG_POS]:
        return False
    argument, returned = get_proper_type(typ.arg_types[0]), get_proper_type(typ.ret_type)
    return (
        isinstance(argument, TypeVarType)
        and isinstance(returned, TypeVarType)
        and argument.id == returned.id
    )
