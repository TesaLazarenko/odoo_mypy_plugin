"""``max(values, default=False)``: the result may be ``False``, not any ``bool``."""

from __future__ import annotations

from conftest import RunMypy

MODEL = """
from datetime import datetime
from typing import Literal
from odoo import fields, models

class Move(models.Model):
    _name = "t.move"

    date = fields.Datetime(required=True)
    done = fields.Boolean()
    deadline = fields.Datetime()
    origin_ids = fields.Many2many("t.move", "t_move_rel", "a", "b")

    def late(self) -> bool:
        latest = max(self.origin_ids.mapped("date"), default=False)
        reveal_type(latest)
        if latest and latest > self.date:
            return True
        first = min(self.origin_ids.mapped("date"), default=None)
        reveal_type(first)
        return bool(first)

    def flags(self, fallback: bool) -> None:
        reveal_type(max(self.origin_ids.mapped("done"), default=False))
        reveal_type(max(self.origin_ids.mapped("date"), default=True))
        reveal_type(max(self.origin_ids.mapped("date"), default=fallback))
        reveal_type(max(1, 2))
        mixed: list[float | bool] = [1.5, True]
        reveal_type(max(mixed, default=False))
        reveal_type(max(["a", "bc"], key=len, default=False))
        reveal_type(max(self.origin_ids.mapped("deadline"), default=False))
        done: list[float | Literal[True]] = [1.5, True]
        reveal_type(max(done, default=False))
"""


def test_default_false_stays_false(run_mypy: RunMypy) -> None:
    result = run_mypy({"t_mod": {"depends": ["base"], "models.py": MODEL}})

    assert result.messages("models.py") == [
        '16: note: Revealed type is "datetime.datetime | Literal[False]"',
        '20: note: Revealed type is "datetime.datetime | None"',
        '24: note: Revealed type is "bool"',  # the items are booleans
        '25: note: Revealed type is "datetime.datetime | bool"',  # only ``False`` is kept
        '26: note: Revealed type is "datetime.datetime | bool"',  # any bool
        '27: note: Revealed type is "int"',
        '29: note: Revealed type is "float | bool"',  # an item may be True
        '30: note: Revealed type is "str | Literal[False]"',
        '31: note: Revealed type is "datetime.datetime | Literal[False]"',  # an empty field
        '33: note: Revealed type is "float | bool"',
    ]
