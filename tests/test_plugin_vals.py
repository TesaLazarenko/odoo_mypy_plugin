"""End-to-end tests of the ``vals`` checks of ``create()`` / ``write()`` (roadmap stage 2)."""

from __future__ import annotations

import pytest
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
    widget = fields.Binary(compute="_compute_widget")

    def _selection_kind(self):
        return []

    def _compute_widget(self):
        pass

class Line(models.Model):
    _name = "t.line"

    order_id = fields.Many2one("t.order")

class Mixin(models.AbstractModel):
    _name = "t.mixin"

    flag = fields.Integer()

    def touch(self) -> None:
        self.write({"flag": 1, "active": False})  # fields of the models using it
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
            "done": "yes",  # Boolean takes anything
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
        '13: error: Odoo model "t.order" has no field "nope"; did you mean "note"?  '
        "[odoo-vals-key]",
        '15: note: Revealed type is "t_use.models.Order"',
    ]


def test_vals_ignore_keys_option_empty_reports_manual_fields(run_mypy: RunMypy) -> None:
    messages = _messages(run_mypy, KEYS, options="vals_ignore_keys = []\n")

    assert '9: error: Odoo model "t.order" has no field "x_custom"  [odoo-vals-key]' in messages


def test_vals_abstract_model_checks_values_not_keys(run_mypy: RunMypy) -> None:
    result = run_mypy({"t_base": {"models.py": T_BASE}})

    assert [line.split(" (")[0] for line in result.messages("t_base/models.py")] == [
        '45: error: Incompatible type for field "flag" of Odoo model "t.mixin"'
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


ASSIGNED = """
import datetime
from typing import Any
from odoo import api, models

class Order(models.Model):
    _inherit = "t.order"

    @api.depends("count")
    def _compute_all(self) -> None:
        for order in self:
            order.name = order.partner_id.name
            order.amount = order.count * 1.5
            order.partner_id = self.env.user.partner_id
            order.partner_id = False
            order.day = datetime.date.today()
            order.state = "done"
            order.line_ids = self.env["t.line"]
            order.extra = {"a": 1}
            order.count = "1"
            order.state = "cancel"
            order.partner_id = "abc"
            order.partner_id = self.env.user
"""


def test_vals_assignment_to_field_checks_value(run_mypy: RunMypy) -> None:
    messages = _messages(run_mypy, ASSIGNED)

    assert [message.split(" (")[0] for message in messages] == [
        '20: error: Incompatible type for field "count" of Odoo model "t.order"',
        '21: error: Value "cancel" is not in the selection of field "state" of Odoo model '
        '"t.order"',
        '22: error: Incompatible type for field "partner_id" of Odoo model "t.order"',
        '23: error: Incompatible records for field "partner_id" of Odoo model "t.order"',
    ]


COMMANDS = """
from odoo import models
from odoo.fields import Command

class Order(models.Model):
    _inherit = "t.order"

    def check(self) -> None:
        self.create({"line_ids": [
            Command.create({"order_id": "x"}),
            Command.update(1, {"nope": 1}),
            (0, 0, {"order_id": False, "bad": 1}),
            Command.link(1),
            (4, 1, 0),
        ]})
        self.line_ids = [Command.create({"order_id": self, "missing": 1})]
"""


def test_vals_commands_of_x2many_checked_against_comodel(run_mypy: RunMypy) -> None:
    messages = [line.split(" (")[0].split(";")[0] for line in _messages(run_mypy, COMMANDS)]

    assert messages == [
        '10: error: Incompatible type for field "order_id" of Odoo model "t.line"',
        '11: error: Odoo model "t.line" has no field "nope"  [odoo-vals-key]',
        '12: error: Odoo model "t.line" has no field "bad"  [odoo-vals-key]',
        '16: error: Odoo model "t.line" has no field "missing"  [odoo-vals-key]',
    ]


COPY = """
from odoo import models

class Order(models.Model):
    _inherit = "t.order"

    def check(self) -> None:
        copied = self.copy({"name": 1})
        self.copy(default={"nmae": "x"})
        self.copy()
        reveal_type(copied)
"""


def test_vals_copy_default_checked(run_mypy: RunMypy) -> None:
    messages = [line.split(" (")[0].split(";")[0] for line in _messages(run_mypy, COPY)]

    assert messages == [
        '8: error: Incompatible type for field "name" of Odoo model "t.order"',
        '9: error: Odoo model "t.order" has no field "nmae"',
        '11: note: Revealed type is "t_use.models.Order"',
    ]


IDIOMS = """
from odoo import models

class Order(models.Model):
    _inherit = "t.order"

    def _compute_widget(self) -> None:
        for order in self:
            order.widget = {"lines": []}  # a computed Binary holds any data
            order.done = order.line_ids  # Boolean: bool(value)
            order.day = min([line.order_id.day for line in order.line_ids], default=False)
            order.state = order.name and ""  # falsy: stored as False
            label = False
            if order.count:
                label = True
            order.name = label  # bool: usually False only

    def _compute_partner(self) -> None:
        # A string condition gives "": no record, 0 (``int(value or 0)``).
        self.partner_id = self.note and self.partner_id.id
        self.count = self.note and 3
        self.amount = self.note and 1.5
"""


def test_vals_odoo_idioms_accepted(run_mypy: RunMypy) -> None:
    assert [line for line in _messages(run_mypy, IDIOMS) if "[odoo-vals" in line] == []


CONTEXT_DEFAULTS = """
from odoo import models

class Line(models.Model):
    _inherit = "t.line"

    def check(self) -> None:
        orders = self.env["t.order"]
        orders.with_context(default_state="draft", default_count=2).create({})
        orders.with_context(default_stat="draft", default_extract_state="x").create({})
        orders.with_context({"default_count": "two", "active_test": False}).create({})
        orders.with_context(default_stat="draft").search([])  # not the created model
        orders.with_context(default_stat=False).create({})  # clears another model's default
"""


def test_vals_context_defaults_of_create_checked(run_mypy: RunMypy) -> None:
    assert _messages(run_mypy, CONTEXT_DEFAULTS) == [
        '10: error: Odoo model "t.order" has no field "stat" (default_stat in with_context); '
        'did you mean "state"?  [odoo-vals-key]',
        '11: error: Incompatible type for field "count" of Odoo model "t.order" (expression '
        'has type "str", Integer field takes "int | NewId | Literal[False] | None")  '
        "[odoo-vals-type]",
    ]


FIELD_GUARDS = """
from odoo import models

class Order(models.Model):
    _inherit = "t.order"

    def check(self) -> None:
        if "studio_flag" in self._fields:
            self.write({"studio_flag": 1, "studio_typo": 2})
            self.search([("studio_flag", "=", 1)])
            self.mapped("studio_flag")
            self.read(["studio_flag"])
        self.write({"studio_flag": 1})
        if not hasattr(self, "studio_note"):
            return
        self.create({"studio_note": "x"})
        self.filtered("partner_id.studio_note")
"""


def test_field_guards_keep_optional_fields_unreported(run_mypy: RunMypy) -> None:
    messages = [line.split(";")[0] for line in _messages(run_mypy, FIELD_GUARDS)]

    assert messages == [
        '9: error: Odoo model "t.order" has no field "studio_typo"  [odoo-vals-key]',
        '13: error: Odoo model "t.order" has no field "studio_flag"  [odoo-vals-key]',
    ]


BUILT_VALS = """
from typing import Any
from odoo import models

class Order(models.Model):
    _inherit = "t.order"

    def check(self, rows: list[dict[str, Any]]) -> None:
        vals: dict[str, Any] = {"nmae": "x", "count": "bad"}
        vals["stat"] = "draft"
        vals.update({"notee": 1}, done=True)
        vals.setdefault("amount", "x")
        self.write(vals)
        self.create(dict(vals, nmae2=1))
        vals = {"name": "ok"}
        self.write(vals)
        vals_list = []
        line = {"lable": 1}
        vals_list.append(line)
        vals_list.append({"count": 2})
        vals_list += [{"amont": 1}]
        self.create(vals_list)
        orders = self.with_context(default_stat="draft")
        orders.create({})
        ctx = {"default_stat": "x"}
        self.with_context(ctx).create({})
        later: dict[str, Any] = {}
        self.write(later)
        later["nmae"] = 1
        for vals in rows:
            self.write(vals)
"""


def test_vals_built_in_a_variable_checked(run_mypy: RunMypy) -> None:
    messages = [line.split(";")[0] for line in _messages(run_mypy, BUILT_VALS)]

    assert messages == [
        '9: error: Odoo model "t.order" has no field "nmae"',
        '9: error: Incompatible type for field "count" of Odoo model "t.order" (expression '
        'has type "str", Integer field takes "int | NewId | Literal[False] | None")  '
        "[odoo-vals-type]",
        '10: error: Odoo model "t.order" has no field "stat"',
        '11: error: Odoo model "t.order" has no field "notee"',
        '12: error: Incompatible type for field "amount" of Odoo model "t.order" (expression '
        'has type "str", Float field takes "float | Literal[False] | None")  [odoo-vals-type]',
        '14: error: Odoo model "t.order" has no field "nmae2"',
        # 16: rebound to {"name": ...}: the earlier keys are not its keys
        '18: error: Odoo model "t.order" has no field "lable"  [odoo-vals-key]',
        '21: error: Odoo model "t.order" has no field "amont"',
        '23: error: Odoo model "t.order" has no field "stat" (default_stat in with_context)',
        '25: error: Odoo model "t.order" has no field "stat" (default_stat in with_context)',
        # 28: the key set after the call; 30: a loop variable
    ]


BRANCHES = """
from odoo import models

class Line(models.Model):
    _inherit = "t.line"

    def make(self, kind: str):
        orders = self.env["t.order"]
        vals = {"name": "x"}
        if kind == "a":
            vals = {"email": "x"}  # a res.partner field
            return self.env["res.partner"].create(vals)
        else:
            orders.create(vals)  # the dict of the other arm does not reach here
        match kind:
            case "b":
                vals = {"email": "x"}
                self.env["res.partner"].create(vals)
            case {"vals": captured}:
                orders.create(captured)
            case _:
                orders.create(vals)
        orders.create({"nme": 1})
        self.env[b"t.order"]
"""


def test_vals_of_another_arm_of_if_or_match_do_not_reach_the_call(run_mypy: RunMypy) -> None:
    messages = _messages(run_mypy, BRANCHES)

    assert [message for message in messages if "odoo-vals" in message] == [
        '23: error: Odoo model "t.order" has no field "nme"; did you mean "name"?  [odoo-vals-key]',
    ]
    assert not any("odoo-model" in message for message in messages)  # b"..." is no name


@pytest.mark.odoo(20)
def test_field_takes_what_reading_it_gives(run_mypy: RunMypy) -> None:
    """Odoo 20 reads a binary field as a ``BinaryValue``, which another one takes."""
    result = run_mypy(
        {
            "my_mod": {
                "models.py": """
                from odoo import fields, models

                class Thing(models.Model):
                    _name = "t.thing"
                    _description = "Thing"

                    image = fields.Image()
                    data = fields.Binary()

                    def copy_from(self, other: "Thing") -> None:
                        self.image = other.image
                        self.write({"data": other.data, "image": b"raw"})
                        self.image = 5
                """
            }
        }
    )

    messages = result.messages("models.py")
    assert len(messages) == 1, messages
    assert messages[0].startswith(
        '14: error: Incompatible type for field "image" of Odoo model "t.thing" (expression has'
        ' type "int"'
    )


TAKEN_KEYS = """
from odoo import api, models

class Order(models.Model):
    _inherit = "t.order"

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            counterpart = vals.pop("counterpart_id", None)
        return super().create(vals_list)

    def write(self, vals):
        if "recompute" in vals:
            del vals["recompute"]
        return super().write(vals)

    def check(self) -> None:
        self.create({"counterpart_id": 1, "nmae": "x"})
        self.write({"recompute": True})
        self.env["t.line"].create({"counterpart_id": 1})
"""


def test_keys_that_create_or_write_read_are_taken(run_mypy: RunMypy) -> None:
    """An override that pops a key from the values takes it although it is no field."""
    assert [line.split(";")[0].split("  [")[0] for line in _messages(run_mypy, TAKEN_KEYS)] == [
        '19: error: Odoo model "t.order" has no field "nmae"',
        '21: error: Odoo model "t.line" has no field "counterpart_id"',
    ]
