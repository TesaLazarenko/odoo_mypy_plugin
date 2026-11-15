"""The parts a domain expression is made of: literal term lists and typed domains.

``self.search(AND([self._open_domain(), [("state", "=", "draft")]]))`` is made of a
literal list, whose terms are checked one by one (:mod:`odoo_typing.domains`), and of a
domain whose model its declaration names (``-> "odoo.domain.sale_order"``), which is
checked only against the model the consumer takes (:class:`TypedPart`).

The walk goes through ``a + b`` of lists, ``a & b`` / ``a | b`` / ``~a`` of ``Domain``
objects, ``Domain([...])`` / ``Domain(path, operator, value)`` / ``Domain.AND`` / ``OR``
(Odoo 19+), ``expression.AND`` / ``OR``, ``a if c else b``, ``a or b``, the plugin's own
wrapper of annotated expressions (:mod:`odoo_typing.annotations`) and, with the
function's :class:`~odoo_typing.local_values.LocalValues`, a variable built before the
consumer (``domain = [...]``, ``domain += [...]``, ``domain.append(term)``...).

The model of a typed part is read from a declaration, never from the type of the
expression (mypy simplifies the type of a call, which drops the marker):

* a call: the return type of the callee's declared type;
* ``cast(<domain type>, e)``: the type of the cast;
* a name or attribute with a domain annotation: the declared type of its variable;
* a name without one: the model every binding before the consumer gives
  (:meth:`DomainParts.agreed_model`); a parameter, a global or a name of an enclosing
  function has an unknown binding as well, so it never agrees.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from typing import Final

from mypy.nodes import (
    ARG_POS,
    LDEF,
    CallExpr,
    CastExpr,
    ConditionalExpr,
    Expression,
    ListExpr,
    MemberExpr,
    NameExpr,
    OpExpr,
    RefExpr,
    TupleExpr,
    UnaryExpr,
    Var,
)

from odoo_typing.local_values import MAX_DEPTH, Built, Change, LocalValues

__all__ = [
    "COMBINATORS",
    "WRAPPER",
    "DeclaredModel",
    "DomainParts",
    "LiteralPart",
    "Part",
    "TypedPart",
]

# ``expression.AND([...])`` / ``OR`` (``odoo.osv.expression``, Odoo 17-19).
COMBINATORS: Final = frozenset({"odoo.osv.expression.AND", "odoo.osv.expression.OR"})
# The wrapper of annotated expressions (odoo_typing.annotations): ``__build_class__(E, ...)``.
WRAPPER: Final = "builtins.__build_class__"
# The in-place changes (Change.method) that add terms to a list (``x += [...]``...), not
# ``Domain`` operators.
LIST_CHANGES: Final = frozenset({"__iadd__", "extend", "append", "insert"})


@dataclass(frozen=True, slots=True)
class LiteralPart:
    """A literal list of domain items.

    Attributes:
        items: The items (terms and operators).
        whole: Whether the list is a whole domain (an argument, a return value, an item
            of ``AND([...])``...), not a piece of one (``["|"] + a + b``): only then
            must its operators have their terms.
        model: The model of the annotated name the list was written to, if any; the
            terms are checked against it instead of the consumer's model.
        condition: The one term of ``Domain(path, operator, value)``.
    """

    items: Sequence[Expression]
    whole: bool
    model: str | None = None
    condition: bool = False


@dataclass(frozen=True, slots=True)
class TypedPart:
    """A domain whose model a declaration names (``-> "odoo.domain.x"``).

    Attributes:
        node: The expression, where a message about it points.
        model: The model.
    """

    node: Expression
    model: str


Part = LiteralPart | TypedPart
# The model a call, cast, name or attribute is declared a domain of (None: not one).
DeclaredModel = Callable[[Expression], str | None]


class DomainParts:
    """Walk domain expressions of one function into their parts.

    Args:
        domain_class: Full name of ``Domain`` (Odoo 19+), ``None`` before.
        local: The variables of the function the expression is in, if any.
        declared: The model an expression's declaration names (see the module).
    """

    def __init__(
        self, domain_class: str | None, local: LocalValues | None, declared: DeclaredModel
    ) -> None:
        self._domain_class = domain_class
        self._local = local
        self._declared = declared

    def parts(self, domain: Expression, whole: bool = True) -> list[Part]:
        """The parts of a domain expression (``whole``: it is a whole domain)."""
        return list(self._parts(domain, whole, None, True, 0))

    def _parts(
        self, node: Expression, whole: bool, model: str | None, typed: bool, depth: int
    ) -> Iterator[Part]:
        """The parts of an expression.

        Args:
            node: The expression.
            whole: Whether it is a whole domain.
            model: The model of the annotated name the expression was written to.
            typed: Whether typed parts are wanted (not inside a variable's bindings:
                the variable itself is the typed part there).
            depth: How many variables deep the walk is.
        """
        if isinstance(node, ListExpr):
            yield LiteralPart(node.items, whole, model)
        elif isinstance(node, OpExpr) and node.op in {"+", "&", "|", "or"}:
            # ``a + b`` joins lists (pieces); ``&`` / ``|`` combine Domain objects and
            # ``or`` picks one: their operands are whole domains.
            operands = whole if node.op == "or" else node.op != "+"
            yield from self._parts(node.left, operands, model, typed, depth)
            yield from self._parts(node.right, operands, model, typed, depth)
        elif isinstance(node, UnaryExpr) and node.op == "~":
            yield from self._parts(node.expr, True, model, typed, depth)
        elif isinstance(node, ConditionalExpr):
            yield from self._parts(node.if_expr, whole, model, typed, depth)
            yield from self._parts(node.else_expr, whole, model, typed, depth)
        elif isinstance(node, CallExpr):
            yield from self._call_parts(node, whole, model, typed, depth)
        elif isinstance(node, NameExpr):
            yield from self._name_parts(node, whole, model, typed, depth)
        elif isinstance(node, CastExpr | MemberExpr) and typed:
            found = self._declared(node)
            if found is not None:
                yield TypedPart(node, found)

    def _call_parts(
        self, call: CallExpr, whole: bool, model: str | None, typed: bool, depth: int
    ) -> Iterator[Part]:
        if isinstance(call.analyzed, CastExpr):  # cast(...) keeps the call, analyzed
            yield from self._parts(call.analyzed, whole, model, typed, depth)
            return
        callee = call.callee
        if isinstance(callee, RefExpr) and callee.fullname == WRAPPER and len(call.args) == 3:
            yield from self._parts(call.args[0], whole, model, typed, depth)
            return
        if any(kind != ARG_POS for kind in call.arg_kinds):
            return
        domain_class = self._domain_class
        if (
            domain_class is not None
            and isinstance(callee, RefExpr)
            and callee.fullname == domain_class
        ):
            if len(call.args) == 1:  # Domain([...]), Domain(other)
                yield from self._parts(call.args[0], True, model, typed, depth)
            elif len(call.args) == 3:  # Domain(path, operator, value)
                yield LiteralPart([TupleExpr(list(call.args))], True, model, condition=True)
            return
        if _is_combinator(callee, domain_class):
            argument = call.args[0] if len(call.args) == 1 else None
            if isinstance(argument, ListExpr | TupleExpr):
                for item in argument.items:
                    yield from self._parts(item, True, model, typed, depth)
            return
        if typed:
            found = self._declared(call)
            if found is not None:
                yield TypedPart(call, found)

    def _name_parts(
        self, name: NameExpr, whole: bool, model: str | None, typed: bool, depth: int
    ) -> Iterator[Part]:
        """A variable: its declared model, and what it was built from in the function."""
        local = self._local
        annotated = self._declared(name)
        if annotated is not None:
            if typed:
                yield TypedPart(name, annotated)
            if local is None or not self._is_local(name, local):
                return
            model = annotated  # its literals are checked against the annotation
        elif typed and (agreed := self.agreed_model(name)) is not None:
            yield TypedPart(name, agreed)
        if local is None or depth >= MAX_DEPTH:
            return
        built = local.built(name, name.line)
        yield from self._built_parts(built, whole, model, depth + 1)

    def _built_parts(
        self, built: Built, whole: bool, model: str | None, depth: int
    ) -> Iterator[Part]:
        """The parts of a variable's value and of its in-place changes."""
        value, changes = built.value, built.changes
        if not changes:
            if value is not None:
                yield from self._parts(value, whole, model, False, depth)
            return
        assembled = _assembled(value, changes) if _one_path(built) else None
        if assembled is not None:  # d = ["|"]; d += [a, b]: one list, ["|", a, b]
            yield LiteralPart(assembled, whole, model)
            return
        pieces = any(change.method in LIST_CHANGES for change in changes)
        if value is not None:
            yield from self._parts(value, whole and not pieces, model, False, depth)
        for change in changes:
            yield from self._changed_parts(change, model, depth)

    def _changed_parts(self, change: Change, model: str | None, depth: int) -> Iterator[Part]:
        """The parts an in-place change adds to a domain variable."""
        args = change.args
        if change.method in {"__ior__", "__iand__"} and len(args) == 1:
            yield from self._parts(args[0], True, model, False, depth)
        elif change.method in {"__iadd__", "extend"} and len(args) == 1:
            yield from self._parts(args[0], False, model, False, depth)
        elif change.method == "append" and len(args) == 1:
            yield LiteralPart([args[0]], False, model)
        elif change.method == "insert" and len(args) == 2:
            yield LiteralPart([args[1]], False, model)

    def agreed_model(self, name: NameExpr, depth: int = 0) -> str | None:
        """The model of a name without a domain annotation, if its bindings agree on one.

        The bindings in the function before the name's line count; a parameter, a
        global or a name of an enclosing function also has an unknown binding (from
        the caller, the module, the outer function), and a binding later in a loop that
        runs the line again makes the value unknown. There is a model only if every
        binding is a typed part (:class:`TypedPart`) of the same model, or another such
        name (``d1 = d0``), followed up to :data:`MAX_DEPTH` names deep.
        """
        local = self._local
        if local is None or not self._is_local(name, local) or _is_parameter(name, local):
            return None
        values = local.bindings_before(name, name.line)
        if not values:
            return None
        models = {self._binding_model(value, depth) for value in values}
        if len(models) != 1:
            return None
        return models.pop()

    def _binding_model(self, value: Expression | None, depth: int) -> str | None:
        if value is None:
            return None
        found = self._declared(value)
        if found is None and isinstance(value, NameExpr) and depth < MAX_DEPTH:
            return self.agreed_model(value, depth + 1)
        return found

    @staticmethod
    def _is_local(name: NameExpr, local: LocalValues) -> bool:
        """Whether a name is a variable (or parameter) of the function, bound in it."""
        if name.kind != LDEF or not isinstance(name.node, Var):
            return False
        return _is_parameter(name, local) or bool(local.bindings(name))


def _is_parameter(name: NameExpr, local: LocalValues) -> bool:
    return any(argument.variable is name.node for argument in local.function.arguments)


def _is_combinator(callee: Expression, domain_class: str | None) -> bool:
    """``expression.AND`` / ``OR``, or ``Domain.AND`` / ``OR`` (Odoo 19+)."""
    if isinstance(callee, RefExpr) and callee.fullname in COMBINATORS:
        return True
    return (
        domain_class is not None
        and isinstance(callee, MemberExpr)
        and callee.name in {"AND", "OR"}
        and isinstance(callee.expr, RefExpr)
        and callee.expr.fullname == domain_class
    )


def _one_path(built: Built) -> bool:
    """Whether a variable's binding and changes run in one execution, each once.

    Not when a change is in a loop that its binding is not in (it may run any number of
    times), or two of them are in other arms of one ``if`` / ``try`` (never both run).
    """
    loops = {key for kind, key, _ in built.path if kind == "loop"}
    arms: dict[int, int] = {}
    paths = (built.path, *(change.path for change in built.changes))
    for kind, key, arm in (step for path in paths for step in path):
        if kind == "loop":
            if key not in loops:
                return False
        elif arms.setdefault(key, arm) != arm:
            return False
    return True


def _assembled(value: Expression | None, changes: Sequence[Change]) -> list[Expression] | None:
    """The items of a list display extended in place with lists and ``append``, if it is one."""
    if not isinstance(value, ListExpr):
        return None
    items = list(value.items)
    for change in changes:
        args = change.args
        if change.method in {"__iadd__", "extend"} and len(args) == 1:
            if not isinstance(args[0], ListExpr):
                return None
            items += args[0].items
        elif change.method == "append" and len(args) == 1:
            items.append(args[0])
        else:
            return None
    return items
