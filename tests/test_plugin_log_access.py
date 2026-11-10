"""End-to-end tests of the log-access fields Odoo adds to models at run time (roadmap 4.1).

``create_uid``, ``create_date``, ``write_uid`` and ``write_date`` are not declared in any
class; Odoo adds them when ``_log_access`` is on (by default, it follows ``_auto``).
"""

from __future__ import annotations

from conftest import RunMypy


def test_log_access_fields_have_odoo_types(run_mypy: RunMypy) -> None:
    result = run_mypy(
        {
            "my_mod": {
                "models.py": """
                from odoo import fields, models

                class Users(models.Model):
                    _inherit = "res.users"

                    x_level = fields.Integer()

                class Thing(models.Model):
                    _name = "t.thing"

                    def action(self) -> None:
                        reveal_type(self.create_date)
                        reveal_type(self.write_date)
                        reveal_type(self.create_uid.x_level)
                        reveal_type(self.write_uid.login)
                        self.create_uid.x_levell
                """
            }
        }
    )

    messages = result.messages("models.py")
    assert messages[:4] == [
        '13: note: Revealed type is "datetime.datetime | Literal[False]"',
        '14: note: Revealed type is "datetime.datetime | Literal[False]"',
        '15: note: Revealed type is "int"',
        '16: note: Revealed type is "str | Literal[False]"',
    ]
    assert len(messages) == 5
    assert 'has no attribute "x_levell"' in messages[4]


def test_log_access_fields_on_abstract_mixin_and_transient(run_mypy: RunMypy) -> None:
    result = run_mypy(
        {
            "my_mod": {
                "models.py": """
                from odoo import models

                class Mixin(models.AbstractModel):
                    _name = "t.mixin"

                    def stamp(self) -> None:
                        reveal_type(self.create_date)

                class Wizard(models.TransientModel):
                    _name = "t.wizard"

                    def stamp(self) -> None:
                        reveal_type(self.write_date)
                """
            }
        }
    )

    assert result.messages("models.py") == [
        '8: note: Revealed type is "datetime.datetime | Literal[False]"',
        '14: note: Revealed type is "datetime.datetime | Literal[False]"',
    ]


def test_log_access_disabled_or_not_auto_has_no_fields(run_mypy: RunMypy) -> None:
    result = run_mypy(
        {
            "my_mod": {
                "models.py": """
                from odoo import models

                class Report(models.Model):
                    _name = "t.report"
                    _auto = False

                    def read_it(self) -> None:
                        self.create_date

                class Log(models.Model):
                    _name = "t.log"
                    _log_access = False

                    def read_it(self) -> None:
                        self.write_uid

                class LogExtension(models.Model):
                    _inherit = "t.log"

                    def read_more(self) -> None:
                        self.create_uid
                """
            }
        }
    )

    assert result.messages("models.py") == [
        '9: error: "Report" has no attribute "create_date"  [attr-defined]',
        '16: error: "Log" has no attribute "write_uid"  [attr-defined]',
        '22: error: "LogExtension" has no attribute "create_uid"  [attr-defined]',
    ]


def test_log_access_field_declared_by_model_keeps_its_declaration(run_mypy: RunMypy) -> None:
    result = run_mypy(
        {
            "my_mod": {
                "models.py": """
                from odoo import fields, models

                class Thing(models.Model):
                    _name = "t.thing"

                    create_date = fields.Date()

                    def action(self) -> None:
                        reveal_type(self.create_date)
                """
            }
        }
    )

    assert result.messages("models.py") == [
        '10: note: Revealed type is "datetime.date | Literal[False]"',
    ]
