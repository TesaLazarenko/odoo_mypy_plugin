"""What the code of a function proves about an expression where it is evaluated.

mypy narrows names and member expressions, not calls: after
``if not self.env.context.get("active_ids"): raise ...`` the next
``self.env.context.get("active_ids")`` is ``Any | None`` again, and so is
``env.ref("base.partner", raise_if_not_found=False)`` after the same call was bound to
``partner`` and ``if not partner: raise ...`` tested it. :func:`proven` reads the facts
that hold where an expression is evaluated, along the path from the function's body to
it:

* the tests of the ``if`` / ``elif`` / ``while`` arms it is in (true), and those of the
  arms before (false);
* an earlier statement of an enclosing block that ends unless its test is false
  (``if not x: return`` / ``raise`` / ``continue`` / ``break`` / ``self.skipTest(...)``),
  an ``assert``, and a call that raises unless records are there: ``x.ensure_one()``
  (Odoo's ``len(self) != 1`` raises), ``self.assertEqual(len(x), n)`` (n >= 1) and
  ``self.assertRecordValues(x, [...])`` of a list that is not empty (it compares the
  records with the list item by item);
* the left operand of ``and`` / ``or`` and the test of a conditional expression it is
  an operand of, and the conditions of a comprehension it is the element of (also
  inside a lambda or a comprehension).

A test gives facts through ``not``, ``and`` / ``or``, ``x is None`` / ``x is not None``
and ``len(x)``, ``len(x) < n``, ``len(x) == n`` (also of ``x or []``), ``bool(x)``,
``x.ids`` and ``x.exists()`` (matched by name: Odoo's records are true when these are),
a true read of a field of the records (``if found.partner_id:``, given the names of
their fields: Odoo's ``Field.__get__`` reads the null value of a field off no records,
``False``, ``0`` or no records, which is false; also ``found.line_ids.ids``,
``len(found.line_ids)``, ``bool(found.partner_id)``, but not a field of the comodel,
``found.partner_id.name``), and through a local name to the value of
its last binding, when that one surely ran (below; ``present = found.exists()``, then
``if present:`` proves ``found``; not the truth of a local filled in place,
``rows.append(v)``, see
:func:`odoo_typing.rebound.filled_names`). A fact is about an expression
with the same structure (``context.get("k")`` and ``context["k"]`` read the same
item), and holds while no name it reads is bound again between the test and the
expression; none holds when ``:=`` binds such a name in the part of the statement
holding the expression that runs with it (its own expressions, an ``if`` or ``while``
test; not its blocks, nor a ``match`` guard, which runs after the subject): in ``x.f
if (x := v) else ...`` it runs first. The callers decide which expressions keep their
value (a read of ``env.context``, ``env.ref``). A fact from
before a loop does not hold in it when the loop binds a name the expression reads: the
binding runs before the expression on the next pass; nor does one from outside a lambda
or a generator that holds the expression when a later statement of the function binds
such a name (it may run after it; a generator a builtin such as ``any`` iterates runs
at once). A fact about a local passes to the value of its last binding before the fact
(``x |= v`` binds it too) when that binding runs before the expression each time it
runs (a statement of a block on the way to it, not under another ``if``), from that
binding on, only while the local still holds it where the fact is used: not when a
loop the fact is in binds the local again, nor when a lambda or generator the fact is
in may run after it does.
``self.assertRecordValues(x, [*rows])`` proves nothing (``rows`` may be empty);
``[{...}, *rows]`` does.
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Iterator
from dataclasses import dataclass
from typing import Literal

from mypy.nodes import (
    ARG_POS,
    AssertStmt,
    AssignmentExpr,
    AssignmentStmt,
    Block,
    BreakStmt,
    CallExpr,
    ClassDef,
    ComparisonExpr,
    ConditionalExpr,
    ContinueStmt,
    Decorator,
    DictExpr,
    DictionaryComprehension,
    Expression,
    ExpressionStmt,
    ForStmt,
    FuncDef,
    FuncItem,
    GeneratorExpr,
    IfStmt,
    IndexExpr,
    IntExpr,
    LambdaExpr,
    ListComprehension,
    ListExpr,
    MatchStmt,
    MemberExpr,
    NameExpr,
    OperatorAssignmentStmt,
    OpExpr,
    RaiseStmt,
    ReturnStmt,
    SetComprehension,
    StarExpr,
    Statement,
    StrExpr,
    TupleExpr,
    UnaryExpr,
    Var,
    WhileStmt,
    WithStmt,
)
from mypy.server.subexpr import get_subexpressions

from odoo_typing.guards import SKIP_TEST
from odoo_typing.rebound import filled_names, function_bindings, nested_blocks, walrus_targets

__all__ = ["Proof", "proven"]

Proof = Literal["truthy", "not_none"]
ENSURE_ONE = "ensure_one"
ASSERT_EQUAL = "assertEqual"
ASSERT_RECORD_VALUES = "assertRecordValues"
IDS = "ids"
EXISTS = "exists"
# Builtins that iterate what they are given before they return.
ITERATING = frozenset(
    f"builtins.{name}"
    for name in ("any", "all", "sum", "min", "max", "sorted", "list", "tuple", "set", "frozenset")
)
FALSY_NAMES = frozenset({"builtins.False", "builtins.None"})
MAX_DEPTH = 16  # nested tests followed for facts


@dataclass(frozen=True, slots=True)
class _Fact:
    """An expression that is true (or not ``None``) from a place on (see :func:`_place`)."""

    expression: Expression
    truthy: bool  # ``False``: only not ``None``
    line: int


def proven(function: FuncItem, target: Expression, fields: Collection[str] = ()) -> Proof | None:
    """What the function proves about an expression where it is evaluated (see the module).

    ``fields``: the names of the fields of the records ``target`` is, whose true read
    proves them (:func:`_read_off`).
    """
    facts: list[_Fact] = []
    if not _walk(function.body, target, facts):
        return None
    values, unplaced = _latest_values(function)
    if any(node.node in unplaced for node in _names(target)):
        return None  # bound again where no place is known: it may hold anything here
    if _walrus_beside(function.body, target):
        return None  # ``x.f if (x := v) else ...``: the binding may run first
    lines = {variable: [line for line, _, _ in bound] for variable, bound in values.items()}
    places = {
        variable: [(line, name.column) for line, _, name in bound]
        for variable, bound in values.items()
    }
    loops = _loops_around(function.body, target)
    # A lambda (or a generator) runs later: for a fact from outside it, a binding up to the
    # end of the function counts. Looked up once, when a binding after it may matter.
    around: list[list[Expression]] = []

    def delayed() -> list[Expression]:
        if not around:
            around.append(_delayed_around(function, target))
        return around[0]

    found: Proof | None = None
    at = (target.line, target.column)
    late = _bound_after(at, target, places)
    ran = _bindings_before(function.body, target)
    filled = filled_names(function)
    for fact in _with_values(facts, values, loops, delayed, ran, unplaced, filled):
        if not _same(fact.expression, target) and not _read_off(fact.expression, target, fields):
            continue
        nodes = delayed() if late else []
        outside = bool(nodes) and not _contains(nodes[-1], fact.expression)
        if not _unchanged(target, _place(fact), places, None if outside else at):
            continue
        if not _unchanged_in_loops(target, fact.line, lines, loops):
            continue
        if fact.truthy:
            return "truthy"
        found = "not_none"
    return found


def _walk(block: Block, target: Expression, facts: list[_Fact]) -> bool:
    """Collect the facts on the way to the statement of a block that holds the target."""
    for statement in block.body:
        if _contains(statement, target):
            return _enter(statement, target, facts)
        facts.extend(_after(statement))
    return False


def _enter(statement: Statement, target: Expression, facts: list[_Fact]) -> bool:
    if isinstance(statement, FuncDef | Decorator | ClassDef):
        return False  # runs later: what holds here need not hold there
    if isinstance(statement, IfStmt):
        for index, (test, body) in enumerate(zip(statement.expr, statement.body, strict=True)):
            if _contains(test, target):
                facts.extend(_false_before(statement, index))
                return _expression(test, target, facts)
            if _contains(body, target):
                facts.extend(_false_before(statement, index))
                facts.extend(_true(test, test.line))
                return _walk(body, target, facts)
        if statement.else_body is not None and _contains(statement.else_body, target):
            facts.extend(_false_before(statement, len(statement.expr)))
            return _walk(statement.else_body, target, facts)
        return False
    if isinstance(statement, WhileStmt):
        if _contains(statement.expr, target):
            return _expression(statement.expr, target, facts)
        if _contains(statement.body, target):
            facts.extend(_true(statement.expr, statement.expr.line))
            return _walk(statement.body, target, facts)
        return statement.else_body is not None and _walk(statement.else_body, target, facts)
    blocks = nested_blocks(statement)
    if blocks:
        expressions = _expressions(statement)
        for expression in expressions:
            if _contains(expression, target):
                return _expression(expression, target, facts)
        return any(_contains(inner, target) and _walk(inner, target, facts) for inner in blocks)
    return any(
        _contains(expression, target) and _expression(expression, target, facts)
        for expression in _expressions(statement)
    )


def _expression(expression: Expression, target: Expression, facts: list[_Fact]) -> bool:
    """Collect the facts of the operands before the target inside an expression."""
    if expression is target:
        return True
    if (
        isinstance(expression, OpExpr)
        and expression.op in {"and", "or"}
        and _contains(expression.right, target)
    ):
        if expression.op == "and":
            facts.extend(_true(expression.left, expression.line))
        else:
            facts.extend(_false(expression.left, expression.line))
        return _expression(expression.right, target, facts)
    if isinstance(expression, GeneratorExpr) and _contains(expression.left_expr, target):
        for group in expression.condlists:  # ``[x.f for x in xs if found]``
            for condition in group:
                facts.extend(_true(condition, condition.line))
        return _expression(expression.left_expr, target, facts)
    if isinstance(expression, ConditionalExpr):
        if _contains(expression.if_expr, target):
            facts.extend(_true(expression.cond, expression.line))
            return _expression(expression.if_expr, target, facts)
        if _contains(expression.else_expr, target):
            facts.extend(_false(expression.cond, expression.line))
            return _expression(expression.else_expr, target, facts)
    return any(
        _contains(child, target) and _expression(child, target, facts)
        for child in _children(expression)
    )


def _false_before(statement: IfStmt, index: int) -> Iterator[_Fact]:
    for test in statement.expr[:index]:
        yield from _false(test, test.line)


def _after(statement: Statement) -> list[_Fact]:
    """The facts that hold after a statement that completes."""
    if isinstance(statement, AssertStmt):
        return list(_true(statement.expr, statement.line))
    if isinstance(statement, ExpressionStmt) and isinstance(statement.expr, CallExpr):
        return [
            _Fact(records, True, statement.line)
            for found in _present(statement.expr)
            for records in _truthy_parts(found)
        ]
    if isinstance(statement, Block):
        return [fact for inner in statement.body for fact in _after(inner)]
    if isinstance(statement, IfStmt) and len(statement.expr) == 1:
        test = statement.expr[0]
        if _ends(statement.body[0]) and statement.else_body is None:
            return list(_false(test, statement.line))
        ends = statement.else_body is not None and _ends(statement.else_body)
        if ends and not _ends(statement.body[0]):
            return list(_true(test, statement.line))
    return []


def _ends(block: Block) -> bool:
    """Whether a block never completes: it returns, raises, continues or breaks."""
    if not block.body:
        return False
    last = block.body[-1]
    if isinstance(last, ReturnStmt | RaiseStmt | ContinueStmt | BreakStmt):
        return True
    if (
        isinstance(last, ExpressionStmt)
        and isinstance(last.expr, CallExpr)
        and isinstance(last.expr.callee, MemberExpr)
        and last.expr.callee.name == SKIP_TEST
    ):
        return True  # unittest's skipTest raises SkipTest
    if isinstance(last, IfStmt) and last.else_body is not None:
        return all(_ends(body) for body in last.body) and _ends(last.else_body)
    return isinstance(last, Block) and _ends(last)


def _true(test: Expression, line: int, depth: int = 0) -> Iterator[_Fact]:
    """The facts a true test gives."""
    if depth > MAX_DEPTH:
        return
    for part in _truthy_parts(test):
        yield _Fact(part, True, line)
    if isinstance(test, OpExpr) and test.op == "and":
        yield from _true(test.left, line, depth + 1)
        yield from _true(test.right, line, depth + 1)
    elif isinstance(test, UnaryExpr) and test.op == "not":
        yield from _false(test.expr, line, depth + 1)
    elif isinstance(test, ComparisonExpr):
        yield from _compared(test, line, negated=False)


def _false(test: Expression, line: int, depth: int = 0) -> Iterator[_Fact]:
    """The facts a false test gives."""
    if depth > MAX_DEPTH:
        return
    if isinstance(test, UnaryExpr) and test.op == "not":
        yield from _true(test.expr, line, depth + 1)
    elif isinstance(test, OpExpr) and test.op == "or":
        yield from _false(test.left, line, depth + 1)
        yield from _false(test.right, line, depth + 1)
    elif isinstance(test, ComparisonExpr):
        yield from _compared(test, line, negated=True)


def _compared(test: ComparisonExpr, line: int, negated: bool) -> Iterator[_Fact]:
    """``x is (not) None`` and ``len(x) < n`` / ``len(x) == n``, true or (``negated``) false."""
    if len(test.operators) != 1:
        return
    operator = test.operators[0]
    left, right = test.operands
    is_none = isinstance(right, NameExpr) and right.fullname == "builtins.None"
    if is_none and operator == ("is" if negated else "is not"):
        yield _Fact(left, False, line)
        return
    size = _len_argument(left)
    if size is None or not isinstance(right, IntExpr):
        return
    # Whether the test says len(x) >= 1: a false ``len(x) < n``, ``!= n`` (n >= 1) or
    # ``== 0``, a true ``len(x) >= n``, ``== n`` (n >= 1), ``> n`` or ``!= 0``.
    value = right.value
    small_when_true = (
        (operator == "<" and value >= 1)
        or (operator == "==" and value == 0)
        or (operator == "!=" and value >= 1)
    )
    sized_when_true = (
        (operator == ">=" and value >= 1)
        or (operator == ">" and value >= 0)
        or (operator == "!=" and value == 0)
        or (operator == "==" and value >= 1)
    )
    if (negated and small_when_true) or (not negated and sized_when_true):
        for part in _truthy_parts(size):
            yield _Fact(part, True, line)


def _len_argument(expression: Expression) -> Expression | None:
    return _builtin_argument(expression, "builtins.len")


def _builtin_argument(expression: Expression, fullname: str) -> Expression | None:
    """``x`` of a call ``f(x)`` of a builtin (``fullname``: ``builtins.len``)."""
    if (
        isinstance(expression, CallExpr)
        and isinstance(expression.callee, NameExpr)
        and expression.callee.fullname == fullname
        and len(expression.args) == 1
    ):
        return expression.args[0]
    return None


def _truthy_parts(expression: Expression) -> Iterator[Expression]:
    """An expression that is true, and what it being true makes true (``x or []``: ``x``).

    A true ``len(x)`` makes ``x`` true (a container is true when it has items), and a
    true ``bool(x)`` (the truth of ``x`` itself). So do ``x.ids`` and ``x.exists()``,
    matched by name as Odoo's records have them (:func:`_records_of`).
    """
    yield expression
    if isinstance(expression, OpExpr) and expression.op == "or" and _falsy(expression.right):
        yield from _truthy_parts(expression.left)
    for inner in (
        _len_argument(expression),
        _builtin_argument(expression, "builtins.bool"),
        _records_of(expression),
    ):
        if inner is not None:
            yield from _truthy_parts(inner)


def _read_off(expression: Expression, target: Expression, fields: Collection[str]) -> bool:
    """Whether an expression reads one of the fields off the target (``found.partner_id``).

    Off no records Odoo reads the null value of a field (``Field.__get__``), and that is
    false (``False``, ``0``, ``0.0``, no records): a true read proves there are records.
    A field read off another one (``found.partner_id.name``) is not followed: which
    names are fields of the comodel is not known here.
    """
    return (
        isinstance(expression, MemberExpr)
        and expression.name in fields
        and _same(expression.expr, target)
    )


def _records_of(expression: Expression) -> Expression | None:
    """``x`` of ``x.ids`` and ``x.exists()``: records true only when ``x`` is.

    Odoo's ``ids`` lists ids of ``x._ids``, ``exists()`` (it takes no argument) gives
    ``x`` or records of some of its ids, and records are true when ``_ids`` is not empty
    (``BaseModel.__bool__``).
    """
    if isinstance(expression, MemberExpr) and expression.name == IDS:
        return expression.expr
    if isinstance(expression, CallExpr) and not expression.args:
        callee = expression.callee
        if isinstance(callee, MemberExpr) and callee.name == EXISTS:
            return callee.expr
    return None


def _present(call: CallExpr) -> Iterator[Expression]:
    """What a call that completes proves there are records of (see the module)."""
    callee = call.callee
    if not isinstance(callee, MemberExpr):
        return
    if callee.name == ENSURE_ONE and not call.args:
        yield callee.expr
    elif callee.name == ASSERT_EQUAL and len(call.args) >= 2:
        first, second = call.args[:2]
        for size, value in ((first, second), (second, first)):
            sized = _len_argument(size)
            if sized is not None and isinstance(value, IntExpr) and value.value >= 1:
                yield sized
    elif callee.name == ASSERT_RECORD_VALUES:
        records = _argument(call, 0, "records")
        expected = _argument(call, 1, "expected_values")
        if (
            records is not None
            and isinstance(expected, ListExpr)
            and any(not isinstance(item, StarExpr) for item in expected.items)
        ):
            yield records  # ``[*rows]`` may be empty, ``[{...}, *rows]`` is not


def _argument(call: CallExpr, position: int, name: str) -> Expression | None:
    """The argument of a call for a parameter, given by position or by name."""
    for index, (kind, given) in enumerate(zip(call.arg_kinds, call.arg_names, strict=True)):
        if given == name or (given is None and kind == ARG_POS and index == position):
            return call.args[index]
    return None


def _falsy(expression: Expression) -> bool:
    if isinstance(expression, NameExpr):
        return expression.fullname in FALSY_NAMES
    if isinstance(expression, ListExpr | TupleExpr | DictExpr):
        return not expression.items
    if isinstance(expression, IntExpr | StrExpr):
        return not expression.value
    return False


Binding = tuple[int, Expression | None, NameExpr]  # line, value (``None``: not known), name


def _latest_values(function: FuncItem) -> tuple[dict[Var, list[Binding]], set[Var]]:
    """The bindings of each local variable in source order, and those bound unplaced.

    ``x |= v`` and ``(x := v)`` bind ``x`` too, to a value not known here (``None``); an
    ``except ... as x`` and a ``case`` capture bind it where no place is known.
    """
    collected = function_bindings(function)
    found = {
        variable: [(name.line, value, name) for name, value in names]
        for variable, names in collected.names.items()
    }
    unknown = [*collected.augmented, *walrus_targets(function)]
    for name in unknown:
        if isinstance(name, NameExpr) and isinstance(name.node, Var):
            found.setdefault(name.node, []).append((name.line, None, name))
    ordered = {
        variable: sorted(bound, key=lambda item: (item[0], item[2].column))
        for variable, bound in found.items()
    }
    return ordered, set(collected.excluded)


def _bindings_before(block: Block, target: Expression) -> set[int]:
    """The names an assignment binds that runs before the target each time it runs.

    An assignment that is a statement of a block on the way from the body to the target,
    before the statement that holds the target there: also on each pass of a loop.
    """
    found: set[int] = set()
    pending = [block]
    while pending:
        current = pending.pop()
        for statement in current.body:
            if _contains(statement, target):
                pending.extend(
                    inner for inner in nested_blocks(statement) if _contains(inner, target)
                )
                break
            if isinstance(statement, AssignmentStmt):
                found.update(
                    id(name) for lvalue in statement.lvalues for name in _bound_names(lvalue)
                )
    return found


def _bound_names(lvalue: Expression) -> Iterator[NameExpr]:
    """The names an assignment target binds (``a, (b, c) = ...``)."""
    if isinstance(lvalue, NameExpr):
        yield lvalue
    elif isinstance(lvalue, TupleExpr | ListExpr):
        for item in lvalue.items:
            yield from _bound_names(item)


def _walrus_beside(block: Block, target: Expression) -> bool:
    """Whether ``:=`` binds a name the target reads in the statement that holds the target.

    Out of its blocks: in its own expressions (:func:`_expressions`). Its
    place there need not tell whether it runs before the target: the test of a
    conditional expression and the conditions of a comprehension run before what is
    left of them, and an assignment's value before its targets.
    """
    statement = _holding(block, target)
    if statement is None:
        return False
    read = {node.node for node in _names(target) if isinstance(node.node, Var)}
    return any(
        isinstance(node, AssignmentExpr) and node.target.node in read
        for head in _expressions(statement)
        for node in get_subexpressions(head)
    )


def _holding(block: Block, target: Expression) -> Statement | None:
    """The innermost statement that holds the target, out of the blocks it has."""
    found: Statement | None = None
    current: Block | None = block
    while current is not None:
        inner: Block | None = None
        for statement in current.body:
            if _contains(statement, target):
                found = statement
                inner = next(
                    (each for each in nested_blocks(statement) if _contains(each, target)),
                    None,
                )
                break
        current = inner
    return found


def _with_values(
    facts: list[_Fact],
    values: dict[Var, list[Binding]],
    loops: list[tuple[int, int]],
    delayed: Callable[[], list[Expression]],
    ran: set[int],
    unplaced: set[Var],
    filled: set[Var],
) -> Iterator[_Fact]:
    """The facts, and for a fact about a local name what it proves about its value then.

    A true local gives the facts a true test of its value gives (``present =
    found.exists()``, then ``if present:``: ``found``; ``single = len(found) == 1``),
    except those that read a name the binding binds (``x = self = ... self ...``: the old
    ``self``), and the same fact of the other names it binds (``x = found = ...``); one
    not ``None``, that its
    value is not ``None``. Not for a local bound where no place is known (``except ...
    as``, ``case``: ``unplaced``), nor the truth of one filled in place (``rows.append(v)``:
    true whatever it was bound to; ``filled``). Not when the name may hold another value
    where the fact is used: bound again later
    in a loop the fact is in (the next pass tests the new value), or after a fact in a
    lambda or a generator (it runs later). Only from the local's last binding before the
    fact, and when that binding runs before the target each time it runs (``ran``, see
    :func:`_bindings_before`): one under an ``if``, or one a later pass of a loop may
    skip, leaves the value the local holds unknown, as a parameter's. The fact about the
    value holds from the binding on: the value's own names bound again since then make
    it another one.
    """
    for fact in facts:
        yield fact
        name = fact.expression
        if not isinstance(name, NameExpr) or not isinstance(name.node, Var):
            continue
        if name.node in unplaced or (fact.truthy and name.node in filled):
            continue
        bindings = values.get(name.node, [])
        at = (name.line, name.column)
        later = [line for line, _, bound in bindings if (line, bound.column) > at]
        if later and (
            any(
                first <= fact.line <= last and any(first <= line <= last for line in later)
                for first, last in loops
            )
            or any(_contains(node, fact.expression) for node in delayed())
        ):
            continue
        earlier = [binding for binding in bindings if (binding[0], binding[2].column) <= at]
        if not earlier:
            continue
        line, value, bound = earlier[-1]
        if value is None or id(bound) not in ran:
            continue
        derived = _true(value, line) if fact.truthy else iter([_Fact(value, False, line)])
        # the names the assignment binds: ``self = ctx.get("k") and self.with_context({})``
        # (or ``x = self = ...``) read the old ``self`` there, the value bound after it
        others = [
            other for entries in values.values() for _, each, other in entries if each is value
        ]
        targets = {other.node for other in others}
        for each in derived:
            if not any(node.node in targets for node in _names(each.expression)):
                yield each
        # ``x = found = found.exists()``: the other targets hold the same object
        for other in others:
            yield _Fact(other, fact.truthy, line)


Place = tuple[int, int]  # line and column in the source


def _place(fact: _Fact) -> Place:
    """Where a fact starts to hold: its expression, on the fact's line (a binding's value)."""
    expression = fact.expression
    return (fact.line, expression.column if expression.line == fact.line else -1)


def _bound_after(at: Place, target: Expression, places: dict[Var, list[Place]]) -> bool:
    """Whether a name the target (at ``at``) reads is bound after it in the source."""
    return any(
        isinstance(node.node, Var) and any(place > at for place in places.get(node.node, []))
        for node in _names(target)
    )


def _delayed_around(function: FuncItem, target: Expression) -> list[Expression]:
    """The lambdas and generators that hold the target, the innermost last.

    A generator runs as it is iterated: that of a list or set comprehension at once, and
    one a builtin that iterates it takes (``any(...)``, ``sorted(...)``, ``"".join(...)``).
    """
    nodes = get_subexpressions(function)
    at_once = {
        id(node.generator)
        for node in nodes
        if isinstance(node, ListComprehension | SetComprehension)
    }
    for node in nodes:
        if isinstance(node, CallExpr) and _iterates(node):
            at_once.update(id(argument) for argument in node.args)
    around = [
        node
        for node in nodes
        if (
            isinstance(node, LambdaExpr)
            or (isinstance(node, GeneratorExpr) and id(node) not in at_once)
        )
        and _contains(node, target)
    ]
    return sorted(around, key=lambda node: (node.line, node.column))


def _iterates(call: CallExpr) -> bool:
    """Whether a call iterates its arguments before it returns."""
    callee = call.callee
    if isinstance(callee, NameExpr):
        return callee.fullname in ITERATING
    return isinstance(callee, MemberExpr) and callee.name == "join"


def _unchanged(
    target: Expression, since: Place, places: dict[Var, list[Place]], until: Place | None
) -> bool:
    """Whether no name the target reads is bound between a fact and ``until``.

    ``until`` is the target's place, or ``None`` (the end of the function) for a target
    in a lambda, which may run after any later statement. Places are lines and columns:
    ``if found: found = found.filtered(...); found.name`` binds it again on the line.
    """
    for node in _names(target):
        if isinstance(node.node, Var) and any(
            since < place and (until is None or place <= until)
            for place in places.get(node.node, [])
        ):
            return False
    return True


def _unchanged_in_loops(
    target: Expression, since: int, lines: dict[Var, list[int]], loops: list[tuple[int, int]]
) -> bool:
    """Whether no loop around the target, begun after the fact, binds a name it reads.

    Such a binding (later in the body, ``found = found.filtered(...)``) runs before the
    target on the next pass.
    """
    for first, last in loops:
        if since >= first:
            continue  # the fact is in the loop: it is tested again on each pass
        for node in _names(target):
            if isinstance(node.node, Var) and any(
                first <= line <= last for line in lines.get(node.node, [])
            ):
                return False
    return True


def _loops_around(block: Block, target: Expression) -> list[tuple[int, int]]:
    """The first and last lines of the ``for`` / ``while`` loops that hold the target."""
    found: list[tuple[int, int]] = []
    pending = [block]
    while pending:
        current = pending.pop()
        for statement in current.body:
            if not _contains(statement, target):
                continue
            # The iterable of a ``for`` is evaluated once, the test of a ``while`` each pass.
            repeated = isinstance(statement, WhileStmt) or (
                isinstance(statement, ForStmt) and not _contains(statement.expr, target)
            )
            if repeated:
                found.append((statement.line, statement.end_line or statement.line))
            pending.extend(nested_blocks(statement))
    return found


def _names(expression: Expression) -> Iterator[NameExpr]:
    pending = [expression]
    while pending:
        current = pending.pop()
        if isinstance(current, NameExpr):
            yield current
        elif isinstance(current, MemberExpr):
            pending.append(current.expr)
        else:
            pending.extend(_children(current))


def _same(first: Expression, second: Expression) -> bool:
    """Whether two expressions read the same value (``get("k")`` and ``["k"]`` alike)."""
    one, other = _item(first), _item(second)
    if one is not None and other is not None:
        return _same(one[0], other[0]) and _same(one[1], other[1])
    if type(first) is not type(second):
        return False
    if isinstance(first, NameExpr) and isinstance(second, NameExpr):
        return first.name == second.name and first.node is second.node
    if isinstance(first, MemberExpr) and isinstance(second, MemberExpr):
        return first.name == second.name and _same(first.expr, second.expr)
    if isinstance(first, StrExpr | IntExpr) and isinstance(second, StrExpr | IntExpr):
        return first.value == second.value
    if isinstance(first, CallExpr) and isinstance(second, CallExpr):
        return (
            _same(first.callee, second.callee)
            and first.arg_names == second.arg_names
            and first.arg_kinds == second.arg_kinds
            and all(_same(one, two) for one, two in zip(first.args, second.args, strict=True))
        )
    return False


def _item(expression: Expression) -> tuple[Expression, Expression] | None:
    """``m.get(k)`` (one argument) and ``m[k]``: the mapping and the key."""
    if isinstance(expression, IndexExpr):
        return expression.base, expression.index
    if (
        isinstance(expression, CallExpr)
        and isinstance(expression.callee, MemberExpr)
        and expression.callee.name == "get"
        and len(expression.args) == 1
        and expression.arg_names == [None]
    ):
        return expression.callee.expr, expression.args[0]
    return None


def _contains(node: Statement | Expression | Block, target: Expression) -> bool:
    """Whether a node's lines hold the target (lines and columns of the source)."""
    start = (node.line, node.column)
    where = (target.line, target.column)
    return start <= where <= _end(node)


def _end(node: Statement | Expression | Block) -> tuple[int, int]:
    """Where a node ends: mypy keeps no end of a lambda, so that of its body."""
    if isinstance(node, LambdaExpr):
        return max((node.line, node.column), _end(node.expr()))
    return (node.end_line or node.line, node.end_column if node.end_column is not None else 10**9)


def _expressions(statement: Statement) -> list[Expression]:
    """The expressions a simple statement (or the head of a compound one) evaluates.

    The tests of an ``if`` and of a ``while``; the subject of a ``match``, not its
    guards (they run after it, and a target in one is not reached: :func:`_enter`).
    """
    if isinstance(statement, IfStmt):
        return list(statement.expr)
    if isinstance(statement, WhileStmt):
        return [statement.expr]
    if isinstance(statement, AssignmentStmt):
        return [statement.rvalue, *statement.lvalues]
    if isinstance(statement, OperatorAssignmentStmt):
        return [statement.rvalue, statement.lvalue]
    if isinstance(statement, ExpressionStmt):
        return [statement.expr]
    if isinstance(statement, ReturnStmt | RaiseStmt):
        return [statement.expr] if statement.expr is not None else []
    if isinstance(statement, AssertStmt):
        return [statement.expr, *([statement.msg] if statement.msg is not None else [])]
    if isinstance(statement, ForStmt):
        return [statement.expr]
    if isinstance(statement, WithStmt):
        return list(statement.expr)
    if isinstance(statement, MatchStmt):
        return [statement.subject]
    return []


def _children(expression: Expression) -> list[Expression]:
    """The expressions directly inside an expression (lambda bodies and comprehensions too)."""
    if isinstance(expression, CallExpr):
        return [expression.callee, *expression.args]
    if isinstance(expression, MemberExpr):
        return [expression.expr]
    if isinstance(expression, IndexExpr):
        return [expression.base, expression.index]
    if isinstance(expression, OpExpr):
        return [expression.left, expression.right]
    if isinstance(expression, ComparisonExpr):
        return list(expression.operands)
    if isinstance(expression, UnaryExpr):
        return [expression.expr]
    if isinstance(expression, ConditionalExpr):
        return [expression.cond, expression.if_expr, expression.else_expr]
    if isinstance(expression, ListExpr | TupleExpr):
        return list(expression.items)
    if isinstance(expression, DictExpr):
        return [item for pair in expression.items for item in pair if item is not None]
    if isinstance(expression, LambdaExpr):
        return [expression.expr()]
    if isinstance(expression, ListComprehension | SetComprehension):
        return [expression.generator]
    if isinstance(expression, GeneratorExpr):
        conditions = [condition for group in expression.condlists for condition in group]
        return [*expression.sequences, *conditions, expression.left_expr]
    if isinstance(expression, DictionaryComprehension):
        conditions = [condition for group in expression.condlists for condition in group]
        items = [expression.key] if expression.key is not None else []  # ``{**x for ...}``
        return [*expression.sequences, *conditions, *items, expression.value]
    return []
