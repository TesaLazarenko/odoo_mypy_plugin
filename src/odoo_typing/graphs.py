"""Graph helpers: strongly connected components (Tarjan) and C3 linearization."""

from __future__ import annotations

from collections.abc import Callable, Hashable, Iterable, Iterator, Sequence
from typing import Generic, TypeVar

__all__ = ["c3_merge", "components_from"]

NodeT = TypeVar("NodeT", bound=Hashable)


def components_from(
    start: NodeT, successors: Callable[[NodeT], Iterable[NodeT]], done: set[NodeT]
) -> list[list[NodeT]]:
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


def c3_merge(sequences: Iterable[Sequence[NodeT]]) -> tuple[list[NodeT], bool]:
    """Merge sequences the way Python's C3 linearization does, without ever failing.

    C3 repeatedly takes the first head (scanning the sequences in order) that is in
    no sequence's tail. Where Python would raise ``TypeError`` (no such head), this
    takes the head of the first sequence anyway and carries on, so every element is
    kept and the order of the earlier sequences wins.

    Args:
        sequences: Each a list of distinct elements; earlier ones take precedence.

    Returns:
        The merged order, and whether a conflict had to be broken.
    """
    lists = [list(reversed(sequence)) for sequence in sequences if sequence]  # head last
    in_tails: dict[NodeT, int] = {}
    for items in lists:
        for item in items[:-1]:
            in_tails[item] = in_tails.get(item, 0) + 1
    merged: list[NodeT] = []
    conflict = False
    while lists:
        head = next((items[-1] for items in lists if not in_tails.get(items[-1])), None)
        if head is None:
            conflict, head = True, lists[0][-1]
        merged.append(head)
        lists = _drop_head(lists, head, in_tails)
    return merged, conflict


def _drop_head(
    lists: list[list[NodeT]], head: NodeT, in_tails: dict[NodeT, int]
) -> list[list[NodeT]]:
    """Remove the chosen element from every sequence (reversed, so the head is last)."""
    in_some_tail = in_tails.get(head, 0) > 0  # only after a conflict was broken
    remaining = []
    for items in lists:
        if items[-1] == head:
            items.pop()
            if items:
                in_tails[items[-1]] -= 1  # the new head is no longer in a tail
        elif in_some_tail and head in items:
            items.remove(head)
            in_tails[head] -= 1
        if items:
            remaining.append(items)
    return remaining


class _Tarjan(Generic[NodeT]):
    """Iterative Tarjan search: model graphs can be deeper than the recursion limit."""

    def __init__(self, successors: Callable[[NodeT], Iterable[NodeT]], done: set[NodeT]) -> None:
        self.successors = successors
        self.done = done
        self.index: dict[NodeT, int] = {}
        self.low: dict[NodeT, int] = {}
        self.stack: list[NodeT] = []
        self.on_stack: set[NodeT] = set()
        self.components: list[list[NodeT]] = []

    def run(self, start: NodeT) -> None:
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

    def _enter(self, node: NodeT) -> tuple[NodeT, Iterator[NodeT]]:
        self.index[node] = self.low[node] = len(self.index)
        self.stack.append(node)
        self.on_stack.add(node)
        return node, iter(self.successors(node))

    def _close(self, node: NodeT) -> None:
        if self.low[node] != self.index[node]:
            return
        position = self.stack.index(node)
        component = self.stack[position:]
        del self.stack[position:]
        self.on_stack.difference_update(component)
        self.components.append(component)
