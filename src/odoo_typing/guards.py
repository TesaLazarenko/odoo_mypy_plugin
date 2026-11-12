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
* the true branch of a conditional expression (``x if "m" in env else y``).

``env`` is ``self.env``, ``env``, ``env.registry`` / ``self.pool`` (and ``.models`` of
them). ``env.ref("sale.x", raise_if_not_found=False)`` in a test guards the XML ids of
the addon ``sale`` (:func:`module_guard`).

A test can also be named: a flag assigned once in the function (``has_sale = "sale.order"
in self.env``, then ``if has_sale:``), or a method of the same class whose body is one
``return <test>`` (``if self._has_sale():``). A helper of another class or addon is not
followed: an override may change it.

A field is guarded the same way, by ``"f" in records._fields``, ``records._fields.get("f")``
or ``hasattr(records, "f")`` (:func:`field_guard`): code that writes a field another addon
may add. The field name alone counts, whatever the records are.
"""

from __future__ import annotations

import ast
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Final, TypeAlias

__all__ = ["Guard", "field_guard", "is_guarded", "model_guards", "module_guard"]

TERMINATORS: Final = (ast.Return, ast.Raise, ast.Continue, ast.Break)
REGISTRY_ATTRIBUTES: Final = frozenset({"registry", "pool", "models"})
BLOCK_FIELDS: Final = ("body", "orelse", "finalbody")
FUNCTIONS: Final = (ast.FunctionDef, ast.AsyncFunctionDef)

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


def model_guards(nodes: Iterable[ast.AST]) -> tuple[Guard, ...]:
    """The guards in a file, from its inner nodes (see ``scanner.syntax.inner_nodes``)."""
    nodes = list(nodes)
    flags, helpers = _flag_scopes(nodes), _helper_scopes(nodes)
    guards: list[Guard] = []
    for node in nodes:
        if isinstance(node, ast.IfExp):
            known = _known_at(node.lineno, flags, helpers)
            when_true, when_false = _test_models(node.test, known)
            guards += _cover(when_true, [node.body])
            guards += _cover(when_false, [node.orelse])
            continue
        for name in BLOCK_FIELDS:
            block = getattr(node, name, None)
            if isinstance(block, list) and block and isinstance(block[0], ast.stmt):
                guards += _block_guards(block, _known_at(block[0].lineno, flags, helpers))
    return tuple(guards)


def _flag_scopes(nodes: Sequence[ast.AST]) -> list[_Scope]:
    """The functions that assign a guard test to a variable once (``has_sale = ... in env``)."""
    functions = [node for node in nodes if isinstance(node, FUNCTIONS)]
    scopes: dict[int, _Scope] = {}
    for node in nodes:
        if not (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
        ):
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


def _helper_scopes(nodes: Sequence[ast.AST]) -> list[_Scope]:
    """The classes with a method whose body is ``return <guard test>``."""
    classes = [node for node in nodes if isinstance(node, ast.ClassDef)]
    scopes: dict[int, _Scope] = {}
    for node in nodes:
        if not isinstance(node, FUNCTIONS) or node.args.args[1:] or node.args.posonlyargs:
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
    return None


def field_guard(field_name: str) -> str:
    """The guard name of a field that may be missing (``if "f" in self._fields:``)."""
    return f"field:{field_name}"


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
