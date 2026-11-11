"""End-to-end tests of the ``vals`` checks of ``create()`` / ``write()`` (roadmap stage 2)."""

from __future__ import annotations

from conftest import RunMypy

T_BASE = """
from odoo import fields, models

class Order(models.Model):
    _name = "t.order"

    name = fields.Char()
    note = fields.Text()
    body = fields.Html()
    count = fields.Integer()
    amount = fields.Float()
    price = fields.Monetary()
    done = fields.Boolean()
    day = fields.Date()
    moment = fields.Datetime()
    data = fields.Binary()
    picture = fields.Image()
    state = fields.Selection([("draft", "Draft"), ("done", "Done")])
    kind = fields.Selection(selection="_selection_kind")
    partner_id = fields.Many2one("res.partner")
    line_ids = fields.One2many("t.line", "order_id")
    tag_ids = fields.Many2many("res.partner.category")
    ref = fields.Reference(selection=[("res.partner", "Partner")])
    extra = fields.Json()

    def _selection_kind(self):
        return []

class Line(models.Model):
    _name = "t.line"

    order_id = fields.Many2one("t.order")

class Mixin(models.AbstractModel):
    _name = "t.mixin"

    flag = fields.Boolean()

    def touch(self) -> None:
        self.write({"flag": True, "active": False})  # fields of the models using it
        self.write({"flag": "yes"})
"""


def _messages(run_mypy: RunMypy, source: str, **kwargs: object) -> list[str]:
    result = run_mypy(
        {
            "t_base": {"models.py": T_BASE},
            "t_use": {"depends": ["t_base"], "models.py": source},
        },
        check=["t_use"],
        **kwargs,
    )
    return result.messages("t_use/models.py")


ACCEPTED = """
import datetime
from typing import Any
from odoo import models
from odoo.fields import Command

class Order(models.Model):
    _inherit = "t.order"

    def check(self, any_value: Any, text: str) -> None:
        self.write({
            "name": "x", "note": False, "body": None, "count": 1, "amount": 1, "price": 2.5,
            "done": True, "day": datetime.date.today(), "moment": "2024-01-01 00:00:00",
            "data": b"x", "picture": "aGk=", "state": "draft", "kind": text,
            "partner_id": self.env.user.partner_id, "line_ids": [Command.clear()],
            "tag_ids": [(6, 0, [1, 2])], "ref": "res.partner,1", "extra": {"a": 1},
            "id": any_value, "create_uid": 1, "display_name": any_value,
        })
        self.write({"day": datetime.datetime.now(), "partner_id": 1, "state": any_value})
        self.write({"state": text, "line_ids": self.env["t.line"], "tag_ids": False})
"""


def test_vals_values_of_every_field_class_accepted(run_mypy: RunMypy) -> None:
    assert _messages(run_mypy, ACCEPTED) == []


REJECTED = """
import datetime
from odoo import models

class Order(models.Model):
    _inherit = "t.order"

    def check(self) -> None:
        self.write({
            "name": 1,
            "count": "1",
            "amount": "1.5",
            "done": "yes",
            "day": 20240101,
            "data": 1,
            "partner_id": "abc",
            "line_ids": 1,
            "ref": 1,
        })
"""


def test_vals_values_of_wrong_type_reported(run_mypy: RunMypy) -> None:
    messages = _messages(run_mypy, REJECTED)

    assert [message.split(" (")[0] for message in messages] == [
        f'{line}: error: Incompatible type for field "{name}" of Odoo model "t.order"'
        for line, name in (
            (10, "name"),
            (11, "count"),
            (12, "amount"),
            (13, "done"),
            (14, "day"),
            (15, "data"),
            (16, "partner_id"),
            (17, "line_ids"),
            (18, "ref"),
        )
    ]
    assert messages[0].endswith(
        '(expression has type "int", Char field takes "str | Literal[False] | None")  '
        "[odoo-vals-type]"
    )


SELECTION = """
from odoo import fields, models

class Order(models.Model):
    _inherit = "t.order"

    state = fields.Selection(selection_add=[("sent", "Sent")])

    def check(self, flag: bool) -> None:
        self.write({"state": "sent"})
        self.write({"state": "cancel"})
        self.write({"state": "draft" if flag else "gone", "kind": "anything"})
"""


def test_vals_selection_literal_not_in_keys_reported(run_mypy: RunMypy) -> None:
    assert _messages(run_mypy, SELECTION) == [
        '11: error: Value "cancel" is not in the selection of field "state" of Odoo model '
        '"t.order" ("draft", "done", "sent")  [odoo-vals-type]',
        '12: error: Value "gone" is not in the selection of field "state" of Odoo model '
        '"t.order" ("draft", "done", "sent")  [odoo-vals-type]',
    ]


COMODEL = """
from odoo import models

class Order(models.Model):
    _inherit = "t.order"

    def check(self) -> None:
        self.write({"partner_id": self.env.user})
        self.write({"partner_id": self.env.user.partner_id, "line_ids": self})
"""


def test_vals_records_of_other_model_reported(run_mypy: RunMypy) -> None:
    assert _messages(run_mypy, COMODEL) == [
        '8: error: Incompatible records for field "partner_id" of Odoo model "t.order" '
        '(expression has "res.users" records, Many2one field takes "res.partner" records)  '
        "[odoo-vals-type]",
        '9: error: Incompatible records for field "line_ids" of Odoo model "t.order" '
        '(expression has "t.order" records, One2many field takes "t.line" records)  '
        "[odoo-vals-type]",
    ]


KEYS = """
from typing import Any
from odoo import models

class Order(models.Model):
    _inherit = "t.order"

    def check(self, vals: dict[str, Any], key: str) -> None:
        self.create({"nmae": "x", "x_custom": 1, key: 1, **vals})
        self.create([{"name": "x"}, {"cuont": 1}])
        self.env["t.line"].create({"order_id": self.id, "message": "x"})
        self.write(vals)
        self.write(dict(nope=1))
        records = self.create({"name": "x"})
        reveal_type(records)
"""


def test_vals_unknown_keys_reported_unless_ignored_or_not_literal(run_mypy: RunMypy) -> None:
    assert _messages(run_mypy, KEYS) == [
        '9: error: Odoo model "t.order" has no field "nmae"; did you mean "name"?  [odoo-vals-key]',
        '10: error: Odoo model "t.order" has no field "cuont"; did you mean "count"?  '
        "[odoo-vals-key]",
        '11: error: Odoo model "t.line" has no field "message"  [odoo-vals-key]',
        '15: note: Revealed type is "t_use.models.Order"',
    ]


def test_vals_ignore_keys_option_empty_reports_manual_fields(run_mypy: RunMypy) -> None:
    messages = _messages(run_mypy, KEYS, options="vals_ignore_keys = []\n")

    assert '9: error: Odoo model "t.order" has no field "x_custom"  [odoo-vals-key]' in messages


def test_vals_abstract_model_checks_values_not_keys(run_mypy: RunMypy) -> None:
    result = run_mypy({"t_base": {"models.py": T_BASE}})

    assert [line.split(" (")[0] for line in result.messages("t_base/models.py")] == [
        '41: error: Incompatible type for field "flag" of Odoo model "t.mixin"'
    ]


VIEW = """
from odoo import fields, models

class Early(models.Model):
    _inherit = "t.order"

    def check(self) -> None:
        self.write({"later": 1, "inherited": "x"})

class Later(models.Model):
    _inherit = "t.order"

    later = fields.Char()

class Child(models.Model):
    _name = "t.child"
    _inherits = {"t.order": "order_id"}

    order_id = fields.Many2one("t.order")

    def check(self) -> None:
        self.create({"order_id": 1, "count": "x", "name": "y"})
"""


def test_vals_fields_from_view_and_delegation(run_mypy: RunMypy) -> None:
    # Odoo 19+ also reports the plain-dict ``_inherits`` (declared ``frozendict``).
    messages = [line for line in _messages(run_mypy, VIEW) if "[odoo-vals" in line]

    assert [message.split(" (")[0] for message in messages] == [
        '8: error: Incompatible type for field "later" of Odoo model "t.order"',
        '8: error: Odoo model "t.order" has no field "inherited"  [odoo-vals-key]',
        '22: error: Incompatible type for field "count" of Odoo model "t.child"',
    ]


def test_vals_error_codes_can_be_disabled_together(run_mypy: RunMypy) -> None:
    messages = _messages(run_mypy, KEYS, flags=["--disable-error-code", "odoo-vals"])

    assert messages == ['15: note: Revealed type is "t_use.models.Order"']


CACHED = """
from odoo import models

class Order(models.Model):
    _inherit = "t.order"

    def check(self) -> None:
        self.write({"name": 1})
"""


def test_vals_cache_field_class_changed_in_dependency_rechecks(run_mypy: RunMypy) -> None:
    # Cross-cutting rule 1: the warm run gives what a cold run gives.
    addons = {"t_use": {"depends": ["t_base"], "models.py": CACHED}}
    first = run_mypy({"t_base": {"models.py": T_BASE}, **addons}, check=["t_use"])
    changed = T_BASE.replace("name = fields.Char()", "name = fields.Integer()")

    second = run_mypy({"t_base": {"models.py": changed}}, check=["t_use"])

    assert len(first.messages("t_use/models.py")) == 1
    assert second.messages("t_use/models.py") == []
