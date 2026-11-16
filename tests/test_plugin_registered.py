"""Records are the abstract classes Odoo registers ``BaseModel`` with (``Sequence``, ``Set``)."""

from __future__ import annotations

from conftest import RunMypy

SAMPLES = """
import random
from collections.abc import Sequence

from odoo import fields, models

class Thing(models.Model):
    _name = "t.thing"

    name = fields.Char()

    def samples(self) -> None:
        picked = random.sample(self.search([]), 2)
        reveal_type(picked)
        picked[0].missing
        first(self)
        random.sample({1, 2}, 1)
        first(self.env["t.other"])

class Other(models.Model):
    _name = "t.other"

def first(items: Sequence[Thing]) -> None:
    pass
"""


def test_records_are_a_sequence_where_a_parameter_takes_one(run_mypy: RunMypy) -> None:
    """``collections.abc.Sequence.register(BaseModel)``: ``random.sample(records, 2)``."""
    messages = run_mypy({"my_mod": {"models.py": SAMPLES}}).messages("models.py")

    assert [message.split("  [")[0] for message in messages] == [
        '14: note: Revealed type is "list[my_mod.models.Thing]"',
        '15: error: "Thing" has no attribute "missing"',
        '17: error: Argument 1 has incompatible type "set[int]"; expected "Sequence[Never]"',
        '18: error: Argument 1 to "first" has incompatible type "Other"; '
        'expected "Sequence[Thing]"',
    ], messages
