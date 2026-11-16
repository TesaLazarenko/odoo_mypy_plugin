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


PASSED_BASE = """
from odoo import models

class Mixin(models.AbstractModel):
    _name = "t.mixin"

    def _execute(self, trackings: dict[int, tuple[set[str], list[str]]]) -> None:
        self._log(trackings)

    def _log(self, trackings: dict[int, tuple[set[str], list[str]]]) -> None:
        pass

    def _finalize(self, records, changes: set[str]) -> None:
        kept = [name for name in changes if name]
        records._execute({record.id: (kept, []) for record in records})
"""

PASSED_USE = """
from odoo import models

class Thing(models.Model):
    _name = "t.thing"
    _inherit = ["t.mixin"]

    def _execute(self, trackings):
        mine = {}
        for index, record in enumerate(self):
            changes, values = trackings.get(record.id, (None, None))
            mine[index] = ([name for name in changes if name != "x"], values)
        super()._execute(mine)
        self._log(mine)
        super()._execute({1: ("ab", [])})

    def _own(self, names: set[str]) -> None:
        self._own([name for name in names])
"""

SILENT = """
[[tool.mypy.overrides]]
module = ["base_mod", "base_mod.*"]
follow_imports = "silent"
"""


def test_a_parameter_takes_the_containers_its_own_class_passes(run_mypy: RunMypy) -> None:
    """``_track_execute(trackings: dict[int, tuple[set[str], ...]])`` is given a list by Odoo."""
    addons = {
        "base_mod": {"models.py": PASSED_BASE},
        "my_mod": {"models.py": PASSED_USE, "depends": ["base_mod"]},
    }
    messages = run_mypy(addons, check=["my_mod"], options=SILENT).messages("my_mod/models.py")

    assert messages == [
        '15: error: Argument 1 to "_execute" of "Mixin" has incompatible type '
        '"dict[int, tuple[str, list[Never]]]"; expected "dict[int, tuple[set[str], list[str]]] '
        '| dict[int, tuple[list[str], list[str]]]"  [arg-type]',
        '18: error: Argument 1 to "_own" of "Thing" has incompatible type "list[str]"; '
        'expected "set[str]"  [arg-type]',
    ], messages


def test_the_annotation_of_checked_code_is_its_authors_word(run_mypy: RunMypy) -> None:
    """Where the errors of the class that wrote the annotation are shown, nothing is added."""
    addons = {
        "base_mod": {"models.py": PASSED_BASE},
        "my_mod": {"models.py": PASSED_USE, "depends": ["base_mod"]},
    }
    result = run_mypy(addons)

    found = result.messages("my_mod/models.py")
    assert [message.split(" has incompatible")[0] for message in found] == [
        '13: error: Argument 1 to "_execute" of "Mixin"',
        '14: error: Argument 1 to "_log" of "Mixin"',
        "15: error: Dict entry 0",
        '18: error: Argument 1 to "_own" of "Thing"',
    ], result.lines
