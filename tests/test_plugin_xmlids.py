"""End-to-end tests of ``env.ref("module.name")`` (roadmap item 4.11)."""

from __future__ import annotations

from conftest import RunMypy

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
"""


def test_env_ref_types_and_checks_xml_ids(run_mypy: RunMypy) -> None:
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
    ]
