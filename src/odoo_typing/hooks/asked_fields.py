"""``"f" in records``: the question whether a field is there, asked before it is read.

A recordset contains the names of its model's fields, and an addon asks before it reads
a field that an addon outside its dependencies adds::

    if "phone_sanitized" in partner:        # ``sms`` adds it to res.partner
        partner.phone_sanitized

    if "optional_product_ids" in self.env["product.template"]:
        self.template.optional_product_ids = [...]

mypy answers ``hasattr(partner, "phone_sanitized")`` by giving ``partner`` the attribute,
with an unknown type; of ``in`` on records it knows nothing, and the read was an unknown
attribute. The hook of ``__contains__`` gives the question mypy's own answer to
``hasattr`` (``TypeChecker.hasattr_type_maps``): the records asked take the attribute
from the question on, in the block the question is in. ``"f" in records._fields`` asks
the same of ``records``.

The question is also about the model: the records of that model which the plugin types
later in the same function (``env["model"]``, the records a test case keeps) take the
attribute too. A variable typed before the question keeps its type.

Only a model that has no such field is given one; a field it has keeps its type.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import cache, partial

from mypy.nodes import (
    CallExpr,
    ComparisonExpr,
    Context,
    Decorator,
    Expression,
    FuncBase,
    FuncItem,
    MemberExpr,
    NameExpr,
    Node,
    RefExpr,
    StrExpr,
    SymbolNode,
    TypeInfo,
    Var,
)
from mypy.plugin import AttributeContext, FunctionContext, MethodContext
from mypy.server.subexpr import get_subexpressions
from mypy.type_visitor import TypeQuery
from mypy.types import (
    AnyType,
    CallableType,
    Instance,
    NoneType,
    Overloaded,
    ProperType,
    Type,
    TypeOfAny,
    TypeVarType,
    UninhabitedType,
    UnionType,
    get_proper_type,
)

from odoo_typing.classes import receiver_type
from odoo_typing.hooks.models import Hooks, Models
from odoo_typing.hooks.nodes import checked_function, type_checker, unfiltered
from odoo_typing.methods import SELF_METHODS

__all__ = ["HASATTR", "AskedFieldHooks"]

# What gives the same records again whatever their class declares: the methods of
# ``BaseModel`` declared ``-> Self`` (also their overrides without annotations) and
# ``_origin``. Through these a field given to a view goes on; see :func:`_keeping`.
KEEPING = SELF_METHODS | {"_origin"}

HASATTR = "builtins.hasattr"

FIELDS = "_fields"


@dataclass(frozen=True, slots=True)
class _Asked:
    """A field a function asked a model for, and the line of the question."""

    model: str
    name: str
    line: int


class AskedFieldHooks(Hooks):
    """The fields a function asks its records for (``"f" in records``)."""

    def __init__(self, models: Models) -> None:
        super().__init__(models)
        # The questions of the function asked last, which is the one being checked.
        self._function: FuncItem | None = None
        self._asked: list[_Asked] = []

    def contains(self, ctx: MethodContext) -> Type:
        """``"f" in records`` and ``"f" in records._fields``: the records have ``f`` from here."""
        comparison = ctx.context
        key = ctx.args[0][0] if ctx.args and ctx.args[0] else None
        if not isinstance(comparison, ComparisonExpr) or not isinstance(key, StrExpr):
            return ctx.default_return_type
        pairs = zip(comparison.operands, comparison.operands[1:], strict=False)
        container = next((right for left, right in pairs if left is key), None)
        if container is None or not key.value.isidentifier():
            return ctx.default_return_type
        checker = type_checker(ctx.api)
        records: Expression = container
        asked: Type | None = ctx.type
        if isinstance(container, MemberExpr) and container.name == FIELDS:
            records = container.expr
            asked = checker.lookup_type_or_none(records)
        models = self._record_models(asked)
        if asked is None or not models:
            return ctx.default_return_type
        self._report_denied(records, asked, key.value, comparison.line, ctx)
        narrowed, _ = checker.hasattr_type_maps(records, asked, key.value)
        for expression, with_field in (narrowed or {}).items():
            if with_field != asked:
                # mypy narrows the right operand of ``in`` to the type it has recorded
                # for it (a union of typed dicts loses items there): the record too.
                checker.store_type(expression, with_field)
                checker.binder.put(expression, with_field, from_assignment=False)
        function = checker.scope.top_level_function()
        if function is not self._function:
            self._function = function
            self._asked = []
        if function is not None:
            self._asked += [_Asked(model, key.value, comparison.line) for model in models]
        return ctx.default_return_type

    def _report_denied(
        self, records: Expression, asked: Type, name: str, line: int, ctx: MethodContext
    ) -> None:
        """Report the reads of the field where the answer is no (:mod:`odoo_typing.denied_reads`).

        mypy gives the records the field in both branches of the ``if``, so it reports
        none of them; the message is the one it has for a missing attribute.
        """
        written = _dotted(records)
        checker = type_checker(ctx.api)
        proper = receiver_type(asked)
        # By the class, not by the type: mypy may already have recorded the field for it.
        if written is None or not isinstance(proper, Instance) or proper.type.get(name):
            return
        calls = cache(partial(_line_calls, checked_function(ctx.api) or checker.tree))
        reads = self.registry.denied_reads(
            ctx.api.path, line, name, written, lambda at: _may_end(calls(), at)
        )
        if not reads:
            return
        # mypy checks ``in`` with its errors held back, and takes one reported meanwhile
        # for a failure of the operator: these are reported past that.
        with unfiltered(ctx.api):
            for context in _read_nodes(reads, name, written, ctx):
                checker.msg.has_no_attr(asked, asked, name, context)

    def view(self, ctx: MethodContext) -> Type:
        """``records.sudo()`` where ``records`` were asked for a field: the field goes along.

        ``user_field = self._fields.get("user_id")``, then under ``if user_field and
        user_field.type == "many2one":`` Odoo reads ``thread = self.sudo()`` and
        ``thread.user_id``. The answer is kept in a variable, so mypy narrows nothing;
        the scan knows the lines the question covers (:mod:`odoo_typing.guards`: the
        branch where it is true, the code after an early ``return``). The same records
        seen another way (:data:`VIEWS`) on such a line have the field.
        """
        call = ctx.context
        if not isinstance(call, CallExpr) or not isinstance(call.callee, MemberExpr):
            return ctx.default_return_type
        records = _dotted(call.callee.expr)
        if records is None:
            return ctx.default_return_type
        if not self._record_models(ctx.default_return_type):
            return ctx.default_return_type
        names = self.registry.asked_fields(
            ctx.api.path, call.line, records, lambda: _keeping(ctx.default_return_type)
        )
        if not names:
            return ctx.default_return_type
        checker = type_checker(ctx.api)
        result = ctx.default_return_type
        for name in sorted(names):
            if not checker.has_valid_attribute(result, name):
                result = checker.add_any_attribute_to_type(result, name)
        return result

    def has_attribute(self, ctx: FunctionContext) -> Type:
        """``hasattr(records, "f")`` of a name their class lacks: ``Any`` in the block.

        The question is whether an addon outside the dependencies is installed
        (``hasattr(ResPartner, "check_vat")``: ``base_vat``); the block then uses what
        that addon adds, the asked name and others (``ResPartner.fix_eu_vat_number()``).
        mypy narrows the records in the block to the type it recorded for them, when
        that type has the attribute: recorded as a union of ``Any`` alone, they are
        ``Any`` in the block, as records of a guarded model are.
        """
        if len(ctx.args) != 2 or len(ctx.args[0]) != 1 or len(ctx.args[1]) != 1:
            return ctx.default_return_type
        records, name = ctx.args[0][0], ctx.args[1][0]
        asked = ctx.arg_types[0][0]
        if not isinstance(name, StrExpr) or not self._record_models(asked):
            return ctx.default_return_type
        checker = type_checker(ctx.api)
        if not checker.has_valid_attribute(asked, name.value):
            # A union of ``Any`` alone: mypy's ``hasattr`` narrowing keeps a type that has
            # the attribute (``Any`` itself it leaves alone), the block reads it as ``Any``.
            checker.store_type(records, UnionType([AnyType(TypeOfAny.special_form)]))
        return ctx.default_return_type

    def _record_models(self, typ: Type | None) -> list[str]:
        """The models of a type that is records and nothing else (``None`` aside)."""
        proper = receiver_type(typ)
        items = proper.items if isinstance(proper, UnionType) else [proper]
        found: list[str] = []
        for item in map(receiver_type, items):
            if isinstance(item, NoneType):
                continue
            model = self._models.model_of(item.type) if isinstance(item, Instance) else None
            if model is None:
                return []
            found.append(model)
        return found

    def with_asked(self, records: Type, ctx: AttributeContext | MethodContext) -> Type:
        """Records typed after their model was asked for a field take that field."""
        if not self._asked:
            return records
        checker = type_checker(ctx.api)
        if checker.scope.top_level_function() is not self._function:
            return records
        proper = get_proper_type(records)
        if isinstance(proper, UnionType):
            items = [self._with_asked(get_proper_type(item), ctx) for item in proper.items]
            return UnionType.make_union(items, proper.line, proper.column)
        return self._with_asked(proper, ctx)

    def _with_asked(self, records: ProperType, ctx: AttributeContext | MethodContext) -> Type:
        if not isinstance(records, Instance):
            return records
        model = self._models.model_of(records.type)
        checker = type_checker(ctx.api)
        result: Type = records
        for asked in self._asked:
            after = asked.model == model and asked.line <= ctx.context.line
            if after and not checker.has_valid_attribute(result, asked.name):
                result = checker.add_any_attribute_to_type(result, asked.name)
        return result


def _keeping(records: Type) -> frozenset[str]:
    """The attributes of records that give the same records again (:data:`KEEPING`).

    Also every method of their classes whose declared result is the records
    themselves (``-> Self``: ``union``, ``concat``, ``new`` of Odoo, one of an addon),
    as the class says: what the method returns carries a field given to the records.
    """
    proper = receiver_type(records)
    if not isinstance(proper, Instance):
        return KEEPING
    found = set(KEEPING)
    for info in proper.type.mro:
        for name, node in info.names.items():
            if name not in found and _returns_self(node.node, info):
                found.add(name)
    return frozenset(found)


def _returns_self(node: SymbolNode | None, owner: TypeInfo) -> bool:
    """Whether a method's declared result may hold its receiver.

    ``Self`` or the class itself, also inside another type: ``grouped() -> dict[Any,
    Self]`` of Odoo 19, ``list[Self]``, ``Self | None``.
    """
    if isinstance(node, Decorator):
        node = node.func
    signature = getattr(node, "type", None)
    items = signature.items if isinstance(signature, Overloaded) else [signature]
    return any(
        isinstance(item, CallableType) and item.ret_type.accept(_HoldsReceiver(owner))
        for item in items
    )


class _HoldsReceiver(TypeQuery[bool]):
    """Whether a type mentions a type variable or a class (the receiver's) anywhere."""

    def __init__(self, owner: TypeInfo) -> None:
        super().__init__()
        self.owner = owner

    def strategy(self, items: list[bool]) -> bool:
        return any(items)

    def visit_type_var(self, t: TypeVarType, /) -> bool:
        return True

    def visit_instance(self, t: Instance, /) -> bool:
        return t.type is self.owner or super().visit_instance(t)


def _dotted(expression: Expression) -> str | None:
    """A name or the attributes of one, as written (``self``, ``order.partner_id``)."""
    if isinstance(expression, NameExpr):
        return expression.name
    if isinstance(expression, MemberExpr):
        owner = _dotted(expression.expr)
        return f"{owner}.{expression.name}" if owner is not None else None
    return None


def _line_calls(scope: Node) -> dict[int, list[CallExpr]]:
    """The calls of a function (or module) by their line."""
    found: dict[int, list[CallExpr]] = {}
    for node in get_subexpressions(scope):
        if isinstance(node, CallExpr):
            found.setdefault(node.line, []).append(node)
    return found


def _may_end(calls: dict[int, list[CallExpr]], line: int) -> bool:
    """Whether a call on a line may not return: declared ``NoReturn``, or not known.

    The records of the question are typed and mypy has not checked the statements of
    the branch yet, so the callee is taken from what semantic analysis bound: a
    function, a method of a name whose type is known (``self.fail()``), a class.
    """
    return any(_may_not_return(call) for call in calls.get(line, ()))


def _may_not_return(call: CallExpr) -> bool:
    callee = call.callee
    node = callee.node if isinstance(callee, RefExpr) else None
    if node is None and isinstance(callee, MemberExpr) and isinstance(callee.expr, NameExpr):
        owner = callee.expr.node
        typ = get_proper_type(owner.type) if isinstance(owner, Var) else None
        found = typ.type.get(callee.name) if isinstance(typ, Instance) else None
        node = found.node if found is not None else None
    if isinstance(node, TypeInfo):
        return False
    if isinstance(node, Decorator):
        node = node.func
    if not isinstance(node, FuncBase | Var):
        return True
    signature = get_proper_type(node.type) if node.type is not None else None
    if signature is None:
        return isinstance(node, Var)  # an unannotated function returns
    items = signature.items if isinstance(signature, Overloaded) else [signature]
    return any(
        not isinstance(item, CallableType)
        or isinstance(get_proper_type(item.ret_type), UninhabitedType)
        for item in items
    )


def _read_nodes(
    reads: list[tuple[int, int, int, int]], name: str, records: str, ctx: MethodContext
) -> list[Context]:
    """The nodes mypy has for the reads the source scan found, to report at (their columns).

    The scan gives the columns of Python's parser, in bytes; mypy's differ on a line
    with other than ASCII before the read (mypy 2.4 counts otherwise than 2.1). Each
    read is mypy's ``records.name`` of the same rank on its line; where mypy has
    another number of them there, the scan's position is kept.
    """
    checker = type_checker(ctx.api)
    scope = checked_function(ctx.api) or checker.tree
    found: dict[int, list[MemberExpr]] = {}
    for node in get_subexpressions(scope):
        if isinstance(node, MemberExpr) and node.name == name and _dotted(node.expr) == records:
            found.setdefault(node.line, []).append(node)
    contexts: list[Context] = []
    for line, column, rank, count in reads:
        nodes = sorted(found.get(line, []), key=lambda node: node.column)
        contexts.append(nodes[rank] if len(nodes) == count else Context(line, column))
    return contexts
