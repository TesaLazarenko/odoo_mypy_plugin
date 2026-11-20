"""Unit tests of the guards of optional dependencies (roadmap items 4.6 and 6.1)."""

from __future__ import annotations

import ast
import textwrap

from odoo_typing.guards import (
    Guard,
    asked_guard,
    call_strings,
    field_guard,
    guarded_fields,
    is_guarded,
    model_guards,
    module_guard,
)

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
        Guard("d.or", 11, 11),  # ``or other``: evaluated when the model is there
        Guard("d.or", 13, 21),
        Guard("e.and", 14, 14),
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
        # who was asked, of ``records._fields``: for the records taken from them there;
        # not for a part of an expression (``x_get``: a guard is its lines)
        Guard(asked_guard("self", "x_in"), 4, 4),
        Guard(asked_guard("record", "x_not"), 7, 12),
        Guard(field_guard("x_attr"), 9, 9),
        Guard(field_guard("x_attr"), 10, 10),
        Guard("sale.order", 10, 10),
    }


def test_operands_of_and_after_a_guard_are_covered() -> None:
    source = """
    if (
        (code := values.get("code"))
        and "intrastat_code_id" in self._fields
        and (record := self.env["account.intrastat.code"].search([]))
    ):
        domain = 1
    found = "x_a" not in self._fields or self.x_a
    """
    guards = model_guards(ast.walk(ast.parse(textwrap.dedent(source))))

    assert set(guards) == {
        Guard(field_guard("intrastat_code_id"), 5, 5),
        Guard(field_guard("intrastat_code_id"), 7, 7),
        Guard(field_guard("x_a"), 8, 8),
    }


NAMED_SOURCE = """
class Thing:
    def _has_sale(self):
        \"\"\"Whether sale is installed.\"\"\"
        return "sale.order" in self.env

    def action(self):
        has_crm = "crm.lead" in self.env and "x_flag" in self._fields
        changed = "ee.only" in self.env
        changed = False
        if has_crm:
            a = 1
        if not self._has_sale():
            return
        b = 2
        if changed:
            c = 3

    def other(self):
        if has_crm:
            d = 4

def helper_outside():
    if self._has_sale():
        e = 5
"""


def test_flags_and_helper_methods_stand_for_their_tests() -> None:
    guards = model_guards(ast.walk(ast.parse(textwrap.dedent(NAMED_SOURCE))))

    assert set(guards) == {
        Guard("crm.lead", 8, 8),
        Guard("crm.lead", 12, 12),
        Guard(field_guard("x_flag"), 12, 12),
        Guard(asked_guard("self", "x_flag"), 12, 12),
        Guard("sale.order", 15, 17),
    }


def test_a_recordset_of_env_contains_its_fields() -> None:
    source = """
    if "x_stock" in env["product.product"]:
        a = 1
    if "x_other" in records:
        b = 2
    """
    guards = model_guards(ast.walk(ast.parse(textwrap.dedent(source))))

    assert set(guards) == {Guard(field_guard("x_stock"), 3, 3)}


def test_guarded_fields_are_the_fields_guarded_on_a_line() -> None:
    guards = (
        Guard(field_guard("x_a"), 3, 9),
        Guard(field_guard("x_b"), 5, 6),
        Guard("sale.order", 1, 20),
    )

    assert guarded_fields(guards, 4) == {"x_a"}
    assert guarded_fields(guards, 5) == {"x_a", "x_b"}
    assert guarded_fields(guards, 10) == frozenset()


CALLS_SOURCE = """
class Common:
    def first(self):
        self.ensure_installed("sale")
        state = self.env["ir.module.module"]._get(name="payment").state
        if journal.type == "purchase" or kind in ("stock", "mrp"):
            domain = [("type", "=", "account")]

        def inner():
            check("inner_only")
        return self.env["sale.order"]

    def second(self):
        return other("crm")

top("module_level")
"""


def test_call_strings_are_the_whole_arguments_of_the_function_around_a_line() -> None:
    tree = ast.parse(textwrap.dedent(CALLS_SOURCE))

    assert call_strings(tree, 11) == {"sale", "payment", "inner_only"}
    assert call_strings(tree, 10) == {"inner_only"}
    assert call_strings(tree, 14) == {"crm"}
    assert call_strings(tree, 16) == frozenset()


def test_call_strings_include_the_tests_that_decide_a_line() -> None:
    source = """
    def check(self):
        if self.modules.search([("name", "=", "website"), ("state", "=", "installed")]):
            a = self.env["website"]
        if kind == "sale":
            b = 1
        c = self.env["sale.order"]
        if not self.modules.search([("name", "=", "stock")]):
            return
        d = self.env["stock.move"]
    """
    tree = ast.parse(textwrap.dedent(source))

    assert call_strings(tree, 4) == {"website", "name", "=", "state", "installed"}
    assert call_strings(tree, 7) == frozenset()
    assert call_strings(tree, 10) == {"stock", "name", "="}


ASKED_SOURCE = """
def kept(self, record, others):
    if "x_kept" in record._fields:
        a = record.sudo()
    field = self._fields.get("x_flag")
    if field:
        b = self.sudo()

def rebound(self, record, others):
    if "x_assigned" in record._fields:
        record = record.company_id
    tracked = "x_loop" in record._fields
    for record in others:
        if tracked:
            c = 1
    if "x_lambda" in self._fields:
        others.mapped(lambda self: self.sudo())
    if "x_nested" in self._fields:
        def inner(self):
            return self.sudo()
    local = others[:1]
    if "x_local" in local._fields:
        d = 1
    if self._fields.get("x_default", others):
        e = 1

def bound_without_a_name(self, record, error, json, others):
    if "x_inline" in self._fields and self.sudo(): g = 1
    if "x_case" in record._fields:
        match others:
            case record:
                h = 1
    if "x_except" in error._fields:
        try:
            i = 1
        except ValueError as error:
            j = 1
    if "x_import" in json._fields:
        import json

if "x_module" in records._fields:
    f = 1
"""


def test_who_was_asked_is_kept_only_for_a_parameter_the_function_never_rebinds() -> None:
    guards = model_guards(ast.walk(ast.parse(textwrap.dedent(ASKED_SOURCE))))

    asked = {guard for guard in guards if guard.model.startswith("asked:")}
    assert asked == {
        Guard(asked_guard("record", "x_kept"), 4, 4),
        Guard(asked_guard("self", "x_flag"), 7, 7),
    }
    # the guards of the fields themselves are all there
    fields = {guard.model for guard in guards if guard.model.startswith("field:")}
    assert fields == {
        field_guard(name)
        for name in (
            "x_kept",
            "x_flag",
            "x_assigned",
            "x_loop",
            "x_lambda",
            "x_nested",
            "x_local",
            "x_default",
            "x_inline",
            "x_case",
            "x_except",
            "x_import",
            "x_module",
        )
    }
