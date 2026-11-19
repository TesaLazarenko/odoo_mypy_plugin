"""Odoo idioms the plugin read as errors: what Odoo does with them is no error."""

from __future__ import annotations

from conftest import RunMypy

FILTERED = """
from odoo import fields, models

class Site(models.Model):
    _name = "t.site"

    url = fields.Char()
    note = fields.Char()
    partner_id = fields.Many2one("res.partner")

    def check(self) -> None:
        for site in self.filtered("url"):
            site.url.startswith("/")
            site.note.startswith("/")
        chosen = self.filtered("url").filtered("note")
        for site in chosen:
            site.url.upper() + site.note.upper()
        for site in self.filtered("partner_id.email"):
            site.note.upper()
"""


def test_records_kept_by_filtered_have_the_field_set(run_mypy: RunMypy) -> None:
    result = run_mypy({"t_mod": {"depends": ["base"], "models.py": FILTERED}})

    bool_item = 'Item "bool" of "str | Literal[False]" has no attribute "upper"  [union-attr]'
    assert result.messages("t_mod/models.py") == [
        '14: error: Item "bool" of "str | Literal[False]" has no attribute "startswith"'
        "  [union-attr]",
        f"19: error: {bool_item}",  # a path: some partner has an email, not the field
    ]


REDEFINED = """
from odoo import api, fields, models

class View(models.Model):
    _name = "t.view"

    theme_id = fields.Many2one("t.view")

class Page(models.Model):
    _name = "t.page"
    _inherits = {"t.view": "view_id"}

    view_id = fields.Many2one("t.view", required=True, ondelete="cascade")
    display_name = fields.Char(compute="_compute_display_name")

    @api.depends("view_id")
    def _compute_display_name(self) -> None:
        for page in self:
            page.display_name = "x"

class PageTheme(models.Model):
    _inherit = "t.page"

    theme_id = fields.Many2one("t.page")
"""


def test_own_fields_redefine_display_name_and_delegated_fields(run_mypy: RunMypy) -> None:
    """Odoo sets up the model's own field: no conflict with ``BaseModel`` or the delegate."""
    result = run_mypy({"t_mod": {"depends": ["base"], "models.py": REDEFINED}})

    assert result.messages("t_mod/models.py") == []


WRAPS = """
import functools

class Response:
    def set_cookie(self, key: str, value: str = "", max_age: int | None = None) -> None: ...

class FutureResponse:
    @functools.wraps(Response.set_cookie)
    def set_cookie(self, key, value="", max_age=None):
        pass

def use(response: FutureResponse) -> None:
    response.set_cookie("website_cookies_bar", max_age=0)
    response.set_cookie()
    FutureResponse.set_cookie(response, "name")
"""


def test_a_method_decorated_with_wraps_is_bound_on_an_instance(run_mypy: RunMypy) -> None:
    result = run_mypy({"t_mod": {"depends": ["base"], "tools.py": WRAPS}})

    messages = result.messages("t_mod/tools.py")
    assert len(messages) == 1
    assert messages[0].startswith('14: error: Missing positional argument "key"')


DECORATED = """
from odoo import api, fields, models

class Order(models.Model):
    _name = "t.order"

    name = fields.Char()
    total = fields.Float(compute="_compute_total")

    def _checked_fields(self) -> list[str]:
        return ["name"]

    @api.constrains(lambda self: self._checked_fields())
    def _check_name(self) -> None:
        pass

    @api.depends(lambda self: ["name"])
    def _compute_total(self) -> None:
        for order in self:
            order.total = 1.0

    @api.depends("nmae")
    def _compute_other(self) -> None:
        pass
"""


def test_field_decorators_take_a_function(run_mypy: RunMypy) -> None:
    """Odoo tests ``callable(args[0])`` (17, 18) or declares an overload (19+)."""
    result = run_mypy({"t_mod": {"depends": ["base"], "models.py": DECORATED}})

    messages = result.messages("t_mod/models.py")
    assert len(messages) == 1
    assert messages[0].startswith('22: error: Odoo model "t.order" has no field "nmae"')


SPLICED = """
from odoo import fields, models

CLOSED_STATES = {"1_done": "Done", "1_canceled": "Canceled"}
OPEN = [("01_in_progress", "In Progress")]

class Task(models.Model):
    _name = "t.task"

    state = fields.Selection([*OPEN, *CLOSED_STATES.items(), ("04_waiting_normal", "Waiting")])

    def check(self) -> None:
        if self.state != "04_waiting_normal" and self.state not in CLOSED_STATES:
            pass
        if self.state == "1_done" or self.state == "04_waiting":
            pass
"""


def test_a_selection_spliced_from_constants_is_checked(run_mypy: RunMypy) -> None:
    result = run_mypy({"t_mod": {"depends": ["base"], "models.py": SPLICED}})

    messages = result.messages("t_mod/models.py")
    assert len(messages) == 1
    assert messages[0].startswith('15: error: Value "04_waiting" is not in the selection')
    assert '("01_in_progress", "1_done", "1_canceled", "04_waiting_normal")' in messages[0]


COMMANDS = """
from odoo import Command, fields, models

class Tag(models.Model):
    _name = "t.tag"

    name = fields.Char()

class Task(models.Model):
    _name = "t.task"

    tag_ids = fields.Many2many("t.tag")

    def tag(self, names: list[str]) -> None:
        self.tag_ids = [Command.set([])] + [Command.create({"nmae": name}) for name in names]
        extra = [Command.create({"name": "x"})]
        self.write({"tag_ids": [*extra, Command.create({"nme": "y"})]})
"""


def test_commands_built_with_comprehensions_and_concatenation(run_mypy: RunMypy) -> None:
    result = run_mypy({"t_mod": {"depends": ["base"], "models.py": COMMANDS}})

    messages = result.messages("t_mod/models.py")
    assert [message.split(":")[0] for message in messages] == ["15", "17"]
    assert '"nmae"' in messages[0]
    assert '"nme"' in messages[1]
