"""Unit tests for the graph helpers (C3 merge)."""

from __future__ import annotations

import pytest

from odoo_typing.graphs import c3_merge


class A:
    pass


class B(A):
    pass


class C(A):
    pass


class D(B, C):
    pass


class E(C, B):
    pass


def _linearize(cls: type) -> list[str]:
    bases = list(cls.__bases__)
    merged, conflict = c3_merge(
        [*([b.__name__, *_linearize(b)] for b in bases), [b.__name__ for b in bases]]
    )
    assert not conflict
    return [name for name in merged if name != "object"]


@pytest.mark.parametrize("cls", [B, D, E])
def test_c3_merge_consistent_hierarchy_matches_python_mro(cls: type) -> None:
    expected = [item.__name__ for item in cls.__mro__[1:] if item is not object]

    assert _linearize(cls) == expected


def test_c3_merge_conflict_keeps_every_element_first_sequence_wins() -> None:
    # class F(D, E) is a TypeError in Python: D wants B before C, E the reverse.
    merged, conflict = c3_merge([["D", "B", "C", "A"], ["E", "C", "B", "A"], ["D", "E"]])

    assert conflict
    assert merged == ["D", "E", "B", "C", "A"]
