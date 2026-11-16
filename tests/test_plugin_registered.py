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


DISPATCH = """
from odoo import models

class View(models.Model):
    _name = "t.view"

class Template(models.Model):
    _name = "t.template"

class Other(models.Model):
    _name = "t.other"

class Thread(models.Model):
    _name = "t.thread"

    def post_with_source(self, source_ref: str | View) -> None:
        self._source_from_ref(source_ref)

    def _source_from_ref(self, source_ref):
        if source_ref._name == "t.template":
            return source_ref
        return False

    def use(self) -> None:
        self.post_with_source(self.env["t.template"])
        self.post_with_source(self.env["t.view"])
        self.post_with_source(self.env["t.other"])
"""


def test_a_parameter_takes_records_of_the_models_its_method_tests(run_mypy: RunMypy) -> None:
    """``message_post_with_source(source_ref: str | IrUiView)`` takes a ``mail.template``."""
    messages = run_mypy({"my_mod": {"models.py": DISPATCH}}).messages("models.py")

    assert [message.split(";")[0] for message in messages] == [
        '27: error: Argument 1 to "post_with_source" of "Thread" has incompatible type "Other"',
    ], messages
