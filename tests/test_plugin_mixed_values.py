"""Dicts and lists of mixed values: mypy's join of the values is not their type."""

from __future__ import annotations

from conftest import RunMypy

MODELS = """
from typing import Any

from odoo import fields, models

class Thing(models.Model):
    _name = "t.thing"

    name = fields.Char()
    amount = fields.Float()

    def mixed(self) -> None:
        values = {"thing": self, "amount": 0.0, "rows": [], "name": self.name}
        values["rows"].append(1)
        values["amount"] += self.amount
        values["thing"].name
        values["later"] = "x"
        values.get("rows").append(2)
        for key, value in values.items():
            value.anything
        reveal_type(values)

    def nested(self) -> None:
        batches = [[self, [], True], [self, [], False]]
        batches[0][1].append({})
        for things, rows, flag in batches:
            things.name
            rows.append(1)
        by_name = {"a": {"x": self, "y": 1}}
        for name, data in by_name.items():
            for key, value in data.items():
                value.anything

    def uniform(self) -> None:
        names = {"a": "x", "b": "y"}
        names["a"].missing
        names["c"] = 1
        things = {"a": self, "b": self}
        things["a"].missing
        amounts = {"a": 1, "b": 1.5}
        amounts["a"].missing

    def declared(self, values: dict[str, object]) -> None:
        values["a"].missing
        own: dict[str, object] = {"a": self, "b": 1}
        own["a"].missing
        for value in own.values():
            value.missing
"""


def test_values_of_a_dict_of_mixed_values_are_not_their_join(run_mypy: RunMypy) -> None:
    result = run_mypy({"my_mod": {"models.py": MODELS}})

    messages = result.messages("models.py")
    assert messages[0] == '21: note: Revealed type is "dict[str, object]"'
    assert messages[1:] == [
        '36: error: "str" has no attribute "missing"  [attr-defined]',
        '37: error: Incompatible types in assignment (expression has type "int", target has '
        'type "str")  [assignment]',
        '39: error: "Thing" has no attribute "missing"  [attr-defined]',
        '41: error: "float" has no attribute "missing"  [attr-defined]',
        '44: error: "object" has no attribute "missing"  [attr-defined]',
        '46: error: "object" has no attribute "missing"  [attr-defined]',
        '48: error: "object" has no attribute "missing"  [attr-defined]',
    ]


UNION = """
from odoo import fields, models

class Thing(models.Model):
    _name = "t.thing"

    name = fields.Char()

    def joined_to_a_union(self) -> None:
        values = {"id": self.id, "name": self.name, "rows": [0]}
        values["rows"].append(1)
        values["rows"] = "x"
        values["count"] = 1.5
        values.setdefault("more", []).append(2)
        same = {"a": self.id, "b": self.id}
        same["a"].missing

    def displays_read_in_place(self) -> None:
        first = {"a": {"n": self, "m": 1}}
        second = {"b": {"n": "s"}}
        for both in [first, second]:
            for key, data in both.items():
                data.items()
"""


def test_a_local_dict_of_mixed_values_takes_and_gives_any_value(run_mypy: RunMypy) -> None:
    """``record.id`` is a union on Odoo 17 and 18: mypy joins the values into a union."""
    result = run_mypy({"my_mod": {"models.py": UNION}})

    # Values of one type keep it (``int | NewId`` on 17 and 18; an untyped id on 19+).
    messages = result.messages("models.py")
    assert all(message.startswith("16: error: Item ") for message in messages), messages
