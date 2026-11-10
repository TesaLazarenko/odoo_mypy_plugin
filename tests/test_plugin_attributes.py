"""End-to-end tests of class attributes once ``_inherit`` parents are real bases.

Odoo model classes override private configuration attributes (``_rec_name``,
``_sequence_index``...) freely, also with properties, and mixins may be generic.
"""

from __future__ import annotations

from conftest import RunMypy

# -- [37] meta attributes keep BaseModel's declared type --------------------------------------


def test_meta_attributes_rec_name_and_same_file_list_inherit_do_not_clash(
    run_mypy: RunMypy,
) -> None:
    result = run_mypy(
        {
            "my_mod": {
                "models.py": """
                from odoo import models

                class A(models.Model):
                    _name = "my.a"
                    _inherit = ["avatar.mixin"]
                    _order = "id desc"
                    _rec_name = "display_name"

                class B(models.Model):
                    _inherit = "my.a"
                    _order = ("id")
                    _rec_name = None

                    def action(self) -> None:
                        reveal_type(self._inherit)
                        reveal_type(self._order)
                        reveal_type(self._rec_name)

                class Standalone(models.Model):
                    _name = "my.standalone"
                    _rec_name = "code"
                """
            }
        }
    )

    assert result.messages("models.py") == [
        '16: note: Revealed type is "str | list[str] | tuple[str, ...]"',
        '17: note: Revealed type is "Any"',
        '18: note: Revealed type is "Any"',
    ]


# -- [32] private attributes of injected mixins -----------------------------------------------


def test_mixin_private_attributes_may_be_overridden_with_other_types(run_mypy: RunMypy) -> None:
    # sequence.mixin: `_sequence_index = False`, regex attributes overridden by properties
    # in account.move; rating.parent.mixin: `_rating_satisfaction_days = False`.
    result = run_mypy(
        {
            "t_seq": {
                "models.py": """
                from odoo import fields, models

                class SequenceMixin(models.AbstractModel):
                    _name = "t.sequence.mixin"
                    _sequence_index = False
                    _prefix = "x"
                    _sequence_regex = fr"^{_prefix}(?P<seq>\\d*)$"
                    _private_field = fields.Integer()
                    _days = False
                """
            },
            "t_acc": {
                "depends": ["t_seq"],
                "models.py": """
                from odoo import models

                class Move(models.Model):
                    _name = "t.move"
                    _inherit = ["t.sequence.mixin"]
                    _sequence_index = "journal_id"
                    _days = 30

                    @property
                    def _sequence_regex(self) -> str:
                        return ""

                    def action(self) -> None:
                        reveal_type(self._sequence_index)
                        reveal_type(self._private_field)
                """,
            },
        },
        check=["t_acc"],
    )

    assert result.messages("t_acc/models.py") == [
        '15: note: Revealed type is "Any"',
        '16: note: Revealed type is "int"',
    ]


def test_private_attribute_declared_with_annotation_keeps_its_type(run_mypy: RunMypy) -> None:
    result = run_mypy(
        {
            "my_mod": {
                "models.py": """
                from odoo import models

                class Mixin(models.AbstractModel):
                    _name = "my.mixin"
                    _limit: int = 10

                class Thing(models.Model):
                    _name = "my.thing"
                    _inherit = ["my.mixin"]
                    _limit = "many"
                """
            }
        }
    )

    assert result.messages("models.py") == [
        '11: error: Incompatible types in assignment (expression has type "str", '
        'variable has type "int")  [assignment]'
    ]


# -- [16] generic mixins ----------------------------------------------------------------------


def test_generic_mixin_type_variables_become_any(run_mypy: RunMypy) -> None:
    result = run_mypy(
        {
            "my_mod": {
                "models.py": """
                from typing import Generic, TypeVar

                from odoo import models

                T = TypeVar("T")

                class GMixin(models.AbstractModel, Generic[T]):
                    _name = "x.gmixin"

                    def put(self, value: T) -> list[T]:
                        return [value]

                class Child(models.Model):
                    _name = "x.child"
                    _inherit = ["x.gmixin"]

                    def action(self) -> None:
                        reveal_type(self.put(1))
                """
            }
        }
    )

    assert result.messages("models.py") == ['19: note: Revealed type is "list[Any]"']
