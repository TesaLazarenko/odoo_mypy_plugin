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
