"""Unit tests for the graph helpers (C3 merge)."""

from __future__ import annotations

import pytest

from odoo_typing.graphs import c3_merge


class Root:
    pass


class Left(Root):
    pass


class Right(Root):
    pass


class Diamond(Left, Right):
    pass


class Crossed(Right, Left):
    pass


def _linearize(cls: type) -> list[str]:
    bases = list(cls.__bases__)
    merged, conflict = c3_merge(
        [*([base.__name__, *_linearize(base)] for base in bases), [base.__name__ for base in bases]]
    )
    assert not conflict
    return [name for name in merged if name != "object"]


@pytest.mark.parametrize("cls", [Left, Diamond, Crossed])
def test_c3_merge_consistent_hierarchy_matches_python_mro(cls: type) -> None:
    expected = [item.__name__ for item in cls.__mro__[1:] if item is not object]

    assert _linearize(cls) == expected


def test_c3_merge_conflict_keeps_every_element_first_sequence_wins() -> None:
    # class F(D, E) is a TypeError in Python: D wants B before C, E the reverse.
    merged, conflict = c3_merge([["D", "B", "C", "A"], ["E", "C", "B", "A"], ["D", "E"]])

    assert conflict
    assert merged == ["D", "E", "B", "C", "A"]
