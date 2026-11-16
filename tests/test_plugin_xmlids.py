"""End-to-end tests of ``env.ref("module.name")`` (roadmap item 4.11)."""

from __future__ import annotations

import pytest
from conftest import OdooNames, RunMypy

T_BASE = {
    "models.py": """
    from odoo import fields, models

    class Thing(models.Model):
        _name = "t.thing"

        name = fields.Char()
    """,
    "data": {
        "data/things.xml": """
        <odoo>
            <record id="thing_one" model="t.thing"><field name="name">One</field></record>
            <record id="action_things" model="ir.actions.act_window">
                <field name="res_model">t.thing</field>
            </record>
            <function model="ir.model.data" name="_update_xmlids">
                <value eval="[{'xml_id': 't_base.made_by_code', 'record': obj().browse(1)}]"/>
            </function>
        </odoo>
        """,
    },
}
T_OTHER = {
    "data": {"data.xml": '<odoo><record id="other_thing" model="t.thing"/></odoo>'},
    "depends": ["t_base"],
}

USE = """
from odoo import models

class Thing(models.Model):
    _inherit = "t.thing"

    def check(self) -> None:
        reveal_type(self.env.ref("t_base.thing_one").name)
        reveal_type(self.env.ref("t_base.thing_one", raise_if_not_found=False))
        reveal_type(self.env.ref("t_base.model_t_thing"))
        self.env.ref("t_base.thing_onee")
        self.env.ref("t_other.other_thing")
        self.env.ref("thing_one")
        self.env.ref("t_base.gone", raise_if_not_found=False)
        self.env.ref("t_use.own_view")
        if self.env.ref("t_other.other_thing", raise_if_not_found=False):
            self.env.ref("t_other.second")
        self.env.ref("local_es.made_by_code")
        reveal_type(self.env.ref("t_base.made_by_code"))
        self.env.user.has_group("t_base.made_by_code")
"""


def test_env_ref_types_and_checks_xml_ids(run_mypy: RunMypy, names: OdooNames) -> None:
    result = run_mypy(
        {
            "t_base": T_BASE,
            "t_other": T_OTHER,
            "t_use": {
                "depends": ["t_base"],
                "models.py": USE,
                "data": {"views.xml": '<odoo><template id="own_view"/></odoo>'},
            },
        },
        check=["t_use"],
    )
    messages = result.messages("t_use/models.py")

    assert messages[0] == '8: note: Revealed type is "str | Literal[False]"'
    assert messages[1].startswith('9: note: Revealed type is "t_use.models.Thing | None')
    assert messages[2].startswith('10: note: Revealed type is "odoo.addons.base.models.ir_model')
    assert messages[3:] == [
        '11: error: Unknown XML id "t_base.thing_onee"; did you mean "t_base.thing_one"?  '
        "[odoo-xmlid]",
        '12: error: XML id "t_other.other_thing" is created by addon "t_other", which is not '
        'in the dependencies of "t_use"  [odoo-xmlid-depends]',
        '13: error: XML id "thing_one" has no module ("module.name")  [odoo-xmlid]',
        # _update_xmlids does not say the model: records of no known model (Odoo 19+
        # declares ``BaseModel``, whose fields would all be unknown attributes).
        '19: note: Revealed type is "Any"',
    ]


@pytest.mark.odoo(19, 20)
def test_ref_of_an_id_the_data_does_not_create_is_no_model_at_all(run_mypy: RunMypy) -> None:
    """Odoo 19 declares ``BaseModel | None``: records of no model, whose fields are errors."""
    result = run_mypy(
        {
            "my_mod": {
                "models.py": """
                from odoo import models

                class Thing(models.Model):
                    _name = "t.thing"

                    def action(self) -> None:
                        record = self.env.ref("other_addon.record", raise_if_not_found=False)
                        if not record:
                            return
                        reveal_type(record)
                        record.name
                        reveal_type(self.env.ref(f"my_mod.{self.id}_thing"))
                        made = self.env.ref(f"my_mod.{self.id}_thing", raise_if_not_found=False)
                        reveal_type(made)
                """
            }
        }
    )

    assert result.messages("models.py") == [
        '11: note: Revealed type is "Any"',
        '13: note: Revealed type is "Any"',  # an id made at run time
        '15: note: Revealed type is "Any | None"',
    ]


@pytest.mark.odoo(19, 20)
def test_ref_of_no_known_model_keeps_odoo_s_type_when_asked_to(run_mypy: RunMypy) -> None:
    result = run_mypy(
        {
            "my_mod": {
                "models.py": """
                from odoo import models

                class Thing(models.Model):
                    _name = "t.thing"

                    def action(self) -> None:
                        reveal_type(self.env.ref(f"my_mod.{self.id}_thing"))
                """
            }
        },
        options='dynamic_env = "base"\n',
    )

    (message,) = result.messages("models.py")
    assert message.startswith('8: note: Revealed type is "odoo.orm.models.BaseModel')


@pytest.mark.odoo(19, 20)
def test_command_set_takes_the_xml_ids_of_data_to_load(run_mypy: RunMypy) -> None:
    """The chart template loader replaces XML ids in commands; Odoo 19 declares ints."""
    result = run_mypy(
        {
            "t_base": T_BASE,
            "t_use": {
                "depends": ["t_base"],
                "data": {"data.xml": '<odoo><record id="own" model="t.thing"/></odoo>'},
                "models.py": """
                from odoo import Command, models

                class Thing(models.Model):
                    _inherit = "t.thing"

                    def data(self) -> list:
                        return [
                            Command.set(["t_base.thing_one", "own", self.id]),
                            Command.set(["t_base.no_such_id"]),
                        ]
                """,
            },
        },
        check=["t_use"],
    )

    (message,) = result.messages("t_use/models.py")
    assert message.startswith('10: error: List item 0 has incompatible type "str"')
