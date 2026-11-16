"""End-to-end tests of reading fields that Odoo leaves untyped (roadmap item 4.2)."""

from __future__ import annotations

from conftest import OdooNames, RunMypy

T_BASE = """
from odoo import fields, models

class Order(models.Model):
    _name = "t.order"

    state = fields.Selection([("draft", "Draft"), ("done", "Done")])
    data = fields.Binary()
    image = fields.Image()
    ref = fields.Reference(selection=[("res.partner", "Partner"), ("t.line", "Line")])
    any_ref = fields.Reference(selection="_selection_models")
    widget = fields.Binary(compute="_compute_widget")

    def _selection_models(self):
        return []

    def _compute_widget(self):
        pass

class Line(models.Model):
    _name = "t.line"

    label = fields.Text()
"""

USE = """
from odoo import fields, models

class Order(models.Model):
    _inherit = "t.order"

    state = fields.Selection(selection_add=[("cancel", "Cancelled")])

    def check(self) -> None:
        reveal_type(self.data)
        reveal_type(self.image)
        reveal_type(self.ref)
        reveal_type(self.any_ref)
        reveal_type(self.state)
        reveal_type(self.widget)
"""


def _messages(run_mypy: RunMypy, options: str = "") -> list[str]:
    result = run_mypy(
        {
            "t_base": {"models.py": T_BASE},
            "t_use": {"depends": ["t_base"], "models.py": USE},
        },
        check=["t_use"],
        options=options,
    )
    return result.messages("t_use/models.py")


def test_binary_and_reference_fields_read_typed(run_mypy: RunMypy, odoo_version: int) -> None:
    messages = _messages(run_mypy)

    binary = "bytes | Literal[False]" if odoo_version < 20 else None
    if binary is not None:
        assert messages[:2] == [
            f'10: note: Revealed type is "{binary}"',
            f'11: note: Revealed type is "{binary}"',
        ]
    assert messages[2].startswith('12: note: Revealed type is "odoo.addons.base.')
    assert "t_base.models.Line" in messages[2]
    assert messages[2].endswith('| Literal[False]"')
    assert messages[3].endswith('BaseModel | Literal[False]"')
    assert messages[4] == '14: note: Revealed type is "str | Literal[False]"'
    if odoo_version < 20:
        assert messages[5] == '15: note: Revealed type is "Any"'


def test_literal_selection_reads_keys_of_view(run_mypy: RunMypy) -> None:
    messages = _messages(run_mypy, options="literal_selection = true\n")

    assert messages[4] == (
        "14: note: Revealed type is \"Literal['draft'] | Literal['done'] | Literal['cancel'] | "
        'Literal[False]"'
    )


READ = """
from odoo import fields, models

class Line(models.Model):
    _inherit = "t.line"

    order_id = fields.Many2one("t.order")
    tag_ids = fields.Many2many("t.order")
    count = fields.Integer()

    def check(self) -> None:
        rows = self.read(["label", "order_id", "tag_ids", "count"])
        reveal_type(rows[0])
        reveal_type(self.read(["order_id"], load="")[0]["order_id"])
        reveal_type(self.search_read([("label", "!=", False)], ["count"])[0]["count"])
        self.read(["lable"])
        reveal_type(self.read())
"""


def test_read_returns_typed_dicts_of_literal_fields(run_mypy: RunMypy, odoo_version: int) -> None:
    result = run_mypy(
        {
            "t_base": {"models.py": T_BASE},
            "t_use": {"depends": ["t_base"], "models.py": READ},
        },
        check=["t_use"],
    )
    messages = result.messages("t_use/models.py")

    assert messages[0] == (
        "13: note: Revealed type is \"TypedDict({'id': int, 'label': str | Literal[False], "
        "'order_id': tuple[int, str] | Literal[False], 'tag_ids': list[int], 'count': int})\""
    )
    assert messages[1:3] == [
        '14: note: Revealed type is "int"',  # int | Literal[False]: False is an int
        '15: note: Revealed type is "int"',
    ]
    assert messages[3].split(";")[0] == (
        '16: error: Odoo model "t.line" has no field "lable" in read()'
    )
    unread = "Any" if odoo_version == 17 else "list[dict[str, Any]]"  # 17: unannotated
    assert messages[4] == f'17: note: Revealed type is "{unread}"'


REQUIRED = """
from odoo import fields, models

class Line(models.Model):
    _inherit = "t.line"

    name = fields.Char(required=True)
    note = fields.Char()
    label = fields.Text(required=True)  # an override makes the base field required
    day = fields.Date(required=True)

    def check(self) -> None:
        reveal_type(self.name)
        reveal_type(self.note)
        reveal_type(self.label)
        reveal_type(self.day)
        reveal_type(self.display_name)

    def stored(self, flag: bool) -> None:
        # A variable with one value has the field's value type.
        once = self.name
        reveal_type(once)
        # One that gets several keeps the type Odoo declares: mypy takes a variable's
        # type from its first value, and the next one may be of a field that is not
        # required.
        if flag:
            text = self.name
        else:
            text = self.note
        reveal_type(text)
        rows = []
        rows.append((self.name, self.display_name, 1))
        rows = [(False, False, 2)]
        values = {"name": self.name}
        values["name"] = False
        # A later value fits a variable typed by its first one.
        lines = []
        lines.append("x")
        lines.append(self.name)
        reveal_type(lines)
"""


def test_required_values_read_without_false(run_mypy: RunMypy) -> None:
    def revealed(options: str) -> list[str]:
        result = run_mypy(
            {
                "t_base": {"models.py": T_BASE},
                "t_use": {"depends": ["t_base"], "models.py": REQUIRED},
            },
            check=["t_use"],
            options=options,
        )
        return [
            message.partition('"')[2].rstrip('"') for message in result.messages("t_use/models.py")
        ]

    required = revealed("")
    declared = revealed("required_values = false\n")

    assert declared[0] == declared[1] == "str | Literal[False]"
    assert required == [
        "str",
        "str | Literal[False]",  # not required
        "str",
        "datetime.date",
        "str",
        "str",  # a variable with one value
        "str | Literal[False]",  # a variable with several values; no error for the rest
        "list[str]",
    ]


FIELD_OBJECTS = """
from odoo import fields, models

class Thing(models.Model):
    _name = "t.thing"

    amount = fields.Float(digits=(16, 2))
    state = fields.Selection([("a", "A")])

    def describe(self) -> None:
        self._fields["amount"].get_digits(self.env)
        self._fields["state"].selection
        reveal_type(self._fields["amount"])
"""


def test_a_field_of_fields_by_name_is_of_its_field_class(
    run_mypy: RunMypy, names: OdooNames
) -> None:
    result = run_mypy({"my_mod": {"models.py": FIELD_OBJECTS}})

    (message,) = result.messages("models.py")
    # Odoo 17 leaves ``_fields`` untyped; in 18 ``Field`` has a dynamic base (any
    # attribute reads), the declared class stays.
    expected = {17: '"Any"', 18: '"odoo.fields.Field[Any]"'}.get(names.version, '.Float"')
    assert message.startswith("13: note: Revealed type is ")
    assert message.endswith(expected)


FIELD_TUPLE = """
from typing import NamedTuple

from odoo import fields, models


class Procurement(NamedTuple):
    product_id: fields.Many2one
    name: fields.Char
    values: dict


class Thing(models.Model):
    _name = "t.thing"

    Procurement = Procurement
    name = fields.Char()
    partner_id = fields.Many2one("res.partner")

    def procure(self) -> None:
        made = self.env["t.thing"].Procurement(self.partner_id, self.name, {})
        made.product_id.name
        Procurement(self.partner_id, "x", 1)
"""


def test_a_tuple_annotated_with_field_classes_holds_their_values(run_mypy: RunMypy) -> None:
    """Odoo 19's ``Procurement`` declares ``product_id: fields.Many2one`` for records."""
    result = run_mypy({"my_mod": {"models.py": FIELD_TUPLE}})

    (message,) = result.messages("models.py")
    assert message.startswith('23: error: Argument 3 to "Procurement" has incompatible type')
