"""The methods of a class that return their ``super()`` call and nothing else.

``product.product`` overrides ``_search`` without annotations::

    def _search(self, domain, *args, **kwargs):
        if self.env.context.get("search_default_categ_id"):
            domain = Domain(domain) & Domain(...)
        return super()._search(domain, *args, **kwargs)

Such an override returns what the method it overrides returns, so the plugin gives a
call of it that method's return type (:func:`odoo_typing.methods.inherited_return`). The
scan reads it from the source: mypy keeps no bodies of the methods of a module whose
errors it does not report (Odoo's addons outside the checked ones), nor of a module it
loads from its cache.

A method counts when it has no annotation at all, a first positional parameter, no
``yield``, and every ``return`` of its own (not of a nested function) gives
``super().<its name>(...)`` (also ``super(<its class>, <its first parameter>)``), or a
local that one plain assignment binds to such a call and nothing else binds (``res +=
...`` and ``res.update(...)`` change it in place), and its body ends in a ``return`` or a
``raise`` on every path, so that no ``None`` falls off its end. A name the class body
binds more than once (two ``def``) does not count.
"""

from __future__ import annotations

import ast
from collections import Counter
from collections.abc import Iterator

__all__ = ["super_returns"]

# Nodes with a scope of their own: their returns and bindings are not the method's.
SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)
# The blocks of a compound statement (also ``handlers`` and ``cases``).
BLOCKS = ("body", "orelse", "finalbody")


def super_returns(node: ast.ClassDef) -> tuple[str, ...]:
    """The names of the methods of a class body that return their ``super()`` call."""
    bound = Counter(name for statement in node.body for name in _class_bindings(statement))
    return tuple(
        statement.name
        for statement in node.body
        if isinstance(statement, ast.FunctionDef)
        and bound[statement.name] == 1
        and _passes_super(statement, node.name)
    )


def _class_bindings(statement: ast.stmt) -> Iterator[str]:
    """The names a statement of a class body binds (a method, an attribute)."""
    if isinstance(statement, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
        yield statement.name
    elif isinstance(statement, ast.Assign | ast.AnnAssign | ast.AugAssign):
        targets = statement.targets if isinstance(statement, ast.Assign) else [statement.target]
        for target in targets:
            yield from (item.id for item in ast.walk(target) if isinstance(item, ast.Name))


def _passes_super(function: ast.FunctionDef, class_name: str) -> bool:
    arguments = function.args
    positional = [*arguments.posonlyargs, *arguments.args]
    every = [*positional, *arguments.kwonlyargs, arguments.vararg, arguments.kwarg]
    if function.returns is not None or any(
        item is not None and item.annotation is not None for item in every
    ):
        return False
    if not positional or not _always_exits(function.body):
        return False
    returned = _returned_names(function.body, function.name, class_name, positional[0].arg)
    if returned is None or returned & {item.arg for item in every if item is not None}:
        return False
    return _bound_once(list(_own_nodes(function.body)), returned)


def _returned_names(
    body: list[ast.stmt], method: str, class_name: str, first: str
) -> frozenset[str] | None:
    """The names the ``return`` statements give, if each gives a name or a ``super()`` call.

    ``None`` if one gives something else, if there is none, or if a returned name has no
    plain assignment of such a call. A test on the statements alone: most methods fail
    it, the others get the walk of every expression (:func:`_bound_once`).
    """
    statements = list(_statements(body))
    returned: set[str] = set()
    found = False
    for statement in statements:
        if not isinstance(statement, ast.Return):
            continue
        found = True
        value = statement.value
        if isinstance(value, ast.Name):
            returned.add(value.id)
        elif value is None or not _super_call(value, method, class_name, first):
            return None
    if not found:
        return None
    bound = {
        target.id
        for statement in statements
        if isinstance(statement, ast.Assign)
        and _super_call(statement.value, method, class_name, first)
        for target in statement.targets
        if isinstance(target, ast.Name)
    }
    return frozenset(returned) if returned <= bound else None


def _statements(body: list[ast.stmt]) -> Iterator[ast.stmt]:
    """The statements of a block and of the blocks in it, not of nested functions."""
    pending = list(reversed(body))
    while pending:
        statement = pending.pop()
        yield statement
        if isinstance(statement, SCOPES):
            continue
        blocks: list[list[ast.stmt]] = [getattr(statement, name, []) for name in BLOCKS]
        blocks += [handler.body for handler in getattr(statement, "handlers", ())]
        blocks += [case.body for case in getattr(statement, "cases", ())]
        for block in reversed(blocks):
            pending.extend(reversed(block))


def _bound_once(own: list[ast.AST], names: frozenset[str]) -> bool:
    """Whether the method is no generator and binds each returned name once, by ``=``.

    That one binding is the assignment of the ``super()`` call :func:`_returned_names`
    found. ``x += ...`` keeps the value, changed in place; ``global x`` / ``nonlocal x``
    make it another variable's.
    """
    # The names an ``Assign`` binds are its targets: the statement is the binding.
    targets = {id(target) for item in own for target in _plain_targets(item)}
    counts = dict.fromkeys(names, 0)
    for item in own:
        if isinstance(item, ast.Yield | ast.YieldFrom):
            return False
        if isinstance(item, ast.Global | ast.Nonlocal) and names.intersection(item.names):
            return False
        if id(item) in targets:
            continue
        for name in names:
            counts[name] += _binds(item, name)
    return all(count == 1 for count in counts.values())


def _plain_targets(node: ast.AST) -> list[ast.Name]:
    """The names an ``Assign`` (``x = ...``) or an ``AugAssign`` (``x += ...``) stores."""
    if isinstance(node, ast.Assign):
        return [target for target in node.targets if isinstance(target, ast.Name)]
    if isinstance(node, ast.AugAssign) and isinstance(node.target, ast.Name):
        return [node.target]
    return []


def _binds(node: ast.AST, name: str) -> bool:
    """Whether a node of the method binds a name, other than by ``+=`` (an in-place change).

    An ``Assign`` that names it among its targets is the binding itself; any other node
    that stores the name (an unpacking, ``for``, ``with ... as``, ``:=``, ``except ... as``,
    an import, ``del``) is one too.
    """
    if isinstance(node, ast.Assign):
        return any(isinstance(target, ast.Name) and target.id == name for target in node.targets)
    if isinstance(node, ast.Name):
        return node.id == name and isinstance(node.ctx, ast.Store | ast.Del)
    if isinstance(node, ast.ExceptHandler):
        return node.name == name
    if isinstance(node, ast.alias):
        return (node.asname or node.name.partition(".")[0]) == name
    if isinstance(node, ast.MatchAs | ast.MatchStar):
        return node.name == name
    if isinstance(node, ast.MatchMapping):
        return node.rest == name
    return False


def _super_call(node: ast.expr | None, method: str, class_name: str, first: str) -> bool:
    """``super().<method>(...)`` or ``super(<class_name>, <first>).<method>(...)``."""
    if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
        return False
    attribute = node.func
    made = attribute.value
    if (
        attribute.attr != method
        or not isinstance(made, ast.Call)
        or not isinstance(made.func, ast.Name)
        or made.func.id != "super"
        or made.keywords
    ):
        return False
    if not made.args:
        return True
    names = [item.id if isinstance(item, ast.Name) else None for item in made.args]
    return names == [class_name, first]


def _always_exits(body: list[ast.stmt]) -> bool:
    """Whether a block ends in a ``return`` or ``raise`` on every path, by syntax."""
    if not body:
        return False
    last = body[-1]
    if isinstance(last, ast.Return | ast.Raise):
        return True
    if isinstance(last, ast.If):
        return _always_exits(last.body) and _always_exits(last.orelse)
    if isinstance(last, ast.Try | ast.TryStar):
        if _always_exits(last.finalbody):
            return True
        ending = last.orelse or last.body
        return _always_exits(ending) and all(_always_exits(item.body) for item in last.handlers)
    return False


def _own_nodes(body: list[ast.stmt]) -> Iterator[ast.AST]:
    """The nodes of a method's body, without those of nested scopes.

    A nested function or class binds its own name in the method, nothing else of it is
    the method's. The targets of a comprehension count, though they are its own: one
    binding more only makes the answer more careful.
    """
    pending: list[ast.AST] = list(reversed(body))
    while pending:
        node = pending.pop()
        yield node
        if isinstance(node, SCOPES):
            if not isinstance(node, ast.Lambda):
                yield ast.Name(id=node.name, ctx=ast.Store())
            continue
        pending.extend(reversed(list(ast.iter_child_nodes(node))))
