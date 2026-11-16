"""End-to-end tests of fields declared with field classes that addons define."""

from __future__ import annotations

from conftest import RunMypy

FIELDS = """
from odoo import fields

class BigInteger(fields.Integer):
    column_type = ("int8", "int8")

class Packed(fields.Json):
    def convert_to_cache(self, value, record, validate=True):
        return value
"""
T_BASE = """
from odoo import fields, models
from .custom import BigInteger, Packed

class Chat(models.Model):
    _name = "t.chat"

    name = fields.Char()
    chat_int_id = BigInteger(required=True)
    payload = Packed()
"""
T_USE = """
from odoo import api, fields, models

class Chat(models.Model):
    _inherit = "t.chat"

    total = fields.Integer(compute="_compute_total")

    @api.depends("chat_int_id", "payload")
    def _compute_total(self) -> None:
        for chat in self:
            chat.total = chat.chat_int_id

    def check(self) -> None:
        self.search([("chat_int_id", "=", 1), ("payload", "!=", False)])
        self.create({"name": "x", "chat_int_id": 1, "payload": {"a": [1]}})
        self.write({"chat_int_id": "one"})
        self.write({"chat_missing_id": 1})
        self.search([("chat_int_id.name", "=", "x")])
        values: "odoo.values.t_chat" = {"chat_int_id": 1, "payload": "any"}
"""


def test_field_of_an_addons_field_class_is_a_field_of_the_odoo_class(run_mypy: RunMypy) -> None:
    result = run_mypy(
        {
            "t_base": {"custom.py": FIELDS, "models.py": T_BASE},
            "t_use": {"depends": ["t_base"], "models.py": T_USE},
        },
        check=["t_use"],
    )

    messages = result.messages("t_use/models.py")
    assert [message.split(" (expression")[0] for message in messages] == [
        '17: error: Incompatible type for field "chat_int_id" of Odoo model "t.chat"',
        '18: error: Odoo model "t.chat" has no field "chat_missing_id"; '
        'did you mean "chat_int_id"?  [odoo-vals-key]',
        '19: error: Field "chat_int_id" of Odoo model "t.chat" in domain is not relational  '
        "[odoo-domain]",
    ]
    # Checked as the Odoo class it derives from; the class that converts values is not.
    assert 'has type "str", Integer field takes "int' in messages[0]
    assert result.stderr == ""
