"""Unit tests of the guards of optional dependencies (roadmap items 4.6 and 6.1)."""

from __future__ import annotations

import ast
import textwrap

from odoo_typing.guards import Guard, field_guard, is_guarded, model_guards, module_guard

SOURCE = """
def action(self):
    if "a.in" in self.env:
        a = 1
    else:
        a = 2
    if "b.not" not in self.env:
        return
    b = 3
    c = 4 if self.env.get("c.get") else 5
    if not ("d.or" in self.pool) or other:
        return
    d = 6
    if "e.and" in env and "e.not" not in env.registry.models:
        e = 7
    if "f.else" not in self.env:
        f = 8
    else:
        f = 9
    if "g.other" in some_dict:
        g = 10
"""


def _guards() -> tuple[Guard, ...]:
    return model_guards(ast.walk(ast.parse(textwrap.dedent(SOURCE))))


def test_model_guards_cover_branches_that_run_with_the_model() -> None:
    assert set(_guards()) == {
        Guard("a.in", 4, 4),
        Guard("b.not", 9, 21),
        Guard("c.get", 10, 10),
        Guard("d.or", 13, 21),
        Guard("e.and", 15, 15),
        Guard("f.else", 19, 19),
    }


def test_is_guarded_checks_model_and_line() -> None:
    guards = _guards()

    assert is_guarded(guards, "a.in", 4)
    assert not is_guarded(guards, "a.in", 6)
    assert not is_guarded(guards, "g.other", 21)


def test_optional_env_ref_guards_the_ids_of_its_addon() -> None:
    source = 'if self.env.ref("sale.group_x", raise_if_not_found=False):\n    y = 1\n'

    guards = model_guards(ast.walk(ast.parse(source)))

    assert guards == (Guard(module_guard("sale"), 2, 2),)


FIELD_SOURCE = """
def action(self, record):
    if "x_in" in self._fields:
        a = 1
    if "x_not" not in record._fields:
        return
    b = 2
    c = 3 if self._fields.get("x_get") else 4
    if hasattr(record, "x_attr") and "sale.order" in self.env:
        d = 5
    if "x_other" in fields:
        e = 6
"""


def test_field_guards_cover_branches_that_run_with_the_field() -> None:
    guards = model_guards(ast.walk(ast.parse(textwrap.dedent(FIELD_SOURCE))))

    assert set(guards) == {
        Guard(field_guard("x_in"), 4, 4),
        Guard(field_guard("x_not"), 7, 12),
        Guard(field_guard("x_get"), 8, 8),
        Guard(field_guard("x_attr"), 10, 10),
        Guard("sale.order", 10, 10),
    }
