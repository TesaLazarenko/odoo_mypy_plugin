"""One empty ``frozenset`` for the plugin's data.

CPython makes a new object for each ``frozenset()`` (216 bytes); the registry and the
scans hold tens of thousands of empty name sets (fields without dynamic arguments, files
without model references), 5.6 MB on 120 core addons.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Final, TypeVar

__all__ = ["EMPTY", "frozen"]

ItemT = TypeVar("ItemT")

EMPTY: Final[frozenset[object]] = frozenset()


def frozen(items: Iterable[ItemT]) -> frozenset[ItemT]:
    """A ``frozenset`` of the items; the shared :data:`EMPTY` when there are none."""
    found = frozenset(items)
    return found if found else EMPTY  # type: ignore[return-value]  # empty: any ItemT
