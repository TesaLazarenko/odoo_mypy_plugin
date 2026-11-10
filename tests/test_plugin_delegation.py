"""End-to-end tests of ``_inherits`` delegation (and ``Many2one(delegate=True)``).

Odoo adds the fields of a delegated parent model to the child model as related
fields (``_add_inherited_fields``); the parent's methods are not delegated.
"""

from __future__ import annotations

from conftest import RunMypy

T_BASE = """
from odoo import fields, models

class Parent(models.Model):
    _name = "t.parent"

    color = fields.Integer()
    size = fields.Char()

    def parent_method(self) -> None:
        pass

class Child(models.Model):
    _name = "t.child"
    _inherits = {"t.parent": "parent_id"}

    parent_id = fields.Many2one("t.parent", required=True, ondelete="cascade")
    size = fields.Integer()

class DelegatingMixin(models.AbstractModel):
    _name = "t.delegating.mixin"
    _inherits = {"t.parent": "parent_id"}

    parent_id = fields.Many2one("t.parent")
"""

# -- [28][7] _inherits -------------------------------------------------------------------------


def test_inherits_delegates_fields_but_not_methods(run_mypy: RunMypy) -> None:
    result = run_mypy(
        {
            "t_base": {"models.py": T_BASE},
            "t_use": {
                "depends": ["t_base"],
                "models.py": """
                from odoo import models

                class Child(models.Model):
                    _inherit = "t.child"

                    def action(self) -> None:
                        reveal_type(self.color)
                        reveal_type(self.size)
                        self.parent_method()

                class UsesMixin(models.Model):
                    _name = "t.uses"
                    _inherit = ["t.delegating.mixin"]

                    def action(self) -> None:
                        reveal_type(self.color)
                        reveal_type(self.env["res.users"].phone)
                """,
            },
        },
        check=["t_use"],
    )

    messages = result.messages("t_use/models.py")
    assert messages[:2] == [
        '8: note: Revealed type is "int"',
        '9: note: Revealed type is "int"',
    ]
    assert 'has no attribute "parent_method"' in messages[2]
    assert messages[3:] == [
        '17: note: Revealed type is "int"',
        '18: note: Revealed type is "str | Literal[False]"',
    ]


def test_delegated_fields_include_extensions_seen_by_the_reading_addon(
    run_mypy: RunMypy,
) -> None:
    result = run_mypy(
        {
            "t_base": {"models.py": T_BASE},
            "t_ext": {
                "depends": ["t_base"],
                "models.py": """
                from odoo import fields, models

                class Parent(models.Model):
                    _inherit = "t.parent"

                    shade = fields.Char()

                class Holder(models.Model):
                    _name = "t.holder"

                    record_id = fields.Many2one("t.parent", delegate=True)
                    child_id = fields.Many2one("t.child")

                    def action(self) -> None:
                        reveal_type(self.shade)
                        reveal_type(self.child_id.shade)
                        reveal_type(self.env["t.child"].shade)
                """,
            },
        },
        check=["t_ext"],
    )

    assert result.messages("t_ext/models.py") == [
        '16: note: Revealed type is "str | Literal[False]"',
        '17: note: Revealed type is "str | Literal[False]"',
        '18: note: Revealed type is "str | Literal[False]"',
    ]
