"""End-to-end tests of field paths in ``mapped`` / ``filtered`` / ``sorted`` / ``grouped``."""

from __future__ import annotations

import pytest
from conftest import RunMypy

T_BASE = """
from odoo import fields, models

class Order(models.Model):
    _name = "t.order"

    name = fields.Char()
    amount = fields.Float()
    partner_id = fields.Many2one("res.partner")
    line_ids = fields.One2many("t.line", "order_id")
    ref = fields.Reference(selection=[("res.partner", "Partner")])

class Line(models.Model):
    _name = "t.line"

    order_id = fields.Many2one("t.order")
    label = fields.Text()
"""


def _messages(run_mypy: RunMypy, source: str) -> list[str]:
    result = run_mypy(
        {
            "t_base": {"models.py": T_BASE},
            "t_use": {"depends": ["t_base"], "models.py": source},
        },
        check=["t_use"],
    )
    return result.messages("t_use/models.py")


MAPPED = """
from odoo import models

class Order(models.Model):
    _inherit = "t.order"

    def check(self) -> None:
        reveal_type(self.mapped("partner_id.name"))
        reveal_type(self.mapped("amount"))
        reveal_type(self.mapped("line_ids.label"))
        reveal_type(self.mapped("line_ids").order_id.name)
        reveal_type(self.grouped("partner_id"))
        self.mapped("partner_id.nmae")
        self.filtered("line_ids.lable")
        self.grouped("partner_id.name")
        self.mapped(lambda order: order.name)
"""


def test_mapped_types_and_paths(run_mypy: RunMypy) -> None:
    messages = _messages(run_mypy, MAPPED)

    assert messages[:3] == [
        '8: note: Revealed type is "list[str | Literal[False]]"',
        '9: note: Revealed type is "list[float]"',
        '10: note: Revealed type is "list[str | Literal[False]]"',
    ]
    assert messages[3] == '11: note: Revealed type is "str | Literal[False]"'
    assert messages[4].startswith('12: note: Revealed type is "dict[odoo.addons.base')
    assert [line.split(";")[0] for line in messages[5:]] == [
        '13: error: Odoo model "res.partner" has no field "nmae" in mapped("partner_id.nmae")',
        '14: error: Odoo model "t.line" has no field "lable" in filtered("line_ids.lable")',
        '15: error: grouped("partner_id.name") takes a field name  [odoo-field-ref]',
    ]


SORTED = """
from odoo import models

class Order(models.Model):
    _inherit = "t.order"

    def check(self) -> None:
        reveal_type(self.sorted("name"))
        self.sorted("nmae")
"""


def test_sorted_field_checked(run_mypy: RunMypy) -> None:
    messages = _messages(run_mypy, SORTED)

    assert [message.split(";")[0] for message in messages] == [
        '8: note: Revealed type is "t_use.models.Order"',
        '9: error: Odoo model "t.order" has no field "nmae" in sorted("nmae")',
    ]


SORTED_ORDER = """
from odoo import models

class Order(models.Model):
    _inherit = "t.order"

    def check(self) -> None:
        self.sorted("partner_id.name desc, amount, id")
        self.sorted("name desc, amont")
"""


def test_sorted_order_checked_on_19(run_mypy: RunMypy, odoo_version: int) -> None:
    messages = [line.split(";")[0] for line in _messages(run_mypy, SORTED_ORDER)]

    if odoo_version < 19:
        assert messages == [
            '8: error: sorted("partner_id.name desc, amount, id") takes a field name  '
            "[odoo-field-ref]",
            '9: error: sorted("name desc, amont") takes a field name  [odoo-field-ref]',
        ]
    else:
        assert messages == [
            '9: error: Odoo model "t.order" has no field "amont" in sorted("name desc, amont")'
        ]


CONSTANT_PATHS = """
from typing import Final, Literal
from odoo import models

PARTNER_NAME: Final = "partner_id.name"
TYPO: Final = "partner_id.nmae"
NAME_TYPO: Final = "nmae"

class Order(models.Model):
    _inherit = "t.order"

    def check(self, path: Literal["amount"], text: str) -> None:
        reveal_type(self.mapped(PARTNER_NAME))
        reveal_type(self.mapped(path))
        reveal_type(self.grouped(path))
        self.filtered(TYPO)
        self.sorted(NAME_TYPO)
        self.mapped(text)  # not known before run time: not checked
"""


def test_final_and_literal_paths_checked_like_literals(run_mypy: RunMypy) -> None:
    messages = [line.split(";")[0] for line in _messages(run_mypy, CONSTANT_PATHS)]

    assert messages[:2] == [
        '13: note: Revealed type is "list[str | Literal[False]]"',
        '14: note: Revealed type is "list[float]"',
    ]
    assert messages[2].startswith('15: note: Revealed type is "dict[float, t_use.models.Order')
    assert messages[3:] == [
        '16: error: Odoo model "res.partner" has no field "nmae" in filtered("partner_id.nmae")',
        '17: error: Odoo model "t.order" has no field "nmae" in sorted("nmae")',
    ]


def test_mapped_of_relational_field_of_unknown_comodel_is_not_a_list(run_mypy: RunMypy) -> None:
    # Its value is Any: the comodel is a constant, or an override has only attributes.
    result = run_mypy(
        {
            "my_mod": {
                "models.py": """
                from odoo import fields, models

                COMODEL = "res.partner"

                class Thing(models.Model):
                    _name = "t.thing"

                    partner_id = fields.Many2one(COMODEL)
                    name = fields.Char()
                    def action(self) -> None:
                        reveal_type(self.mapped("partner_id"))
                        self.mapped("partner_id").ids
                        reveal_type(self.mapped("name"))
                        if not self.filtered("partner_id").mapped("partner_id"):
                            reveal_type(self.name)
                """
            }
        }
    )

    messages = result.messages("models.py")
    # Any on Odoo 18+ (Many2one[Any] when the comodel is no literal), records on 17.
    assert messages[0].startswith('12: note: Revealed type is "')
    assert "list" not in messages[0]
    assert messages[1:] == [
        '14: note: Revealed type is "list[str | Literal[False]]"',
        # The records of the filtered ones may be none (no records at all): reached.
        '16: note: Revealed type is "str | Literal[False]"',
    ]


def test_fields_made_in_the_database_are_not_reported_outside_vals(run_mypy: RunMypy) -> None:
    # vals_ignore_keys (x_* by default): Studio and manual fields, in every check of names;
    # a near miss of a declared x_ field stays a typo (tests/fixtures/addons/probe_mod).
    result = run_mypy(
        {
            "my_mod": {
                "models.py": """
                from odoo import api, fields, models

                class Thing(models.Model):
                    _name = "t.thing"

                    total = fields.Integer(compute="_compute_total")
                    @api.depends("x_studio_amount", "x_partner_id.code", "nope")
                    def _compute_total(self) -> None:
                        self.mapped("x_studio_amount")
                        self.search([("x_studio_amount", ">", 1)], order="x_rank desc")
                        self.read(["x_studio_amount"])
                        self.filtered("x_flag").sorted("x_rank")
                        self.search([("nope", "=", 1)])
                """
            }
        }
    )

    assert result.messages("models.py") == [
        '8: error: Odoo model "t.thing" has no field "nope" in @api.depends("nope")  '
        "[odoo-field-ref]",
        '14: error: Odoo model "t.thing" has no field "nope" in domain  [odoo-domain]',
    ]


@pytest.mark.odoo(19, 20)
def test_mapped_with_a_function_gives_its_values_or_its_records(run_mypy: RunMypy) -> None:
    """Odoo 19 declares ``list[T] | BaseModel``, which fits no parameter that takes a list."""
    messages = _messages(
        run_mypy,
        """
        from odoo import models

        class Order(models.Model):
            _inherit = "t.order"

            def check(self, name: str) -> None:
                reveal_type(self.mapped(lambda order: order.amount * 2))
                reveal_type(sum(self.mapped(lambda order: order.amount)))
                reveal_type(", ".join(self.mapped(lambda order: order.name or "")))
                reveal_type(self.mapped(lambda order: order.line_ids))
                reveal_type(self.mapped(name))
        """,
    )

    assert messages == [
        '8: note: Revealed type is "list[float]"',
        '9: note: Revealed type is "float | Literal[0]"',
        '10: note: Revealed type is "str"',
        '11: note: Revealed type is "t_base.models.Line"',
        '12: note: Revealed type is "Any"',  # a path known only at run time
    ]


def test_field_guard_on_the_records_of_env_covers_a_path(run_mypy: RunMypy) -> None:
    messages = _messages(
        run_mypy,
        """
        from odoo import models

        class Order(models.Model):
            _inherit = "t.order"

            def check(self) -> None:
                if "stock_ids" in self.env["t.line"]:
                    self.mapped("line_ids.stock_ids")
                self.mapped("line_ids.stock_ids")
        """,
    )

    assert len(messages) == 1, messages
    assert messages[0].startswith('10: error: Odoo model "t.line" has no field "stock_ids"')


FILTERED_MAPPED = """
from odoo import models

class Order(models.Model):
    _inherit = "t.order"

    def check(self) -> None:
        reveal_type(self.filtered("name").mapped("name"))
        reveal_type(self.filtered("amount").mapped("name"))
        reveal_type(self.filtered("line_ids.label").mapped("line_ids.label"))
        reveal_type(self.filtered(lambda order: order.name).mapped("name"))
        kept = self.filtered(lambda order: order.amount and order.name)
        reveal_type(kept.grouped("name"))
        reveal_type(kept.mapped("name"))
        reveal_type(self.filtered(lambda order: order.amount or order.name).mapped("name"))
        reveal_type(self.filtered(lambda order: self.name).mapped("name"))
        reveal_type(self.filtered(lambda order: order.partner_id.name).mapped("name"))
        reveal_type(self.filtered("name").grouped("name"))
        reveal_type(self.filtered(lambda order, *rest, limit=1, **more: order.name).mapped("name"))
        reveal_type(self.filtered(lambda order, other: order.name).mapped("name"))
        reveal_type(self.filtered(lambda order=None: order.name).mapped("name"))
        reveal_type(self.filtered(lambda order, *, key: order.name).mapped("name"))
        reveal_type(self.filtered(lambda order: (order := order) and order.name).mapped("name"))
        reveal_type(self.filtered(lambda order: order.name).filtered("amount").mapped("name"))
        for order in self.filtered(lambda order: order.name):
            reveal_type(order.name)
        twice = self.filtered(lambda order: order.name)
        twice = self
        reveal_type(twice.mapped("name"))
        reveal_type(self.filtered("partner_id").grouped("partner_id"))
"""


def test_mapped_of_the_field_records_are_filtered_by_has_no_empty_value(
    run_mypy: RunMypy,
) -> None:
    """``filtered("default_code").mapped("default_code")`` is a list of ``str``.

    So is it after ``filtered(lambda p: p.default_code)``, of each field ``and`` joins
    there, also through a local; ``grouped`` of such a field has no empty key.
    """
    order = "t_use.models.Order"
    messages = _messages(run_mypy, FILTERED_MAPPED)
    # records: as they were (Odoo names the partner class otherwise in each version)
    assert messages[-1].startswith('30: note: Revealed type is "dict[odoo.addons.base.')
    assert messages[-1].endswith(f'Partner, {order}]"')
    assert messages[:-1] == [
        '8: note: Revealed type is "list[str]"',
        '9: note: Revealed type is "list[str | Literal[False]]"',
        # Through a relational field the other lines' labels are mapped too.
        '10: note: Revealed type is "list[str | Literal[False]]"',
        '11: note: Revealed type is "list[str]"',
        f'13: note: Revealed type is "dict[str, {order}]"',
        '14: note: Revealed type is "list[str]"',
        # ``or``, another name's field, a path: nothing said of ``name``.
        '15: note: Revealed type is "list[str | Literal[False]]"',
        '16: note: Revealed type is "list[str | Literal[False]]"',
        '17: note: Revealed type is "list[str | Literal[False]]"',
        f'18: note: Revealed type is "dict[str, {order}]"',
        '19: note: Revealed type is "list[str]"',  # Odoo calls it with the record alone
        '20: note: Revealed type is "list[str | Literal[False]]"',  # a second, required
        '21: note: Revealed type is "list[str]"',  # the record goes to a defaulted first one
        '22: note: Revealed type is "list[str | Literal[False]]"',  # a required keyword
        '23: note: Revealed type is "list[str | Literal[False]]"',  # a lambda that binds a name
        '24: note: Revealed type is "list[str]"',  # each call of a chain keeps its fields
        '26: note: Revealed type is "str"',  # a record of a ``for`` over them
        '29: note: Revealed type is "list[str | Literal[False]]"',  # a local bound twice
    ]


ROWS_COMPLETED = """
from odoo import fields, models

class Order(models.Model):
    _name = "t.order"

    name = fields.Char()
    note = fields.Text()

    def rows(self) -> list:
        rows = self.search_read([], ["name", "note"])
        for row in rows:
            note = row.pop("note")
            row["preview"] = note
            row.update({"size": 1})
            row["nmae"]
        reveal_type(rows[0].get("name"))
        return rows
"""


def test_read_rows_the_function_completes(run_mypy: RunMypy) -> None:
    """Rows sent to the client: the keys the function adds are there, popped ones optional."""
    result = run_mypy({"my_mod": {"models.py": ROWS_COMPLETED}})

    messages = result.messages("my_mod/models.py")
    assert len(messages) == 2
    assert messages[0].startswith('16: error: "nmae" is not a valid TypedDict key; expected one of')
    for key in ("id", "name", "note", "preview", "size"):
        assert f'"{key}"' in messages[0]
    # ``name`` stays required: ``get`` gives no ``None``.
    assert messages[1] == '17: note: Revealed type is "str | Literal[False]"'
