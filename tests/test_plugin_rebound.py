"""Local variables bound more than once take the type of each value (odoo_typing.rebound)."""

from __future__ import annotations

from conftest import OdooNames, RunMypy

MODELS = """
from odoo import fields, models

class Thing(models.Model):
    _name = "t.thing"

    name = fields.Char()
    amount = fields.Float()
    partner_id = fields.Many2one("res.partner")

    def empty_first(self) -> None:
        things = []
        things = self.search([])
        reveal_type(things)
        things.name

    def records_first(self) -> None:
        partner = self.partner_id
        if partner.name:
            partner = False
        reveal_type(partner)
        partner = self.partner_id
        reveal_type(partner)
        partner.missing

    def numbers(self) -> None:
        total = 0
        for thing in self:
            total += thing.amount
        reveal_type(total)

    def loops(self) -> None:
        for line in self:
            line.name
        for line in self.partner_id:
            line.missing

    def once(self) -> None:
        count = 0
        count.missing
        names = []
        names.append(self.name)
        reveal_type(names)

    def kept(self, value: int) -> None:
        value = 1
        value = "x"
        declared: int = 0
        declared = "x"
"""


def test_each_binding_of_a_rebound_variable_has_the_value_s_type(
    run_mypy: RunMypy, names: OdooNames
) -> None:
    result = run_mypy({"my_mod": {"models.py": MODELS}})
    partner = names.partner.rpartition(".")[2]

    assert result.messages("models.py") == [
        '14: note: Revealed type is "my_mod.models.Thing"',
        # ``False`` stays a literal where the branches that gave it values meet.
        f'21: note: Revealed type is "Literal[False] | {names.partner}"',
        f'23: note: Revealed type is "{names.partner}"',
        f'24: error: "{partner}" has no attribute "missing"  [attr-defined]',
        '30: note: Revealed type is "Any"',  # the head of the loop: int, then float
        f'36: error: "{partner}" has no attribute "missing"  [attr-defined]',
        '40: error: "int" has no attribute "missing"  [attr-defined]',
        '43: note: Revealed type is "list[str | Literal[False]]"',
        '47: error: Incompatible types in assignment (expression has type "str", variable has '
        'type "int")  [assignment]',
        '49: error: Incompatible types in assignment (expression has type "str", variable has '
        'type "int")  [assignment]',
    ]


def test_rebound_variables_keep_mypy_s_inference_when_asked_to(run_mypy: RunMypy) -> None:
    result = run_mypy(
        {"my_mod": {"models.py": MODELS}},
        options="rebound_variables = false\n",
    )

    messages = result.messages("models.py")
    assert any(
        message.startswith("13: error: Incompatible types in assignment") for message in messages
    )


FALSE_MODELS = """
from odoo import fields, models

class Thing(models.Model):
    _name = "t.thing"

    name = fields.Char()
    partner_id = fields.Many2one("res.partner")

    def conditional(self) -> None:
        partner = self.partner_id if self.name else False
        reveal_type(partner)
        if partner:
            partner.name
        last = False
        for thing in self:
            if thing.name:
                last = thing
        reveal_type(last)
        if last:
            last.name
        found = self.name and self.partner_id or False
        if found:
            found.name
        first = next((thing for thing in self if thing.name), False)
        if first:
            first.name
        missing = None
        for thing in self:
            missing = thing
        reveal_type(missing)
        missing.name
"""


def test_a_variable_that_may_be_false_keeps_false_as_a_literal(
    run_mypy: RunMypy, names: OdooNames
) -> None:
    result = run_mypy({"my_mod": {"models.py": FALSE_MODELS}})

    messages = result.messages("models.py")
    assert messages[0] == f'12: note: Revealed type is "{names.partner} | Literal[False]"'
    assert messages[1] == '19: note: Revealed type is "Literal[False] | my_mod.models.Thing"'
    # ``x = None`` is a placeholder a later value replaces, as mypy takes it.
    assert messages[2] == '31: note: Revealed type is "Any"'
    assert len(messages) == 3


VIEW_FIRST = """
from odoo import fields, models

class Thing(models.Model):
    _name = "t.thing"

    name = fields.Char()

    def first(self) -> None:
        self.with_context(a=1).later()
        by_thing = {thing: 1 for thing in self.env["t.thing"].search([])}
        for thing in self:
            by_thing[thing]
        self.env["t.thing"].concat(*self.filtered("name"))
"""
VIEW_LATER = """
from odoo import models

class Thing(models.Model):
    _inherit = "t.thing"

    def later(self) -> None:
        pass
"""


def test_self_s_records_are_the_addon_s_records_of_the_model(run_mypy: RunMypy) -> None:
    """``self`` is the defining class; what its ``-> Self`` methods give is the addon's view."""
    result = run_mypy(
        {
            "my_mod": {
                "__init__.py": "from . import first, later\n",
                "first.py": VIEW_FIRST,
                "later.py": VIEW_LATER,
            }
        }
    )

    assert result.messages("first.py") == []


APPENDED = """
from odoo import fields, models

class Thing(models.Model):
    _name = "t.thing"

    name = fields.Char()
    partner_ids = fields.Many2many("res.partner")

    def appended(self, ids: list[int]) -> None:
        rows = []
        rows.append({"name": self.name, "partner_ids": [(4, ids[0])]})
        rows.append({"name": "x", "partner_ids": [(6, 0, ids)]})
        rows += [{"name": "y"}]
        rows.append({"nmae": "z"})
        self.create(rows)
        names = []
        names.append(self.name)
        reveal_type(names)
"""


def test_a_list_given_dict_displays_is_not_typed_from_the_first(run_mypy: RunMypy) -> None:
    """``rows = []`` then ``rows.append({...})`` of dicts that differ: ``list[Any]``."""
    messages = run_mypy({"my_mod": {"models.py": APPENDED}}).messages("models.py")

    # The values of each dict are still checked where the list is created from.
    assert [message.split(";")[0].split("  [")[0] for message in messages] == [
        '15: error: Odoo model "t.thing" has no field "nmae"',
        '19: note: Revealed type is "list[str | Literal[False]]"',
    ], messages
