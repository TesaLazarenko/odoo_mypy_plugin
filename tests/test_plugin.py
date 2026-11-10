"""End-to-end tests: real mypy + plugin on small addons against the installed Odoo."""

from __future__ import annotations

from pathlib import Path

from conftest import RunMypy

PARTNER = "odoo.addons.base.models.res_partner.Partner"


def test_env_getitem_known_model_reveals_model_class(run_mypy: RunMypy) -> None:
    result = run_mypy(
        {
            "my_mod": {
                "models.py": """
                from odoo import models

                class Thing(models.Model):
                    _name = "my.thing"

                    def action(self) -> None:
                        reveal_type(self.env["res.partner"])
                        reveal_type(self.env["my.thing"])
                """
            }
        }
    )
    assert result.messages("models.py") == [
        f'8: note: Revealed type is "{PARTNER}"',
        '9: note: Revealed type is "my_mod.models.Thing"',
    ]


def test_env_getitem_unknown_model_reports_error(run_mypy: RunMypy) -> None:
    result = run_mypy(
        {
            "my_mod": {
                "models.py": """
                from odoo import models

                class Thing(models.Model):
                    _name = "my.thing"

                    def action(self) -> None:
                        self.env["res.partnr"]
                """
            }
        }
    )
    assert result.messages("models.py") == [
        '8: error: Unknown Odoo model "res.partnr"  [odoo-model]'
    ]


def test_inherit_exposes_fields_of_earlier_classes(run_mypy: RunMypy) -> None:
    result = run_mypy(
        {
            "my_mod": {
                "models.py": """
                from odoo import fields, models

                class ResPartner(models.Model):
                    _inherit = "res.partner"

                    x_score = fields.Integer()

                    def action(self) -> None:
                        reveal_type(self.name)
                        reveal_type(self.x_score)
                        self.x_score + self.name
                """
            }
        }
    )
    assert result.messages("models.py") == [
        '10: note: Revealed type is "str | Literal[False]"',
        '11: note: Revealed type is "int"',
        '12: error: Unsupported operand types for + ("int" and "str")  [operator]',
        '12: note: Right operand is of type "str | Literal[False]"',
    ]


def test_inherit_string_does_not_clash_with_list_in_base(run_mypy: RunMypy) -> None:
    # base's Partner uses `_inherit = [...]`; a plain string is just as valid.
    result = run_mypy(
        {
            "my_mod": {
                "models.py": """
                from odoo import models

                class ResPartner(models.Model):
                    _inherit = "res.partner"
                    _order = "name desc"
                """
            }
        }
    )
    assert result.messages("models.py") == []


def test_env_getitem_includes_extensions_from_current_addon(run_mypy: RunMypy) -> None:
    result = run_mypy(
        {
            "my_mod": {
                "models.py": """
                from odoo import fields, models

                class ResPartner(models.Model):
                    _inherit = "res.partner"

                    x_score = fields.Integer()

                class Thing(models.Model):
                    _name = "my.thing"

                    def action(self) -> None:
                        reveal_type(self.env["res.partner"].x_score)
                """
            }
        }
    )
    assert result.messages("models.py") == ['13: note: Revealed type is "int"']


def test_relational_fields_are_bound_to_comodel(run_mypy: RunMypy) -> None:
    result = run_mypy(
        {
            "my_mod": {
                "models.py": """
                from odoo import fields, models

                class Thing(models.Model):
                    _name = "my.thing"

                    partner_id = fields.Many2one("res.partner")
                    line_ids = fields.One2many("my.line", "thing_id")
                    tag_ids = fields.Many2many(comodel_name="res.partner.category")

                    def action(self) -> None:
                        reveal_type(self.partner_id)
                        reveal_type(self.partner_id.name)
                        reveal_type(self.line_ids)
                        reveal_type(self.tag_ids)
                        self.line_ids.qty.upper()

                class Line(models.Model):
                    _name = "my.line"

                    thing_id = fields.Many2one("my.thing")
                    qty = fields.Integer()
                """
            }
        }
    )
    assert result.messages("models.py") == [
        f'12: note: Revealed type is "{PARTNER}"',
        '13: note: Revealed type is "str | Literal[False]"',
        '14: note: Revealed type is "my_mod.models.Line"',
        '15: note: Revealed type is "odoo.addons.base.models.res_partner.PartnerCategory"',
        '16: error: "int" has no attribute "upper"  [attr-defined]',
    ]


def test_relational_field_unknown_comodel_reports_error(run_mypy: RunMypy) -> None:
    result = run_mypy(
        {
            "my_mod": {
                "models.py": """
                from odoo import fields, models

                class Thing(models.Model):
                    _name = "my.thing"

                    partner_id = fields.Many2one("res.partnr")
                """
            }
        }
    )
    assert result.messages("models.py") == [
        '7: error: Unknown Odoo model "res.partnr"  [odoo-model]'
    ]


def test_models_outside_dependencies_are_unknown(run_mypy: RunMypy) -> None:
    result = run_mypy(
        {
            "other_mod": {
                "models.py": """
                from odoo import models

                class Other(models.Model):
                    _name = "other.model"
                """
            },
            "my_mod": {
                "models.py": """
                from odoo import models

                class Thing(models.Model):
                    _name = "my.thing"

                    def action(self) -> None:
                        self.env["other.model"]
                """
            },
        },
        check=["my_mod"],
    )
    assert result.messages("models.py") == [
        '8: error: Unknown Odoo model "other.model"  [odoo-model]'
    ]


def test_inherit_sees_fields_from_dependency_addon(run_mypy: RunMypy) -> None:
    result = run_mypy(
        {
            "dep_mod": {
                "models.py": """
                from odoo import fields, models

                class ResPartner(models.Model):
                    _inherit = "res.partner"

                    x_level = fields.Integer()
                """
            },
            "my_mod": {
                "depends": ["dep_mod"],
                "models.py": """
                from odoo import models

                class ResPartner(models.Model):
                    _inherit = "res.partner"

                    def action(self) -> None:
                        reveal_type(self.x_level)
                        reveal_type(self.name)
                """,
            },
        },
        check=["my_mod"],
    )
    assert result.messages("my_mod/models.py") == [
        '8: note: Revealed type is "int"',
        '9: note: Revealed type is "str | Literal[False]"',
    ]


def test_probe_fixture_reports_type_errors(run_mypy: RunMypy) -> None:
    source = Path(__file__).parent / "fixtures/addons/probe_mod/models/partner.py"
    result = run_mypy({"probe_mod": {"partner.py": source.read_text()}})
    lines = {int(message.split(":", 1)[0]) for message in result.messages("partner.py")}
    # comodel typo, int + str|False, unknown env model
    assert {8, 15, 18} <= lines
