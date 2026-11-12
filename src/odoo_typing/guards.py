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
them).
"""

from __future__ import annotations

import ast
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Final

__all__ = ["Guard", "is_guarded", "model_guards"]

TERMINATORS: Final = (ast.Return, ast.Raise, ast.Continue, ast.Break)
REGISTRY_ATTRIBUTES: Final = frozenset({"registry", "pool", "models"})
BLOCK_FIELDS: Final = ("body", "orelse", "finalbody")


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
    """The guards in a file, from its inner nodes (see ``scanner._inner_nodes``)."""
    guards: list[Guard] = []
    for node in nodes:
        if isinstance(node, ast.IfExp):
            when_true, when_false = _test_models(node.test)
            guards += _cover(when_true, [node.body])
            guards += _cover(when_false, [node.orelse])
            continue
        for field in BLOCK_FIELDS:
            block = getattr(node, field, None)
            if isinstance(block, list) and block and isinstance(block[0], ast.stmt):
                guards += _block_guards(block)
    return tuple(guards)


def _block_guards(block: Sequence[ast.stmt]) -> list[Guard]:
    guards: list[Guard] = []
    for position, statement in enumerate(block):
        if not isinstance(statement, ast.If):
            continue
        when_true, when_false = _test_models(statement.test)
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


def _test_models(test: ast.expr) -> tuple[set[str], set[str]]:
    """The models a test proves installed when it is true, and when it is false."""
    if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
        when_true, when_false = _test_models(test.operand)
        return when_false, when_true
    if isinstance(test, ast.BoolOp):
        parts = [_test_models(value) for value in test.values]
        if isinstance(test.op, ast.And):  # true: every part is true
            return set().union(*(true for true, _ in parts)), set()
        return set(), set().union(*(false for _, false in parts))  # false: every part false
    if isinstance(test, ast.Compare) and len(test.ops) == 1 and _is_registry(test.comparators[0]):
        name = _string(test.left)
        if name is not None and isinstance(test.ops[0], ast.In):
            return {name}, set()
        if name is not None and isinstance(test.ops[0], ast.NotIn):
            return set(), {name}
    if (
        isinstance(test, ast.Call)
        and isinstance(test.func, ast.Attribute)
        and test.func.attr == "get"
        and _is_registry(test.func.value)
        and test.args
    ):
        name = _string(test.args[0])
        if name is not None:
            return {name}, set()
    return set(), set()


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
