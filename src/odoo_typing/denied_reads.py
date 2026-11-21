"""Reads of a field in the branch where the test says the records do not have it.

``if "f" in records:`` gives ``records`` the field for the plugin
(:mod:`odoo_typing.hooks.asked_fields`), through the type mypy records for the operand
of ``in``. mypy narrows that operand to the recorded type in both branches, and calls no
plugin hook where a branch begins: the field is there in the ``else`` branch too, in the
body of ``if "f" not in records:`` and after ``if "f" in records: return`` (or
``self.skipTest(...)``, declared not to return). A read of the field there raises
``AttributeError`` each time it runs, and mypy would not report it.

So the reads are found in the source: the ``if`` statement whose test is the question
(or ``not`` of it), the statements that run when the answer is no, and in them the
attribute ``f`` of the records as they were written in the question.

Only what the source says for sure is reported. Nothing is, when those statements bind
the name of the records again (``rec = other``, ``for rec in ...``), ask again
(``hasattr(rec, "f")``, another ``in``) or set attributes by name (``setattr``): the
name may hold other records, or the field may be there after all. A nested function,
lambda or comprehension is not read (its names are its own), nor is what follows a
``return`` or ``raise``, or a call that may not return (``-> NoReturn``; the caller
says which: :func:`denied_reads`), since mypy does not check unreachable code either.
"""

from __future__ import annotations

import ast
from collections.abc import Callable, Iterator, Mapping
from typing import Final, TypeAlias

from odoo_typing.guards import BLOCK_FIELDS, SKIP_TEST, TERMINATORS, binds, dotted, ends

__all__ = ["IfStatements", "MayEnd", "denied_reads", "if_statements", "line_reads"]

# The ``if`` statements of a file by the line of their test, with the statements after each.
IfStatements: TypeAlias = Mapping[int, tuple[tuple[ast.If, tuple[ast.stmt, ...]], ...]]
# Whether a call on a line may not return (declared ``NoReturn``, or not known); with a
# method name, whether the first call of that method there is declared not to return.
MayEnd: TypeAlias = Callable[[int, str | None], bool]

SCOPES: Final = (
    ast.FunctionDef,
    ast.AsyncFunctionDef,
    ast.Lambda,
    ast.ClassDef,
    ast.ListComp,
    ast.SetComp,
    ast.DictComp,
    ast.GeneratorExp,
)
ASKING: Final = frozenset({"hasattr", "setattr", "getattr", "isinstance"})
FIELDS: Final = "_fields"


def if_statements(tree: ast.AST) -> IfStatements:
    """The ``if`` statements of a file by the line of their test, each with what follows it.

    Built once for a parsed file: every question in it looks its statement up here.
    """
    found: dict[int, list[tuple[ast.If, tuple[ast.stmt, ...]]]] = {}
    for block in _blocks(tree):
        for position, statement in enumerate(block):
            if isinstance(statement, ast.If):
                rest = tuple(block[position + 1 :])
                found.setdefault(statement.test.lineno, []).append((statement, rest))
    return {line: tuple(items) for line, items in found.items()}


def denied_reads(
    statements: IfStatements,
    line: int,
    name: str,
    records: str,
    may_end: MayEnd,
) -> list[tuple[int, int]]:
    """The reads of ``records.name`` where the question on a line is answered no.

    Args:
        statements: The ``if`` statements of the file (:func:`if_statements`).
        line: The line of ``"name" in records`` (or ``in records._fields``).
        name: The field asked for.
        records: The records asked, as written (``self``, ``order.partner_id``).
        may_end: Whether the call that is a statement on a line may not return: the
            statements after it in its block are not read. Asked of ``skipTest`` for a
            branch that ends with a call of it (see :data:`MayEnd`).

    Returns:
        The line and column of each read, in source order; none when the question is
        not the whole test of an ``if`` statement (``and``, a conditional expression),
        when the line holds two such statements, or when the statements leave a doubt
        (see the module).
    """
    found = [
        (statement, rest)
        for statement, rest in statements.get(line, ())
        if _question(statement.test, name, records) is not None
    ]
    if len(found) != 1:
        return []
    statement, rest = found[0]
    denied = _denied(statement, name, records, list(rest), may_end)
    nodes = list(_nodes(denied, may_end))
    root = records.partition(".")[0]
    if any(_doubts(node, root, name, records) for node in nodes) or _rebinds_inside(denied, root):
        return []
    return sorted(
        (node.lineno, node.col_offset)
        for node in nodes
        if isinstance(node, ast.Attribute)
        and node.attr == name
        and isinstance(node.ctx, ast.Load)
        and dotted(node.value) == records
    )


def line_reads(tree: ast.AST, line: int, name: str, records: str) -> list[int]:
    """The columns of every ``records.name`` on a line, read or written, in order.

    mypy has a node for each of them: the n-th of the line in the source is its n-th.
    """
    return sorted(
        node.col_offset
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and node.lineno == line
        and node.attr == name
        and dotted(node.value) == records
    )


def _blocks(tree: ast.AST) -> Iterator[list[ast.stmt]]:
    """The statement lists of a file's nodes (``body``, ``orelse``, ``finalbody``)."""
    for parent in ast.walk(tree):
        for field in BLOCK_FIELDS:
            block = getattr(parent, field, None)
            if isinstance(block, list):
                yield block


def _question(test: ast.expr, name: str, records: str) -> ast.Compare | None:
    """The question a test is (``"name" in records``), or is ``not`` of."""
    if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
        test = test.operand
    if not isinstance(test, ast.Compare) or len(test.ops) != 1:
        return None
    key, container = test.left, test.comparators[0]
    if not isinstance(test.ops[0], ast.In | ast.NotIn):
        return None
    if not isinstance(key, ast.Constant) or key.value != name:
        return None
    if isinstance(container, ast.Attribute) and container.attr == FIELDS:
        container = container.value
    return test if dotted(container) == records else None


def _denied(
    statement: ast.If,
    name: str,
    records: str,
    rest: list[ast.stmt],
    may_end: MayEnd,
) -> list[ast.stmt]:
    """The statements that run when the answer is no: a branch, and after one that ends.

    A branch ends with ``return``, ``raise``, ``continue``, ``break``, or a call of
    ``skipTest`` (:func:`odoo_typing.guards.ends`) that ``may_end`` says is declared not
    to return, on a line no other statement of the branch shares: ``unittest``'s raises
    ``SkipTest``; a method of that name declared to return, or not known, ends nothing.
    """
    question = _question(statement.test, name, records)
    assert question is not None
    positive = isinstance(question.ops[0], ast.In) == (statement.test is question)
    granted, denied = (
        (statement.body, statement.orelse) if positive else (statement.orelse, statement.body)
    )
    if granted and _branch_ends(granted, may_end):
        return [*denied, *rest]
    return list(denied)


def _nodes(statements: list[ast.stmt], may_end: MayEnd) -> Iterator[ast.AST]:
    """The nodes of some statements, in their scope, up to a statement that ends them."""
    for statement in statements:
        yield from _inner(statement, may_end)
        if isinstance(statement, TERMINATORS) or _may_end(statement, may_end):
            return


def _branch_ends(branch: list[ast.stmt], may_end: MayEnd) -> bool:
    """Whether the last statement of a branch ends it (see :func:`_denied`).

    ``may_end`` answers for a line: a ``skipTest`` that shares its line with another
    statement of the branch (``a; self.skipTest()``) ends nothing.
    """
    last = branch[-1]
    if isinstance(last, TERMINATORS):
        return True
    if not ends(last) or not isinstance(last, ast.Expr):
        return False
    before = branch[-2] if len(branch) > 1 else None
    if before is not None and (before.end_lineno or before.lineno) >= last.value.lineno:
        return False
    return _may_end(last, may_end, SKIP_TEST)


def _may_end(statement: ast.stmt, may_end: MayEnd, method: str | None = None) -> bool:
    """Whether a statement is a call that may not return (``self.fail(...)``).

    With ``method``, a call of that method declared not to return (:data:`MayEnd`).
    """
    return (
        isinstance(statement, ast.Expr)
        and isinstance(statement.value, ast.Call)
        and may_end(statement.value.lineno, method)
    )


def _inner(node: ast.AST, may_end: MayEnd) -> Iterator[ast.AST]:
    yield node
    if isinstance(node, SCOPES):
        return
    blocks = [
        block for field in BLOCK_FIELDS if isinstance(block := getattr(node, field, None), list)
    ]
    listed = {id(statement) for block in blocks for statement in block}
    for child in ast.iter_child_nodes(node):
        if id(child) not in listed:
            yield from _inner(child, may_end)
    for block in blocks:
        yield from _nodes(block, may_end)


def _doubts(node: ast.AST, root: str, name: str, records: str) -> bool:
    """Whether a node may change what the name holds or whether the field is there."""
    if isinstance(node, ast.Name):
        return node.id == root and not isinstance(node.ctx, ast.Load)
    if isinstance(node, ast.Attribute) and not isinstance(node.ctx, ast.Load):
        written = dotted(node)  # ``self.partner = ...`` where the records are ``self.partner``
        return written is not None and (records == written or records.startswith(f"{written}."))
    if isinstance(node, ast.Call):
        return isinstance(node.func, ast.Name) and node.func.id in ASKING
    if isinstance(node, ast.Compare):  # the same records asked again for the field
        asked = dotted(node.comparators[0])
        again = isinstance(node.left, ast.Constant) and node.left.value == name
        return again and asked is not None and asked.partition(".")[0] == root
    if isinstance(node, ast.arg):
        return node.arg == root
    return binds(node, root)


def _rebinds_inside(statements: list[ast.stmt], root: str) -> bool:
    """Whether a nested scope binds the name in this one.

    A walrus in a comprehension binds it in the enclosing scope; so does ``nonlocal``
    or ``global`` in a nested function.
    """
    for statement in statements:
        for node in ast.walk(statement):
            if isinstance(node, ast.NamedExpr) and dotted(node.target) == root:
                return True
            if isinstance(node, ast.Nonlocal | ast.Global) and root in node.names:
                return True
    return False
