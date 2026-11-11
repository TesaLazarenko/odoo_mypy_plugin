"""End-to-end tests: real mypy + plugin on small addons against the installed Odoo."""

from __future__ import annotations

import textwrap
from pathlib import Path

from conftest import OdooNames, RunMypy
from pytest_mock import MockerFixture
from test_harness import ENV_LOOKUP

from odoo_typing import registry as registry_module


def test_env_getitem_known_model_reveals_model_class(run_mypy: RunMypy, names: OdooNames) -> None:
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
        f'8: note: Revealed type is "{names.partner}"',
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


def test_relational_fields_are_bound_to_comodel(run_mypy: RunMypy, names: OdooNames) -> None:
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
        f'12: note: Revealed type is "{names.partner}"',
        '13: note: Revealed type is "str | Literal[False]"',
        '14: note: Revealed type is "my_mod.models.Line"',
        f'15: note: Revealed type is "{names.partner_category}"',
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


OTHER_MOD = """
from odoo import models

class Other(models.Model):
    _name = "other.model"
"""

OUTSIDE_USER = """
from odoo import fields, models

class Thing(models.Model):
    _name = "my.thing"

    other_id = fields.Many2one("other.model")

    def action(self) -> None:
        reveal_type(self.env["other.model"])
        self.env["other.modl"]
        self.env["other.model"]  # type: ignore[odoo-model]
"""


def test_models_outside_dependencies_report_depends_code(run_mypy: RunMypy) -> None:
    result = run_mypy(
        {"other_mod": {"models.py": OTHER_MOD}, "my_mod": {"models.py": OUTSIDE_USER}},
        check=["my_mod"],
    )

    outside = (
        'Odoo model "other.model" is defined in addon "other_mod", '
        'which is not in the dependencies of "my_mod"  [odoo-model-depends]'
    )
    assert result.messages("my_mod/models.py") == [
        f"7: error: {outside}",
        f"10: error: {outside}",
        '10: note: Revealed type is "Any"',
        '11: error: Unknown Odoo model "other.modl"  [odoo-model]',
    ]


def test_models_outside_dependencies_depends_code_can_be_disabled(run_mypy: RunMypy) -> None:
    result = run_mypy(
        {"other_mod": {"models.py": OTHER_MOD}, "my_mod": {"models.py": OUTSIDE_USER}},
        check=["my_mod"],
        flags=["--disable-error-code", "odoo-model-depends"],
    )

    assert result.messages("my_mod/models.py") == [
        '10: note: Revealed type is "Any"',
        '11: error: Unknown Odoo model "other.modl"  [odoo-model]',
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
    assert result.messages("partner.py") == [
        '8: error: Unknown Odoo model "res.partnr"  [odoo-model]',
        '15: error: Unsupported operand types for + ("int" and "str")  [operator]',
        '15: note: Right operand is of type "str | Literal[False]"',
        '18: error: Unknown Odoo model "res.partnr"  [odoo-model]',
    ]


SCRIPT = """
from odoo.api import Environment

def run(env: Environment) -> None:
    reveal_type(env["res.partner"])
    reveal_type(env["no.such.model"])
"""


def test_env_getitem_outside_addon_keeps_declared_type_unchecked(
    run_mypy: RunMypy, names: OdooNames
) -> None:
    # Files outside any addon have no dependency view: nothing is resolved or checked there.
    result = run_mypy({"my_mod": {"models.py": ""}}, scripts={"tool.py": SCRIPT})

    assert result.messages("tool.py") == [
        f'5: note: Revealed type is "{names.base_model}"',
        f'6: note: Revealed type is "{names.base_model}"',
    ]


def test_cold_run_scans_only_addons_of_loaded_files(
    run_mypy: RunMypy, mocker: MockerFixture, odoo_version: int
) -> None:
    # Odoo core files (odoo/http.py, odoo/tests/common.py...) use env[...] too; they
    # must not make the plugin scan every installed addon.
    scan = mocker.spy(registry_module, "scan_addon")
    # Odoo 20's odoo/tests/common.py imports odoo.addons.bus.tools: bus (and web, its
    # dependency) are addons of loaded files there.
    core = {"bus", "web"} if odoo_version >= 20 else set()

    run_mypy(ENV_LOOKUP, scripts={"tool.py": SCRIPT}, fresh_cache=True)

    assert {call.args[0].name for call in scan.call_args_list} == {"base", "my_mod"} | core


def test_inherit_from_utf8_bom_file_exposes_fields(run_mypy: RunMypy) -> None:
    thing = textwrap.dedent(
        """
        from odoo import fields, models

        class Thing(models.Model):
            _name = "x.thing"
            score = fields.Integer()
        """
    )
    result = run_mypy(
        {
            "dep_mod": {"thing.py": b"\xef\xbb\xbf" + thing.encode()},
            "my_mod": {
                "depends": ["dep_mod"],
                "user.py": """
                from odoo import models

                class ThingExt(models.Model):
                    _inherit = "x.thing"

                    def action(self) -> None:
                        reveal_type(self.score)
                """,
            },
        },
        check=["my_mod"],
    )

    assert result.messages("user.py") == ['8: note: Revealed type is "int"']
