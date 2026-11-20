"""End-to-end tests of reading fields that Odoo leaves untyped (roadmap item 4.2)."""

from __future__ import annotations

import pytest
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

    # Odoo 20 declares the value of a Binary field itself, of a computed one too.
    declared = "odoo.tools.binary.BinaryValue"
    binary = "bytes | Literal[False]" if odoo_version < 20 else declared
    assert messages[:2] == [
        f'10: note: Revealed type is "{binary}"',
        f'11: note: Revealed type is "{binary}"',
    ]
    assert messages[2].startswith('12: note: Revealed type is "odoo.addons.base.')
    assert "t_base.models.Line" in messages[2]
    assert messages[2].endswith('| None"')  # empty, a Reference reads None, not False
    # A selection the scan cannot read: records of a model known only at run time.
    assert messages[3] == '13: note: Revealed type is "Any | None"'
    computed = "Any" if odoo_version < 20 else declared
    assert messages[4:] == [
        '14: note: Revealed type is "str | Literal[False]"',
        f'15: note: Revealed type is "{computed}"',
    ]


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
    owner_id = fields.Many2one("t.order", required=True)

    def check(self) -> None:
        rows = self.read(["label", "order_id", "tag_ids", "count"])
        reveal_type(rows[0])
        reveal_type(self.read(["order_id"], load="")[0]["order_id"])
        reveal_type(self.search_read([("label", "!=", False)], ["count"])[0]["count"])
        self.read(["lable"])
        reveal_type(self.read())
        reveal_type(self.read(["owner_id"])[0]["owner_id"])
        reveal_type(self.read(["owner_id"], load="")[0]["owner_id"])
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
        "14: note: Revealed type is \"TypedDict({'id': int, 'label': str | Literal[False], "
        "'order_id': tuple[int, str] | Literal[False], 'tag_ids': list[int], 'count': int})\""
    )
    assert messages[1:3] == [
        '15: note: Revealed type is "int"',  # int | Literal[False]: False is an int
        '16: note: Revealed type is "int"',
    ]
    assert messages[3].split(";")[0] == (
        '17: error: Odoo model "t.line" has no field "lable" in read()'
    )
    unread = "Any" if odoo_version == 17 else "list[dict[str, Any]]"  # 17: unannotated
    assert messages[4] == f'18: note: Revealed type is "{unread}"'
    # A required many2one is never False, as reading the field is not (required_values).
    assert messages[5:] == [
        '19: note: Revealed type is "tuple[int, str]"',
        '20: note: Revealed type is "int"',
    ]


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
        # One that gets several takes the type of each value (odoo_typing.rebound), and
        # where values of other types meet it is ``Any``: no value is checked against
        # the first.
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
        # ``+=`` does not give the variable another value type: the required value stays.
        label = self.name
        if flag:
            label += " (x)"
        reveal_type(label)
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
        "Any",  # a variable with several values, where they meet; no error for the rest
        "list[str]",
        "str",  # changed by ``+=`` only
    ]


RELATED_REQUIRED = """
from odoo import fields, models

class Value(models.Model):
    _name = "t.value"

    name = fields.Char(required=True)
    code = fields.Char()

class Line(models.Model):
    _name = "t.value.line"

    value_id = fields.Many2one("t.value", required=True)
    other_id = fields.Many2one("t.value")
    name = fields.Char(related="value_id.name")
    code = fields.Char(related="value_id.code")
    other_name = fields.Char(related="other_id.name")
    named = fields.Char(related="name")

    def check(self) -> None:
        reveal_type(self.name)
        reveal_type(self.code)
        reveal_type(self.other_name)
        reveal_type(self.named)
        reveal_type(self.read(["name", "code"])[0])
"""


def test_a_related_path_of_required_fields_reads_as_required(run_mypy: RunMypy) -> None:
    """``ptav.name`` is related to ``product_attribute_value_id.name``, both required."""
    messages = run_mypy({"my_mod": {"models.py": RELATED_REQUIRED}}).messages("models.py")

    assert [message.partition('"')[2].rstrip('"') for message in messages] == [
        "str",
        "str | Literal[False]",  # the last field is not required
        "str | Literal[False]",  # a hop is not required
        "str",  # related to a related field of required ones
        "TypedDict({'id': int, 'name': str, 'code': str | Literal[False]})",
    ], messages


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
    later = """
    from odoo import models

    class Thing(models.Model):
        _inherit = "t.thing"

        def procure_later(self) -> None:
            self.env["t.thing"].Procurement(self.partner_id, self.name, {})
    """
    result = run_mypy(
        {
            "my_mod": {
                "__init__.py": "from . import models, later\n",
                "models.py": FIELD_TUPLE,
                "later.py": later,
            }
        }
    )

    assert result.messages("later.py") == []  # through the class that inherits the alias
    (message,) = result.messages("models.py")
    assert message.startswith('23: error: Argument 3 to "Procurement" has incompatible type')


FALSE_OPERAND = """
from datetime import timedelta

from odoo import fields, models

class Picking(models.Model):
    _name = "t.picking"

    scheduled = fields.Datetime()
    deadline = fields.Datetime(required=True)
    delay = fields.Integer()

    def on_time(self) -> bool:
        late = abs(self.scheduled - self.deadline) <= timedelta(seconds=1)
        reveal_type(self.delay - 1)
        reveal_type(False - 1)
        return late

    def from_text(self, text: str) -> None:
        planned = fields.Datetime.from_string(text)
        out = planned + timedelta(days=self.delay)
        abs(planned - out)
"""


def test_an_operator_false_does_not_support_is_one_error(
    run_mypy: RunMypy, odoo_version: int
) -> None:
    """``abs(date - other) <= delta`` of an empty date: the operand, no error about ``int``."""
    messages = run_mypy({"my_mod": {"models.py": FALSE_OPERAND}}).messages("models.py")

    errors = [message.split("  [")[0] for message in messages if ": error: " in message]
    # Odoo 19+ declares ``from_string`` as ``datetime | None``: the operands are
    # reported, and no error about ``abs`` of a ``datetime`` that no value is. Odoo 17
    # and 18 leave it untyped: nothing more is reported.
    declared = [
        '21: error: Unsupported operand types for + ("None" and "timedelta")',
        '22: error: Unsupported left operand type for - ("None")',
    ]
    assert errors == [
        '14: error: Unsupported operand types for - ("Literal[False]" and "datetime")',
        *(declared if odoo_version >= 19 else []),
    ], messages
    assert [message for message in messages if ": note: Revealed" in message] == [
        '15: note: Revealed type is "int"',
        '16: note: Revealed type is "int"',
    ]


RECORD_IDS = """
from odoo import models

def label(record_id: int) -> str:
    return str(record_id)

class Thing(models.Model):
    _name = "t.thing"

    def check(self, totals: dict[int, float]) -> None:
        reveal_type(self.id)
        for record in self.search([]):
            reveal_type(record._origin.id)
            totals.get(record.id)
            label(record.id)
"""


@pytest.mark.parametrize("new", [False, True], ids=["saved", "new_record_ids"])
def test_record_id_is_an_int_unless_new_record_ids(
    run_mypy: RunMypy, names: OdooNames, new: bool
) -> None:
    """Odoo 17 and 18: ``int | NewId``, the id of a record of ``new()`` or of an onchange."""
    result = run_mypy(
        {"my_mod": {"models.py": RECORD_IDS}}, options="new_record_ids = true\n" if new else ""
    )

    messages = [line.partition(": ")[2] for line in result.messages("my_mod/models.py")]
    declared = names.version < 19  # Odoo 19 and 20 leave the field's ``__get__`` untyped
    new_id = "odoo.models.NewId" if names.version == 17 else "odoo.api.NewId"
    read = "Any" if not declared else f"int | {new_id}" if new else "int"
    origin = "Any" if names.version == 17 else read  # 17: ``_origin`` is untyped
    errors = [
        'error: Argument 1 to "get" of "dict" has incompatible type "int | NewId"; expected "int"'
        "  [arg-type]",
        'error: Argument 1 to "label" has incompatible type "int | NewId"; expected "int"'
        "  [arg-type]",
    ]
    assert messages == [
        f'note: Revealed type is "{read}"',
        f'note: Revealed type is "{origin}"',
        *(errors if new and declared else []),
    ]


REFERENCE_FIELDS = """
from odoo import fields, models

class Wizard(models.Model):
    _name = "t.wizard"

    target = fields.Reference(selection="_selection_target", required=True)
    optional = fields.Reference(selection="_selection_target")
    partner = fields.Reference(selection=[("res.partner", "Partner")], required=True)

    def _selection_target(self):
        return []

    def use(self) -> None:
        self.target.collaborator_ids
        self.optional.collaborator_ids
        reveal_type(self.partner)
        for found in self.search([("optional", "!=", False)]):
            reveal_type(found.optional)
"""


def test_reference_to_a_model_known_at_run_time(run_mypy: RunMypy) -> None:
    """Any record of some model; ``dynamic_env = "base"`` keeps records of no model."""
    plain = run_mypy({"my_mod": {"models.py": REFERENCE_FIELDS}}).messages("my_mod/models.py")
    base = run_mypy(
        {"my_mod": {"models.py": REFERENCE_FIELDS}}, options='dynamic_env = "base"\n'
    ).messages("my_mod/models.py")

    # The field may be empty: a record whose model is not known, or None.
    assert plain[0] == (
        '16: error: Item "None" of "Any | None" has no attribute "collaborator_ids"  [union-attr]'
    )
    # Required: no None. Found by a search that asks for a value: none either.
    assert plain[1].startswith('17: note: Revealed type is "odoo.addons.base.')
    assert ".res_partner." in plain[1]  # Partner, ResPartner on Odoo 19
    assert "None" not in plain[1]
    assert plain[2:] == ['19: note: Revealed type is "Any"']
    errors = [message for message in base if ": error: " in message]
    assert [message.partition(": error: ")[2].split('"')[1] for message in errors] == [
        "BaseModel",
        "BaseModel",
        "None",
    ]
