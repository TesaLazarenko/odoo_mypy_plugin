"""Strongly connected components of a lazily explored directed graph (Tarjan)."""

from __future__ import annotations

from collections.abc import Callable, Hashable, Iterable, Iterator
from typing import Generic, TypeVar

__all__ = ["components_from"]

T = TypeVar("T", bound=Hashable)


def components_from(
    start: T, successors: Callable[[T], Iterable[T]], done: set[T]
) -> list[list[T]]:
    """Return the strongly connected components reachable from a node.

    Nodes in ``done`` were explored by an earlier call and are skipped: everything
    reachable from them was explored then too, so they cannot share a cycle with a
    new node. Every node visited here is added to ``done``.

    Args:
        start: Node to start from.
        successors: Edges of a node; must give the same answer for the same node.
        done: Nodes already explored (updated in place).

    Returns:
        The components, each in discovery order; a node outside any cycle is a
        component of its own.
    """
    search = _Tarjan(successors, done)
    if start not in done:
        search.run(start)
    done.update(search.index)
    return search.components


class _Tarjan(Generic[T]):
    """Iterative Tarjan search: model graphs can be deeper than the recursion limit."""

    def __init__(self, successors: Callable[[T], Iterable[T]], done: set[T]) -> None:
        self.successors = successors
        self.done = done
        self.index: dict[T, int] = {}
        self.low: dict[T, int] = {}
        self.stack: list[T] = []
        self.on_stack: set[T] = set()
        self.components: list[list[T]] = []

    def run(self, start: T) -> None:
        work = [self._enter(start)]
        while work:
            node, children = work[-1]
            for child in children:
                if child in self.done:
                    continue
                if child not in self.index:
                    work.append(self._enter(child))
                    break
                if child in self.on_stack:
                    self.low[node] = min(self.low[node], self.index[child])
            else:
                work.pop()
                if work:
                    parent = work[-1][0]
                    self.low[parent] = min(self.low[parent], self.low[node])
                self._close(node)

    def _enter(self, node: T) -> tuple[T, Iterator[T]]:
        self.index[node] = self.low[node] = len(self.index)
        self.stack.append(node)
        self.on_stack.add(node)
        return node, iter(self.successors(node))

    def _close(self, node: T) -> None:
        if self.low[node] != self.index[node]:
            return
        position = self.stack.index(node)
        component = self.stack[position:]
        del self.stack[position:]
        self.on_stack.difference_update(component)
        self.components.append(component)
