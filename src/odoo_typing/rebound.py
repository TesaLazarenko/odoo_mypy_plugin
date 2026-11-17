"""Local variables a function binds more than once: each value keeps its own type.

mypy takes a local variable's type from its first value and checks every later value
against it::

    products = []                                   # list[Any]
    products = self.env["product.product"].search(domain)     # error

    proposal = lead.company_id                      # res.company
    proposal = False                                # error

    total = 0                                       # int
    total += line.balance                           # error: float

The same goes for a ``for`` variable that two loops use over records of two models, or
``wizard`` that holds a wizard and then the invoice it made. While records were ``Any``
such code was silent; with records and field values typed, every later value of
another type is an error, though the code is right: Python variables take any value.

Before mypy checks a module of an addon, every local variable without an annotation
that its function binds more than once (``=``, the target of a ``for``, a ``with`` or an
unpacking, and ``x += v`` after an int literal: a list changes in place) is declared
``Any``, and its first binding stops being a definition. mypy narrows an inferred
variable declared ``Any`` to the type of each value assigned to it
(``ConditionalTypeBinder.assign_type``), so:

* after each binding the variable has the type of that value: reads, calls and field
  checks see the records, ``False`` or numbers it holds there;
* no binding is an error for its type, and ``x = []`` needs no annotation;
* where branches that gave the variable values of different types meet (after an
  ``if``, at the head of a loop), it is ``Any``; values of one type keep it.

Odoo code marks "no record" with ``False``: ``partner = line.partner_id if line else
False``, ``so_line = lines and lines[-1] or False``, ``next((m for m in members if ...),
False)``. mypy widens that ``False`` to ``bool`` when it types a variable, and ``if
partner:`` leaves ``Literal[True]``, whose attributes are errors. A variable that a
binding may give ``False`` (the value itself, an arm of a conditional expression, an
operand of ``and`` / ``or``, the default of ``next()``, ``getattr()`` or ``.get()``) is
declared ``Any | Literal[False]``, also when it is bound once: mypy then types the
``False`` of each binding as ``Literal[False]``, which ``if partner:`` removes, and where
branches meet the variable is the union of their values. ``None`` needs nothing of the
kind: mypy does not narrow it away either, it takes ``x = None`` as a placeholder that a
later value replaces.

A variable bound once (besides the above), a parameter, a ``global`` / ``nonlocal``
name, an ``except ... as e`` name and a name a ``case`` pattern captures keep mypy's
own inference. The variables are declared when the plugin is first asked for a hook in
the module, which for a model is its first field: in a function already checked by then
nothing changes, and in the function being checked only the variables whose first
binding mypy has not reached yet.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator

from mypy.nodes import (
    LDEF,
    AssignmentStmt,
    Block,
    CallExpr,
    ClassDef,
    ConditionalExpr,
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
    IntExpr,
    LambdaExpr,
    ListComprehension,
    ListExpr,
    MatchStmt,
    MemberExpr,
    MypyFile,
    NameExpr,
    OperatorAssignmentStmt,
    OpExpr,
    OverloadedFuncDef,
    RefExpr,
    SetComprehension,
    SetExpr,
    StarExpr,
    Statement,
    TryStmt,
    TupleExpr,
    UnaryExpr,
    Var,
    WhileStmt,
    WithStmt,
)
from mypy.patterns import (
    AsPattern,
    ClassPattern,
    MappingPattern,
    OrPattern,
    Pattern,
    SequencePattern,
    StarredPattern,
)
from mypy.types import AnyType, Instance, LiteralType, Type, TypeOfAny, UnionType

__all__ = ["ReboundVariables", "function_bindings", "functions", "nested_blocks", "rebound_names"]

FALSE = "builtins.False"
# Displays: a variable bound to one with displays inside is declared ``Any``.
DISPLAYS = (ListExpr, DictExpr, SetExpr, TupleExpr)
EMPTY_CALLS = frozenset({"builtins.list", "builtins.dict", "builtins.set"})
# Containers mypy has no partial type for: ``Counter()`` alone "needs an annotation".
UNINFERRED_CALLS = frozenset(
    {"collections.Counter", "collections.OrderedDict", "collections.deque"}
)
# Functions and methods whose last argument is the value they give when nothing is
# found: ``next(iterator, False)``, ``getattr(record, name, False)``, ``d.get(key, False)``.
DEFAULT_ARGUMENTS = {"builtins.next": 2, "builtins.getattr": 3}
DEFAULT_METHODS = {"get": 2}
# The methods of a list that add an item to it.
ADDING_METHODS = frozenset({"append", "insert"})
# ``rows.extend(...)``: mypy completes ``rows = []`` from a list argument only.
EXTEND = "extend"
# Calls whose result is a list: ``rows.extend(list(...))`` completes ``rows = []``.
LIST_CALLS = frozenset({"builtins.list", "builtins.sorted"})


class ReboundVariables:
    """Declare ``Any`` the local variables a function binds more than once, once per tree."""

    def __init__(self) -> None:
        self._prepared: dict[str, MypyFile] = {}  # module -> the tree prepared last

    def prepared(self, tree: MypyFile) -> bool:
        """Whether a tree was prepared (or left alone) already."""
        return self._prepared.get(tree.fullname) is tree

    def skip(self, tree: MypyFile) -> None:
        """Leave a tree as mypy inferred it (a module outside the addons)."""
        self._prepared[tree.fullname] = tree

    def prepare(self, tree: MypyFile, bool_type: Instance) -> None:
        """Declare the rebound variables of every function of a module that mypy has not met.

        Args:
            tree: The module.
            bool_type: ``builtins.bool``, the fallback of ``Literal[False]``.
        """
        self._prepared[tree.fullname] = tree
        for function in _functions(tree.defs):
            for variable, bindings in rebound_names(function).items():
                if variable.type is not None:
                    continue  # mypy has checked the first binding already
                variable.type = _declared(bindings, bool_type)
                for name, _ in bindings:
                    name.is_inferred_def = False


Binding = tuple[NameExpr, Expression | None]


def rebound_names(function: FuncItem) -> dict[Var, list[Binding]]:
    """The local variables of a function to declare, with their bindings.

    Those bound more than once, those a binding may give ``False`` and those bound to
    a display of displays or to a value with an empty display inside
    (:func:`_structured`). Each binding is
    the name bound and the value (``None`` when not known: a ``for`` target...).
    Bindings of nested functions and classes are their own; a variable that is also an
    ``except`` name or a ``case`` capture is left out (mypy binds those itself).
    """
    collector = _Bindings()
    collector.block(function.body)
    collector.numbers()
    return {
        variable: bindings
        for variable, bindings in collector.names.items()
        if variable not in collector.excluded
        and _is_plain(variable)
        and (
            len(bindings) > 1
            or _gives(bindings, FALSE)
            or _structured(bindings)
            or (variable in collector.given_dicts and _is_empty_binding(bindings))
        )
    }


def function_bindings(function: FuncItem) -> _Bindings:
    """Every binding of the local variables of a function (``x += v`` among the targets)."""
    collector = _Bindings()
    collector.block(function.body)
    return collector


def functions(statements: Iterable[Statement]) -> Iterator[FuncItem]:
    """The functions of a module or class body, and the functions defined in them."""
    return _functions(statements)


def nested_blocks(statement: Statement) -> list[Block]:
    """The blocks of a compound statement (not the body of a nested function)."""
    return _nested_blocks(statement)


def _leaves_list_uninferred(method: str, arguments: list[Expression]) -> bool:
    """``rows.append({...})`` / ``.insert(i, {...})``, or ``rows.extend(x)`` of no list."""
    if method in ADDING_METHODS:
        return any(isinstance(argument, DictExpr) for argument in arguments)
    return method == EXTEND and len(arguments) == 1 and _no_list(arguments[0])


def _no_list(node: Expression) -> bool:
    """An argument that may be no list: a call (not ``list()``), a generator, an attribute.

    mypy completes the type of ``rows = []`` from ``rows.extend(x)`` only when ``x`` is
    a list; ``rows.extend(groups.values())`` or ``rows.extend(order.order_line)`` "needs
    a type annotation". A local name is left to mypy (most often a list).
    """
    if isinstance(node, (GeneratorExpr, MemberExpr)):
        return True
    if not isinstance(node, CallExpr):
        return False
    callee = node.callee
    return not (isinstance(callee, NameExpr) and callee.fullname in LIST_CALLS)


def _is_empty_binding(bindings: list[Binding]) -> bool:
    """Whether a variable's first value is an empty display (``rows = []``)."""
    value = bindings[0][1]
    return value is not None and _is_empty(value)


def _declared(bindings: list[Binding], bool_type: Instance) -> Type:
    """``Any``, or ``Any | Literal[False]`` if a binding may give ``False``."""
    anything = AnyType(TypeOfAny.special_form)
    if _gives(bindings, FALSE):
        return UnionType([anything, LiteralType(False, bool_type)])
    return anything


def _structured(bindings: list[Binding]) -> bool:
    """Whether a binding is a display of dicts, or a value with an empty display inside.

    mypy types a display from its items and a variable from its first value, with no
    context: ``[{"id": partner.id, "debit": 0.0}, {"id": tax.id, "debit": 1.5, "x": {}}]``
    is a ``list[object]`` (the dicts' types differ, a list's items are invariant), and
    ``{"name": name, "lines": []}`` or ``dict((team, []) for team in teams)`` "needs a
    type annotation" (the empty list has no item type). Data built this way (test
    expectations, ``vals``, accumulators) was then an error wherever it went. Declared
    ``Any``, the variable's value is typed in that context: ``list[Any]``,
    ``dict[Any, Any]``.
    """
    for _, bound in bindings:
        if bound is None:
            continue
        for value in _values(bound):
            if _is_call_of(value, UNINFERRED_CALLS):
                return True
            inner = list(_descendants(value))
            if any(_is_empty(node) for node in inner):
                return True
            if isinstance(value, DISPLAYS) and any(map(_holds_dicts, _children(value))):
                return True
            if isinstance(value, CallExpr) and any(map(_makes_dicts, value.args)):
                return True  # ``defaultdict(lambda: {"lines": ...})``
    return False


def _is_call_of(node: Expression, fullnames: frozenset[str]) -> bool:
    """A call without arguments of one of some classes (``Counter()``)."""
    return (
        isinstance(node, CallExpr)
        and not node.args
        and isinstance(node.callee, RefExpr)
        and node.callee.fullname in fullnames
    )


def _makes_dicts(node: Expression) -> bool:
    """A ``lambda`` that gives a dict display: the factory of a ``defaultdict`` of dicts."""
    return isinstance(node, LambdaExpr) and isinstance(node.expr(), DictExpr)


def _holds_dicts(node: Expression) -> bool:
    """A dict display, or a list / set display of them (not a tuple: domain terms)."""
    if isinstance(node, DictExpr):
        return True
    return isinstance(node, (ListExpr, SetExpr)) and any(
        isinstance(item, DictExpr) for item in node.items
    )


def _is_int(value: Expression | None) -> bool:
    """``0``, ``-1``."""
    if isinstance(value, UnaryExpr) and value.op == "-":
        value = value.expr
    return isinstance(value, IntExpr)


def _is_empty(node: Expression) -> bool:
    """``[]``, ``{}``, ``set()``, ``list()``, ``dict()``, ``OrderedDict()``..."""
    if isinstance(node, (ListExpr, DictExpr, SetExpr)):
        return not node.items
    return _is_call_of(node, EMPTY_CALLS | UNINFERRED_CALLS)


def _descendants(node: Expression) -> Iterator[Expression]:
    """The expressions inside an expression, not inside a lambda."""
    pending = list(_children(node))
    while pending:
        item = pending.pop()
        yield item
        pending += _children(item)


def _children(node: Expression) -> list[Expression]:
    if isinstance(node, (ListExpr, TupleExpr, SetExpr)):
        return list(node.items)
    if isinstance(node, DictExpr):
        return [item for pair in node.items for item in pair if item is not None]
    if isinstance(node, CallExpr):
        return list(node.args)
    if isinstance(node, (ListComprehension, SetComprehension)):
        return [node.generator]
    if isinstance(node, GeneratorExpr):
        conditions = [test for tests in node.condlists for test in tests]
        return [node.left_expr, *node.sequences, *conditions]
    if isinstance(node, DictionaryComprehension):
        return [item for item in (node.key, node.value, *node.sequences) if item is not None]
    if isinstance(node, ConditionalExpr):
        return [node.cond, node.if_expr, node.else_expr]
    if isinstance(node, OpExpr):
        return [node.left, node.right]
    if isinstance(node, StarExpr):
        return [node.expr]
    return []


def _gives(bindings: list[Binding], constant: str) -> bool:
    """Whether a binding may give a constant (``False``) as the variable's value."""
    return any(
        isinstance(value, NameExpr) and value.fullname == constant
        for _, bound in bindings
        if bound is not None
        for value in _values(bound)
    )


def _values(expression: Expression) -> Iterator[Expression]:
    """The expressions whose value an expression may be.

    Itself, the arms of a conditional expression, the operands of ``and`` / ``or`` and
    the default of ``next()``, ``getattr()``, ``.get()``.
    """
    pending = [expression]
    while pending:
        node = pending.pop()
        yield node
        if isinstance(node, ConditionalExpr):
            pending += [node.if_expr, node.else_expr]
        elif isinstance(node, OpExpr) and node.op in ("and", "or"):
            pending += [node.left, node.right]
        elif isinstance(node, CallExpr):
            callee = node.callee
            count = None
            if isinstance(callee, NameExpr):
                count = DEFAULT_ARGUMENTS.get(callee.fullname)
            elif isinstance(callee, MemberExpr):
                count = DEFAULT_METHODS.get(callee.name)
            if count is not None and len(node.args) == count and node.arg_names[-1] is None:
                pending.append(node.args[-1])


def _is_plain(variable: Var) -> bool:
    """A local variable without an annotation, not a parameter nor a final name."""
    return variable.is_inferred and not variable.is_argument and not variable.is_final


def _functions(statements: Iterable[Statement]) -> Iterator[FuncItem]:
    """The functions of a module or class body, and the functions defined in them."""
    for statement in statements:
        if isinstance(statement, FuncDef):
            yield statement
            yield from _functions(_nested(statement.body))
        elif isinstance(statement, Decorator):
            yield statement.func
            yield from _functions(_nested(statement.func.body))
        elif isinstance(statement, OverloadedFuncDef):
            for item in statement.items:
                yield from _functions([item])
            if statement.impl is not None:
                yield from _functions([statement.impl])
        elif isinstance(statement, ClassDef):
            yield from _functions(statement.defs.body)
        elif isinstance(statement, Block):
            yield from _functions(statement.body)
        else:
            yield from _functions(_nested_blocks(statement))


def _nested(block: Block) -> Iterator[Statement]:
    """The definitions anywhere in a function body (``def`` / ``class`` in an ``if``...)."""
    for statement in block.body:
        if isinstance(statement, (FuncDef, Decorator, OverloadedFuncDef, ClassDef)):
            yield statement
        else:
            for inner in _nested_blocks(statement):
                yield from _nested(inner)


def _nested_blocks(statement: Statement) -> list[Block]:
    """The blocks of a compound statement."""
    if isinstance(statement, IfStmt):
        return [*statement.body, *([statement.else_body] if statement.else_body else [])]
    if isinstance(statement, (ForStmt, WhileStmt)):
        return [statement.body, *([statement.else_body] if statement.else_body else [])]
    if isinstance(statement, WithStmt):
        return [statement.body]
    if isinstance(statement, TryStmt):
        blocks = [statement.body, *statement.handlers]
        return blocks + [block for block in (statement.else_body, statement.finally_body) if block]
    if isinstance(statement, MatchStmt):
        return list(statement.bodies)
    if isinstance(statement, Block):
        return [statement]
    return []


class _Bindings:
    """The names each local variable of one function is bound through, in source order.

    A plain walk of the statements: compiled mypy does not let interpreted classes
    subclass its visitors.
    """

    def __init__(self) -> None:
        self.names: dict[Var, list[Binding]] = {}
        self.excluded: set[Var] = set()
        self.augmented: list[Expression] = []  # targets of ``x += v``, ``x -= v``...
        # Lists the function adds dict displays to (``rows.append({...})``, ``+= [{...}]``)
        # or extends with what is no list (``plan.extend(groups.values())``).
        self.given_dicts: set[Var] = set()

    def numbers(self) -> None:
        """Count ``x += v`` as a binding of a variable whose first value is an int literal."""
        for target in self.augmented:
            if not isinstance(target, NameExpr) or not isinstance(target.node, Var):
                continue
            bindings = self.names.get(target.node)
            if bindings and _is_int(bindings[0][1]):
                bindings.append((target, None))

    def block(self, block: Block | None) -> None:
        if block is None:
            return
        for statement in block.body:
            self.statement(statement)

    def statement(self, statement: Statement) -> None:
        if isinstance(statement, AssignmentStmt):
            for target in statement.lvalues:
                self.target(target, statement.rvalue)
        elif isinstance(statement, OperatorAssignmentStmt):
            # ``x += v`` rebinds ``x`` to another type only for an immutable value:
            # ``total = 0`` then ``total += line.balance`` (a list changes in place).
            self.augmented.append(statement.lvalue)
            if _holds_dicts(statement.rvalue):
                self.given(statement.lvalue)
        elif isinstance(statement, ExpressionStmt):
            call = statement.expr if isinstance(statement.expr, CallExpr) else None
            callee = call.callee if call is not None else None
            if call is not None and isinstance(callee, MemberExpr):
                self.given_by(callee, call.args)
        elif isinstance(statement, ForStmt):
            self.target(statement.index)
            self.block(statement.body)
            self.block(statement.else_body)
        elif isinstance(statement, WhileStmt):
            self.block(statement.body)
            self.block(statement.else_body)
        elif isinstance(statement, WithStmt):
            for bound in statement.target:
                if bound is not None:
                    self.target(bound)
            self.block(statement.body)
        elif isinstance(statement, IfStmt):
            for body in statement.body:
                self.block(body)
            self.block(statement.else_body)
        elif isinstance(statement, TryStmt):
            self.block(statement.body)
            for name in statement.vars:
                if name is not None and isinstance(name.node, Var):
                    self.excluded.add(name.node)
            for handler in statement.handlers:
                self.block(handler)
            self.block(statement.else_body)
            self.block(statement.finally_body)
        elif isinstance(statement, MatchStmt):
            for pattern in statement.patterns:
                self.excluded.update(_captured(pattern))
            for body in statement.bodies:
                self.block(body)
        elif isinstance(statement, Block):
            self.block(statement)

    def given_by(self, callee: MemberExpr, arguments: list[Expression]) -> None:
        """A call of a list method that leaves the list's items to infer (see the class)."""
        if _leaves_list_uninferred(callee.name, arguments):
            self.given(callee.expr)

    def given(self, target: Expression) -> None:
        """A list the function adds a dict display to."""
        if isinstance(target, NameExpr) and target.kind == LDEF and isinstance(target.node, Var):
            self.given_dicts.add(target.node)

    def target(self, target: Expression, value: Expression | None = None) -> None:
        if isinstance(target, NameExpr):
            if target.kind == LDEF and isinstance(target.node, Var):
                self.names.setdefault(target.node, []).append((target, value))
        elif isinstance(target, (TupleExpr, ListExpr)):
            for item in target.items:
                self.target(item)
        elif isinstance(target, StarExpr):
            self.target(target.expr)


def _captured(pattern: Pattern) -> Iterator[Var]:
    """The variables a ``case`` pattern binds."""
    names: list[NameExpr] = []
    pending = [pattern]
    while pending:
        item = pending.pop()
        if isinstance(item, AsPattern):
            names += [item.name] if item.name is not None else []
            pending += [item.pattern] if item.pattern is not None else []
        elif isinstance(item, StarredPattern):
            names += [item.capture] if item.capture is not None else []
        elif isinstance(item, (OrPattern, SequencePattern)):
            pending += item.patterns
        elif isinstance(item, MappingPattern):
            pending += item.values
            names += [item.rest] if item.rest is not None else []
        elif isinstance(item, ClassPattern):
            pending += [*item.positionals, *item.keyword_values]
    return (name.node for name in names if isinstance(name.node, Var))
