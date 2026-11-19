"""``parent_path`` of a model with ``_parent_store``: Odoo fills it for every saved record.

``create`` and ``write`` compute the path (``_parent_store_create``,
``_parent_store_update``), so it reads like a required field, as ``create_date`` does.
"""

from __future__ import annotations

from conftest import RunMypy

MODEL = """
from odoo import fields, models

class Place(models.Model):
    _name = "t.place"
{attrs}
    parent_id = fields.Many2one("t.place")
    parent_path = fields.Char(index=True)

    def ancestors(self) -> list[int]:
        reveal_type(self.parent_path)
        return [int(item) for item in self.parent_path.split("/")[:-1]]
"""


def test_parent_path_of_parent_store_model_is_a_string(run_mypy: RunMypy) -> None:
    addons = {"my_mod": {"models.py": MODEL.format(attrs="    _parent_store = True")}}

    assert run_mypy(addons).messages("models.py") == ['11: note: Revealed type is "str"']
    declared = run_mypy(addons, options="required_values = false\n").messages("models.py")
    assert declared[0] == '11: note: Revealed type is "str | Literal[False]"'
    assert len(declared) == 2


def test_parent_path_without_parent_store_keeps_false(run_mypy: RunMypy) -> None:
    """The field alone is an ordinary ``Char``: nothing fills it."""
    for attrs in ("", "    _parent_store = False"):
        addons = {"my_mod": {"models.py": MODEL.format(attrs=attrs)}}

        messages = run_mypy(addons).messages("models.py")

        assert 'Revealed type is "str | Literal[False]"' in messages[0]
        assert 'has no attribute "split"' in messages[1]


EXTENSION = """
from odoo import models

class Place(models.Model):
    _inherit = "t.place"
{attrs}
    def depth(self) -> int:
        return self.parent_path.count("/")
"""


def test_parent_store_turned_on_by_a_later_class_of_the_same_addon(run_mypy: RunMypy) -> None:
    """The registry class takes ``_parent_store`` from its most derived class that sets it."""
    files = {
        "models.py": MODEL.format(attrs=""),
        "more.py": EXTENSION.format(attrs="    _parent_store = True\n"),
    }

    result = run_mypy({"my_mod": files})

    assert result.messages("more.py") == []
    assert result.messages("models.py") == ['11: note: Revealed type is "str"']


def test_parent_store_turned_on_by_a_later_addon_leaves_old_rows_empty(run_mypy: RunMypy) -> None:
    """Odoo computes the paths only when it adds the column: the rows of before keep none."""
    addons = {
        "my_mod": {"models.py": MODEL.format(attrs="")},
        "my_ext": {
            "depends": ["my_mod"],
            "models.py": EXTENSION.format(attrs="    _parent_store = True\n"),
        },
    }

    messages = run_mypy(addons).messages("my_ext/models.py")

    assert len(messages) == 1
    assert 'has no attribute "count"' in messages[0]


def test_parent_store_of_the_model_holds_for_a_later_addon(run_mypy: RunMypy) -> None:
    addons = {
        "my_mod": {"models.py": MODEL.format(attrs="    _parent_store = True")},
        "my_ext": {"depends": ["my_mod"], "models.py": EXTENSION.format(attrs="")},
    }

    assert run_mypy(addons).messages("my_ext/models.py") == []


def test_parent_store_said_again_by_a_later_addon_changes_nothing(run_mypy: RunMypy) -> None:
    """The model had ``_parent_store`` when Odoo added the column: every row has its path."""
    addons = {
        "my_mod": {"models.py": MODEL.format(attrs="    _parent_store = True")},
        "my_ext": {
            "depends": ["my_mod"],
            "models.py": EXTENSION.format(attrs="    _parent_store = True\n"),
        },
    }

    assert run_mypy(addons).messages("my_ext/models.py") == []


def test_parent_store_turned_off_and_on_again_leaves_old_rows_empty(run_mypy: RunMypy) -> None:
    addons = {
        "my_mod": {"models.py": MODEL.format(attrs="    _parent_store = True")},
        "my_off": {
            "depends": ["my_mod"],
            "models.py": EXTENSION.format(attrs="    _parent_store = False\n"),
        },
        "my_ext": {
            "depends": ["my_off"],
            "models.py": EXTENSION.format(attrs="    _parent_store = True\n"),
        },
    }

    messages = run_mypy(addons).messages("my_ext/models.py")

    assert len(messages) == 1
    assert 'has no attribute "count"' in messages[0]


def test_parent_path_in_a_comprehension_and_through_a_many2one(run_mypy: RunMypy) -> None:
    addons = {
        "my_mod": {
            "models.py": MODEL.format(attrs="    _parent_store = True")
            + """
class Item(models.Model):
    _name = "t.item"

    place_id = fields.Many2one("t.place")

    def inside(self, other: Place) -> bool:
        return self.place_id.parent_path.startswith(other.parent_path)
"""
        }
    }

    assert run_mypy(addons).messages("models.py") == ['11: note: Revealed type is "str"']
