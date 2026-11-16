"""Guards of optional dependencies: code that runs only when a model is installed.

An addon may use a model of an addon it does not depend on, when it checks first::

    if "sale.order" in self.env:
        self.env["sale.order"].search([])

    if "sale.order" not in self.env:
        return
    orders = self.env["sale.order"]...

    lines = self.env.get("sale.order.line") and ...

Such names are not errors (neither ``odoo-model-depends`` nor an unknown model); the
records are ``Any`` there, since their classes may not be on the addons path. The scan
records, for each file, the lines a guard covers:

* the body of an ``if`` whose test proves the model is there (``"m" in env``,
  ``env.get("m")``, also inside ``and``), and the ``else`` of one that proves it is not;
* the rest of the block after ``if "m" not in env: return`` (``raise``, ``continue``,
  ``break``), also with ``or``;
* the true branch of a conditional expression (``x if "m" in env else y``);
* the operands of ``and`` after a test that proves the model there (``"m" in env and
  env["m"].search(...)``), and those of ``or`` after one that proves it is not.

``env`` is ``self.env``, ``env``, ``env.registry`` / ``self.pool`` (and ``.models`` of
them). ``env.ref("sale.x", raise_if_not_found=False)`` in a test guards the XML ids of
the addon ``sale`` (:func:`module_guard`).

A test can also be named: a flag assigned once in the function (``has_sale = "sale.order"
in self.env``, then ``if has_sale:``), or a method of the same class whose body is one
``return <test>`` (``if self._has_sale():``). A helper of another class or addon is not
followed: an override may change it.

A field is guarded the same way, by ``"f" in records._fields``, ``records._fields.get("f")``,
``"f" in env["model"]`` or ``hasattr(records, "f")`` (:func:`field_guard`): code that writes
a field another addon may add. The field name alone counts, whatever the records are.

Code that runs only with an addon outside the dependencies also uses that addon's models.
Two more signs of it are read where such a model would be reported
(:func:`guarded_fields`, :func:`call_strings`): the line is under the guard of a field
that no model of the dependencies has, or the function passes the name of the addon that
defines the model to a call (``self.ensure_installed("sale")``,
``modules._get("sale").state``) or names it in the test of an ``if`` the line is under
(``if modules.search([("name", "=", "sale"), ("state", "=", "installed")]):``).
"""

from __future__ import annotations

import ast
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Final, TypeAlias

from odoo_typing.node_index import NodeIndex

__all__ = [
    "Guard",
    "call_strings",
    "field_guard",
    "guarded_fields",
    "is_guarded",
    "model_guards",
    "module_guard",
]

TERMINATORS: Final = (ast.Return, ast.Raise, ast.Continue, ast.Break)
REGISTRY_ATTRIBUTES: Final = frozenset({"registry", "pool", "models"})
BLOCK_FIELDS: Final = ("body", "orelse", "finalbody")
FIELD_PREFIX: Final = "field:"
# ``self.skipTest(...)`` ends a test like ``return`` (unittest raises ``SkipTest``).
SKIP_TEST: Final = "skipTest"


def _statement_blocks(index: NodeIndex) -> list[list[ast.stmt]]:
    """The non-empty statement lists of a file's nodes (``body``, ``orelse``, ``finalbody``)."""
    return [
        block
        for kind in BLOCK_TYPES & index.classes()
        for node in index.of(kind)
        for block in (getattr(node, name, None) for name in BLOCK_FIELDS)
        if isinstance(block, list) and block and isinstance(block[0], ast.stmt)
    ]


def _node_classes(base: type[ast.AST] = ast.AST) -> Iterator[type[ast.AST]]:
    for cls in base.__subclasses__():
        yield cls
        yield from _node_classes(cls)


# Node classes that have statement blocks (``If``, ``For``, ``FunctionDef``, ``Try``...).
BLOCK_TYPES: Final = frozenset(
    cls for cls in _node_classes() if set(BLOCK_FIELDS) & set(getattr(cls, "_fields", ()))
)

# The guard names a test proves when it is true, and when it is false.
Proves: TypeAlias = tuple[set[str], set[str]]


@dataclass(frozen=True, slots=True)
class _Scope:
    """Names that stand for a test on some lines: flags of a function, helpers of a class."""

    first: int
    last: int
    names: dict[str, Proves] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class _Known:
    """The flags and helper methods a test may use."""

    flags: Mapping[str, Proves]
    helpers: Mapping[str, Proves]


NO_NAMES: Final = _Known({}, {})


@dataclass(frozen=True, slots=True)
class Guard:
    """Lines of a file where a model is known to be installed.

    Attributes:
        model: The model name.
        first: First line covered.
        last: Last line covered.
    """

    model: str
    first: int
    last: int


def is_guarded(guards: Iterable[Guard], model: str, line: int) -> bool:
    """Whether a line of a file is covered by a guard of a model."""
    return any(guard.model == model and guard.first <= line <= guard.last for guard in guards)


def guarded_fields(guards: Iterable[Guard], line: int) -> frozenset[str]:
    """The fields whose guards cover a line of a file (``if "f" in self._fields:``)."""
    return frozenset(
        guard.model.removeprefix(FIELD_PREFIX)
        for guard in guards
        if guard.first <= line <= guard.last and guard.model.startswith(FIELD_PREFIX)
    )


def call_strings(tree: ast.AST, line: int) -> frozenset[str]:
    """The strings by which the function around a line may name an addon it relies on.

    Those it passes to calls as whole arguments (``cls.ensure_installed("sale")`` gives
    ``sale``), and every string in the test of an ``if`` that the line is under, or that
    ends the function before it (``if not modules.search([("name", "=", "sale"), ...]):
    return``). A string in a list, a tuple or a comparison anywhere else
    (``("type", "=", "sale")``, ``journal.type == "sale"``) names nothing.
    """
    functions = [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.lineno <= line <= (node.end_lineno or node.lineno)
    ]
    if not functions:
        return frozenset()
    innermost = max(functions, key=lambda node: node.lineno)
    found: set[str] = set()
    for node in ast.walk(innermost):
        if isinstance(node, ast.Call):
            arguments = [*node.args, *(keyword.value for keyword in node.keywords)]
            found.update(name for name in map(_string, arguments) if name is not None)
        elif isinstance(node, ast.If) and _decides(node, line):
            found.update(
                part.value
                for part in ast.walk(node.test)
                if isinstance(part, ast.Constant) and isinstance(part.value, str)
            )
    return frozenset(found)


def _decides(test: ast.If, line: int) -> bool:
    """Whether an ``if`` decides that a line runs: the line is in it, or it ends before.

    ``self.skipTest(...)`` ends a test as ``return`` does (it raises ``SkipTest``).
    """
    last = test.body[-1]
    if test.body[0].lineno <= line <= (last.end_lineno or last.lineno):
        return True
    return _ends(last) and (last.end_lineno or last.lineno) < line


def _ends(statement: ast.stmt) -> bool:
    """``return``, ``raise``, ``continue``, ``break``, or a call of ``skipTest``."""
    if isinstance(statement, TERMINATORS):
        return True
    call = statement.value if isinstance(statement, ast.Expr) else None
    return (
        isinstance(call, ast.Call)
        and isinstance(call.func, ast.Attribute)
        and call.func.attr == SKIP_TEST
    )


def model_guards(nodes: Iterable[ast.AST] | NodeIndex) -> tuple[Guard, ...]:
    """The guards in a file, from its inner nodes (see :func:`odoo_typing.node_index.index_nodes`).

    Only the node classes a guard can come from are read: statement blocks, ``x if t
    else y``, and the assignments, functions and classes that name a test.
    """
    index = nodes if isinstance(nodes, NodeIndex) else NodeIndex(nodes)
    if_expressions = index.of(ast.IfExp)
    assignments = index.of(ast.Assign)
    functions: list[ast.FunctionDef | ast.AsyncFunctionDef] = [
        *index.of(ast.FunctionDef),
        *index.of(ast.AsyncFunctionDef),
    ]
    classes = index.of(ast.ClassDef)
    blocks = _statement_blocks(index)
    flags = _flag_scopes(assignments, functions)
    helpers = _helper_scopes(functions, classes)
    guards: list[Guard] = []
    for expression in if_expressions:
        known = _known_at(expression.lineno, flags, helpers)
        when_true, when_false = _test_models(expression.test, known)
        guards += _cover(when_true, [expression.body])
        guards += _cover(when_false, [expression.orelse])
    for operation in index.of(ast.BoolOp):
        guards += _operand_guards(operation, _known_at(operation.lineno, flags, helpers))
    for block in blocks:
        guards += _block_guards(block, _known_at(block[0].lineno, flags, helpers))
    return tuple(guards)


def _flag_scopes(
    assignments: Sequence[ast.Assign], functions: Sequence[ast.FunctionDef | ast.AsyncFunctionDef]
) -> list[_Scope]:
    """The functions that assign a guard test to a variable once (``has_sale = ... in env``)."""
    scopes: dict[int, _Scope] = {}
    for node in assignments:
        if len(node.targets) != 1 or not isinstance(node.targets[0], ast.Name):
            continue
        proves = _test_models(node.value)
        if not proves[0] and not proves[1]:
            continue
        function = _innermost(functions, node.lineno)
        if function is None or _assignments(function, node.targets[0].id) != 1:
            continue
        scope = scopes.setdefault(id(function), _scope_of(function))
        scope.names[node.targets[0].id] = proves
    return list(scopes.values())


def _helper_scopes(
    functions: Sequence[ast.FunctionDef | ast.AsyncFunctionDef], classes: Sequence[ast.ClassDef]
) -> list[_Scope]:
    """The classes with a method whose body is ``return <guard test>``."""
    scopes: dict[int, _Scope] = {}
    for node in functions:
        if node.args.args[1:] or node.args.posonlyargs:
            continue
        body = node.body[1:] if ast.get_docstring(node) is not None else node.body
        if len(body) != 1 or not isinstance(body[0], ast.Return) or body[0].value is None:
            continue
        proves = _test_models(body[0].value)
        owner = _innermost(classes, node.lineno)
        if owner is None or (not proves[0] and not proves[1]):
            continue
        scopes.setdefault(id(owner), _scope_of(owner)).names[node.name] = proves
    return list(scopes.values())


def _innermost(nodes: Sequence[ast.stmt], line: int) -> ast.stmt | None:
    """The innermost of some function or class definitions that contains a line."""
    found: ast.stmt | None = None
    for node in nodes:
        inside = node.lineno <= line <= (node.end_lineno or node.lineno)
        if inside and (found is None or node.lineno >= found.lineno):
            found = node
    return found


def _scope_of(node: ast.stmt) -> _Scope:
    return _Scope(node.lineno, node.end_lineno or node.lineno)


def _assignments(function: ast.AST, name: str) -> int:
    """How many times a function binds a name (a flag must not change)."""
    return sum(
        1
        for node in ast.walk(function)
        if isinstance(node, ast.Name) and node.id == name and isinstance(node.ctx, ast.Store)
    )


def _known_at(line: int, flags: Sequence[_Scope], helpers: Sequence[_Scope]) -> _Known:
    if not flags and not helpers:
        return NO_NAMES
    flag_scope = _innermost_scope(flags, line)
    helper_scope = _innermost_scope(helpers, line)
    return _Known(
        flag_scope.names if flag_scope is not None else {},
        helper_scope.names if helper_scope is not None else {},
    )


def _innermost_scope(scopes: Sequence[_Scope], line: int) -> _Scope | None:
    found: _Scope | None = None
    for scope in scopes:
        if scope.first <= line <= scope.last and (found is None or scope.first >= found.first):
            found = scope
    return found


def _block_guards(block: Sequence[ast.stmt], known: _Known = NO_NAMES) -> list[Guard]:
    guards: list[Guard] = []
    for position, statement in enumerate(block):
        if not isinstance(statement, ast.If):
            continue
        when_true, when_false = _test_models(statement.test, known)
        guards += _cover(when_true, statement.body)
        guards += _cover(when_false, statement.orelse)
        rest = block[position + 1 :]
        if rest and isinstance(statement.body[-1], TERMINATORS):
            guards += _cover(when_false, rest)  # past the if: the test was false
        if rest and statement.orelse and isinstance(statement.orelse[-1], TERMINATORS):
            guards += _cover(when_true, rest)
    return guards


def _operand_guards(operation: ast.BoolOp, known: _Known) -> list[Guard]:
    """The operands of ``and`` after a true test, of ``or`` after a false one.

    ``"f" in self._fields and self.env["m"].search(...)`` evaluates the search only when
    the field is there, as the body of ``if`` does.
    """
    guards: list[Guard] = []
    for position, value in enumerate(operation.values[:-1]):
        when_true, when_false = _test_models(value, known)
        proven = when_true if isinstance(operation.op, ast.And) else when_false
        guards += _cover(proven, operation.values[position + 1 :])
    return guards


def _cover(models: Iterable[str], nodes: Sequence[ast.stmt | ast.expr]) -> list[Guard]:
    if not nodes:
        return []
    first = nodes[0].lineno
    last = nodes[-1].end_lineno or nodes[-1].lineno
    return [Guard(model, first, last) for model in models]


def _test_models(test: ast.expr, known: _Known = NO_NAMES) -> Proves:
    """The models a test proves installed when it is true, and when it is false."""
    if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
        when_true, when_false = _test_models(test.operand, known)
        return when_false, when_true
    if isinstance(test, ast.BoolOp):
        parts = [_test_models(value, known) for value in test.values]
        if isinstance(test.op, ast.And):  # true: every part is true
            return set().union(*(true for true, _ in parts)), set()
        return set(), set().union(*(false for _, false in parts))  # false: every part false
    if isinstance(test, ast.Compare) and len(test.ops) == 1:
        name = _contained_name(test.left, test.comparators[0])
        if name is not None and isinstance(test.ops[0], ast.In):
            return {name}, set()
        if name is not None and isinstance(test.ops[0], ast.NotIn):
            return set(), {name}
    if (
        isinstance(test, ast.Call)
        and isinstance(test.func, ast.Attribute)
        and test.func.attr == "get"
        and test.args
    ):
        name = _contained_name(test.args[0], test.func.value)
        if name is not None:
            return {name}, set()
    if (
        isinstance(test, ast.Call)
        and isinstance(test.func, ast.Name)
        and test.func.id == "hasattr"
        and len(test.args) == 2
    ):
        name = _string(test.args[1])
        if name is not None:
            return {field_guard(name)}, set()
    if isinstance(test, ast.Name) and test.id in known.flags:
        return known.flags[test.id]
    if (
        isinstance(test, ast.Call)
        and not test.args
        and isinstance(test.func, ast.Attribute)
        and isinstance(test.func.value, ast.Name)
        and test.func.value.id in {"self", "cls"}
        and test.func.attr in known.helpers
    ):
        return known.helpers[test.func.attr]
    if isinstance(test, ast.Call) and _is_optional_ref(test):
        name = _string(test.args[0])
        if name is not None and "." in name:  # the id's addon is installed
            return {module_guard(name.partition(".")[0])}, set()
    return set(), set()


def _contained_name(key: ast.expr, container: ast.expr) -> str | None:
    """The guard name of ``key in container``: a model of the registry, a field of ``_fields``."""
    name = _string(key)
    if name is None:
        return None
    if _is_registry(container):
        return name
    if isinstance(container, ast.Attribute) and container.attr == "_fields":
        return field_guard(name)
    if isinstance(container, ast.Subscript) and _is_registry(container.value):
        return field_guard(name)  # ``"f" in env["model"]``: a recordset contains its fields
    return None


def field_guard(field_name: str) -> str:
    """The guard name of a field that may be missing (``if "f" in self._fields:``)."""
    return f"{FIELD_PREFIX}{field_name}"


def module_guard(addon_name: str) -> str:
    """The guard name of an installed addon (``env.ref("sale.x", ...)`` in a test)."""
    return f"addon:{addon_name}"


def _is_optional_ref(call: ast.Call) -> bool:
    """``env.ref("module.name", raise_if_not_found=False)``."""
    return (
        isinstance(call.func, ast.Attribute)
        and call.func.attr == "ref"
        and _is_registry(call.func.value)
        and bool(call.args)
        and any(
            keyword.arg == "raise_if_not_found"
            and isinstance(keyword.value, ast.Constant)
            and keyword.value.value is False
            for keyword in call.keywords
        )
    )


def _is_registry(node: ast.expr) -> bool:
    """``env``, ``self.env``, ``env.registry``, ``self.pool``, ``....models``."""
    while isinstance(node, ast.Attribute) and node.attr in REGISTRY_ATTRIBUTES:
        node = node.value
        if isinstance(node, ast.Name) and node.id in {"self", "cls"}:
            return True  # self.pool
    return (isinstance(node, ast.Attribute) and node.attr == "env") or (
        isinstance(node, ast.Name) and node.id == "env"
    )


def _string(node: ast.expr) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None
