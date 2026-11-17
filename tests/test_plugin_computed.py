"""Computed text fields whose compute methods always give a string (``odoo_typing.computed``)."""

from __future__ import annotations

from conftest import RunMypy

COMPUTED = """
import json
from json import dumps as to_json
from urllib.parse import urlparse

from odoo import fields, models


def label(value: object) -> str:
    return f"<{value}>"


def maybe(value: object) -> str | None:
    return None


class Wizard(models.TransientModel):
    _name = "t.wizard"

    action = fields.Selection([("a", "A"), ("b", "B")])
    parent_id = fields.Many2one("t.wizard", required=True)
    data = fields.Text(compute="_compute_data")
    preview = fields.Char(compute="_compute_preview")
    named = fields.Html(compute="_compute_named")
    parent_data = fields.Text(related="parent_id.data")
    empty = fields.Text(compute="_compute_empty")
    unknown = fields.Text(compute="_compute_unknown")
    stored = fields.Text(compute="_compute_data", store=True)
    written = fields.Text(compute="_compute_written", inverse="_inverse_written")
    editable = fields.Text(compute="_compute_written", readonly=False)
    local = fields.Char(compute="_compute_local")
    reset = fields.Char(compute="_compute_reset")
    spread = fields.Char(compute="_compute_spread")
    unassigned = fields.Char(compute="_compute_unassigned")
    count = fields.Integer(compute="_compute_data")

    def _compute_data(self) -> None:
        for record in self:
            reveal_type(record.data)
            if record.action == "a":
                record.data = json.dumps({"a": 1})
            elif record.action == "b":
                record.data = to_json([])
            record.stored = "{}"
            record.count = 1

    def _compute_preview(self) -> None:
        self.preview = "#"
        for record in self.filtered("action"):
            record.preview = f"/shop/{record.id}" if record.id else "/shop/%s" % record.action

    def _compute_named(self) -> None:
        for record in self:
            record.update({"named": label(record.action)})
            record["named"] = str(record.id) + "!"

    def _compute_empty(self) -> None:
        for record in self:
            record.empty = "x" if record.action else False

    def _compute_unknown(self) -> None:
        for record in self:
            record.unknown = maybe(record.action)

    def _compute_written(self) -> None:
        for record in self:
            record.written = "x"
            record.editable = "x"

    def _inverse_written(self) -> None:
        pass

    def _compute_local(self) -> None:
        for record in self:
            url = urlparse("/shop")
            record.local = url.geturl()

    def _compute_reset(self) -> None:
        for record in self:
            record.reset = "x"

    def clear(self) -> None:
        self.reset = False

    def _compute_spread(self) -> None:
        for record in self:
            record.spread = "x"
            record.update({**self._values()})

    def _values(self) -> dict[str, object]:
        return {"spread": False}

    def _compute_unassigned(self) -> None:
        for record in self:
            setattr(record, "unassigned", "x")
            record.write(self._values())

    def check(self) -> None:
        reveal_type(self.data)
        reveal_type(self.preview)
        reveal_type(self.named)
        reveal_type(self.parent_data)
        reveal_type(self.read(["data", "empty"])[0])
        reveal_type(self.empty)
        reveal_type(self.unknown)
        reveal_type(self.stored)
        reveal_type(self.written)
        reveal_type(self.editable)
        reveal_type(self.local)
        reveal_type(self.reset)
        reveal_type(self.spread)
        reveal_type(self.unassigned)
"""
EMPTY = "str | Literal[False]"


def _revealed(messages: list[str]) -> list[str]:
    return [message.partition('"')[2].rstrip('"') for message in messages]


def test_a_text_field_its_compute_always_assigns_reads_as_a_string(run_mypy: RunMypy) -> None:
    """``json.loads(record.move_data)`` of a field computed as ``json.dumps(...)`` (Odoo 17)."""
    messages = run_mypy({"my_mod": {"models.py": COMPUTED}}).messages("models.py")

    assert _revealed(messages) == [
        EMPTY,  # in its own compute method: not assigned yet
        "str",  # calls of functions that return str, in two branches of three
        "str",  # a literal, an f-string and a ``%`` format
        "str",  # ``update`` and an item assignment
        "str",  # related to it through a required Many2one
        "TypedDict({'id': int, 'data': str, 'empty': str | Literal[False]})",
        EMPTY,  # ``False`` in a branch
        EMPTY,  # a function that may return None
        EMPTY,  # stored: a record keeps what it had
        "str",  # an inverse method; assigned to each record on every path
        "str",  # ``readonly=False``; assigned to each record on every path
        EMPTY,  # the result of a method of a local value
        EMPTY,  # another method of the model assigns False
        EMPTY,  # the compute method writes names that are not read
        "str",  # ``setattr`` with the name; ``write`` of a dict that is no display
    ], messages


def test_a_computed_text_keeps_false_without_required_values(run_mypy: RunMypy) -> None:
    result = run_mypy({"my_mod": {"models.py": COMPUTED}}, options="required_values = false\n")

    revealed = _revealed(result.messages("models.py"))

    assert revealed[:5] == [EMPTY] * 5, revealed
    assert (
        revealed[5]
        == "TypedDict({'id': int, 'data': str | Literal[False], 'empty': str | Literal[False]})"
    )


EXTENSION = """
from odoo import models


class Wizard(models.TransientModel):
    _inherit = "t.wizard"

    def _compute_preview(self) -> None:
        super()._compute_preview()
        for record in self:
            if not record.action:
                record.preview = False

    def check_extension(self) -> None:
        reveal_type(self.preview)
        reveal_type(self.data)
"""
READER = """
from odoo import models


class Partner(models.Model):
    _inherit = "res.partner"

    def check_wizard(self) -> None:
        wizard = self.env["t.wizard"]
        reveal_type(wizard.preview)
        reveal_type(wizard.data)

    def _compute_preview(self) -> None:
        reveal_type(self.env["t.wizard"].preview)  # the compute method of another model
"""


def test_an_override_of_the_compute_method_counts_where_it_is_seen(run_mypy: RunMypy) -> None:
    """The classes of the model as the reading addon sees them decide."""
    addons = {
        "t_wizard": {"models.py": COMPUTED},
        "t_extension": {"depends": ["t_wizard"], "models.py": EXTENSION},
        "t_reader": {"depends": ["t_wizard"], "models.py": READER},
    }

    result = run_mypy(addons, check=["t_extension", "t_reader"])

    assert _revealed(result.messages("t_extension/models.py")) == [EMPTY, "str"]
    assert _revealed(result.messages("t_reader/models.py")) == ["str", "str", "str"]


def test_a_computed_text_reads_the_same_from_the_cache(run_mypy: RunMypy) -> None:
    """The functions the compute method calls are found in a warm run too."""
    addons = {
        "t_wizard": {"models.py": COMPUTED},
        "t_reader": {"depends": ["t_wizard"], "models.py": READER},
    }

    cold = run_mypy(addons, check=["t_reader"], fresh_cache=True)
    warm = run_mypy(addons, check=["t_reader"], fresh_cache=True)

    assert _revealed(cold.messages("t_reader/models.py")) == ["str", "str", "str"]
    assert warm.lines == cold.lines


FILLED = """
from odoo import fields, models


class Page(models.Model):
    _name = "t.page"

    url = fields.Char()


class Menu(models.Model):
    _name = "t.menu"

    page_id = fields.Many2one("t.page")
    parent_id = fields.Many2one("t.menu")
    manual_url = fields.Char()
    container = fields.Boolean()
    url = fields.Char(compute="_compute_url", inverse="_inverse_url")
    label = fields.Char(compute="_compute_label", readonly=False)
    target = fields.Char(compute="_compute_target", inverse="_inverse_target")
    anchor = fields.Char(compute="_compute_anchor", inverse="_inverse_target")
    title = fields.Char(compute="_compute_title", readonly=False)
    page = fields.Char(compute="_compute_page", readonly=False)
    note = fields.Html(compute="_compute_note", readonly=False)
    code = fields.Char(compute="_compute_code", readonly=False)

    def _compute_url(self) -> None:
        for menu in self:
            if menu.container:
                menu.url = "#"
            else:
                menu.url = (menu.page_id.url if menu.page_id else menu.manual_url) or "#"

    def _inverse_url(self) -> None:
        for menu in self:
            reveal_type(menu.url)
            if menu.url and menu.page_id:
                menu.page_id.write({"url": menu.url})

    def _compute_label(self) -> None:
        for menu in self:
            if not menu.container:
                menu.label = "-"
                continue
            menu.label = menu.manual_url or "#"

    def _compute_target(self) -> None:
        for menu in self:
            if menu.container:
                menu.target = "#"

    def _compute_anchor(self) -> None:
        for menu in self:
            if not menu.page_id:
                continue
            menu.anchor = "#"

    def _inverse_target(self) -> None:
        pass

    def _compute_title(self) -> None:
        for menu in self.filtered("container"):
            menu.title = "#"

    def _compute_page(self) -> None:
        for menu in self:
            menu.page = "#"
            menu.parent_id.write({"page": menu.manual_url})

    def _compute_note(self) -> None:
        for menu in self:
            menu.note = menu.manual_url or "#"

    def _compute_code(self) -> None:
        self.code = "-"


class MenuOverride(models.Model):
    _inherit = "t.menu"

    def _compute_url(self) -> None:
        super()._compute_url()
        for menu in self:
            if not menu.parent_id:
                menu.url = "/"

    def _compute_code(self) -> None:
        for menu in self:
            if menu.container:
                menu.code = "#"

    def check(self) -> None:
        reveal_type(self.url)
        reveal_type(self.label)
        reveal_type(self.target)
        reveal_type(self.anchor)
        reveal_type(self.title)
        reveal_type(self.page)
        reveal_type(self.note)
        reveal_type(self.code)
        self.filtered(lambda menu: menu.url[:5] == "/shop")
"""


def test_an_editable_text_field_its_compute_assigns_on_every_path_reads_as_a_string(
    run_mypy: RunMypy,
) -> None:
    """``m.url[:5]`` of ``website.menu.url`` (Odoo 20): a compute with an inverse.

    Odoo fills in ``False`` where the compute method of such a field assigns nothing, so
    it must assign each record on every path; ``x or "#"`` is a string in a ``Char``.
    """
    messages = run_mypy({"my_mod": {"models.py": FILLED}}).messages("models.py")

    assert _revealed(messages) == [
        EMPTY,  # in its inverse method: the value written
        "str",  # each branch; ``or "#"``; a write to the records of another model; super()
        "str",  # ``continue`` after the assignment
        EMPTY,  # one branch of two
        EMPTY,  # ``continue`` before the assignment
        EMPTY,  # some of the records
        EMPTY,  # a write to the records of the model itself
        EMPTY,  # ``or`` in an ``Html`` field: the value is kept as it is
        EMPTY,  # an override without super() that assigns in one branch
    ], messages
