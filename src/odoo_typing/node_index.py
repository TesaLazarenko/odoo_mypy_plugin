"""The inner nodes of a syntax tree sorted by class, collected in one pass.

The scanner looks at every file mypy loads several times over: the models a file refers
to (:mod:`odoo_typing.scanner.referenced`) and its guards (:mod:`odoo_typing.guards`).
Testing each of the ~200 nodes of a typical function against every class a check needs
costs more than the checks themselves, so the nodes are sorted by class once and each
check reads only the classes it uses.
"""

from __future__ import annotations

import ast
from collections.abc import Iterable, KeysView
from typing import Final, TypeVar

__all__ = ["NodeIndex", "index_nodes"]

_Node = TypeVar("_Node", bound=ast.AST)

# Concrete node classes that have no child nodes worth visiting (``ast.Load`` etc.).
LEAF_NODES: Final[frozenset[type]] = frozenset(
    {ast.Name, ast.Constant, ast.alias}
    | {
        cls
        for base in (ast.expr_context, ast.operator, ast.boolop, ast.unaryop, ast.cmpop)
        for cls in base.__subclasses__()
    }
)


class NodeIndex:
    """Nodes of a tree by their exact class."""

    __slots__ = ("_by_class",)

    def __init__(self, nodes: Iterable[ast.AST] = ()) -> None:
        by_class: dict[type[ast.AST], list[ast.AST]] = {}
        for node in nodes:
            bucket = by_class.get(type(node))
            if bucket is None:
                by_class[type(node)] = [node]
            else:
                bucket.append(node)
        self._by_class = by_class

    def of(self, cls: type[_Node]) -> list[_Node]:
        """The nodes of exactly this class (ast node classes have no subclasses)."""
        return [node for node in self._by_class.get(cls, ()) if isinstance(node, cls)]

    def classes(self) -> KeysView[type[ast.AST]]:
        """The classes that have nodes."""
        return self._by_class.keys()


def index_nodes(tree: ast.AST) -> NodeIndex:
    """Sort every node of a tree except the leaves (names, constants, operators) by class.

    The traversal is inlined on purpose (hot loop): ``ast.walk`` also visits the leaves,
    about half of all nodes, and its nested generators cost more than the work itself.
    """
    index = NodeIndex()
    by_class = index._by_class
    stack: list[ast.AST] = [tree]
    node_type, leaves = ast.AST, LEAF_NODES
    while stack:
        node = stack.pop()
        bucket = by_class.get(type(node))
        if bucket is None:
            by_class[type(node)] = [node]
        else:
            bucket.append(node)
        for name in node._fields:
            value = getattr(node, name, None)
            if isinstance(value, list):
                stack += [
                    item
                    for item in value
                    if isinstance(item, node_type) and type(item) not in leaves
                ]
            elif isinstance(value, node_type) and type(value) not in leaves:
                stack.append(value)
    return index
