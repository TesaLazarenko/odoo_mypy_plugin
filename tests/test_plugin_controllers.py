"""End-to-end tests of ``request.env`` in controllers (roadmap item 4.5)."""

from __future__ import annotations

import pytest
from conftest import RunMypy

CONTROLLER = """
from odoo import http
from odoo.http import request


class Main(http.Controller):
    @http.route("/t", auth="user")
    def page(self) -> None:
        reveal_type(request.env)
        partners = request.env["res.partner"].search([])
        reveal_type(partners)
        request.env["res.partnr"]
"""


def test_request_env_in_controller_is_checked(run_mypy: RunMypy) -> None:
    result = run_mypy({"my_mod": {"controllers.py": CONTROLLER}})

    messages = result.messages("my_mod/controllers.py")
    assert messages[0].endswith('.Environment"')
    assert messages[1].startswith('11: note: Revealed type is "odoo.addons.base.')
    assert messages[2:] == ['12: error: Unknown Odoo model "res.partnr"  [odoo-model]']


def test_request_attributes_set_by_addons_are_any(run_mypy: RunMypy) -> None:
    # website and http_routing set them at run time; Request does not declare them.
    result = run_mypy(
        {
            "my_mod": {
                "controllers.py": """
                from odoo import http
                from odoo.http import request


                class Main(http.Controller):
                    @http.route("/t", auth="public")
                    def page(self) -> None:
                        reveal_type(request.website)
                        if request.is_frontend and request.lang:
                            request.website_routing = 1
                        request.webiste
                """
            }
        }
    )

    assert result.messages("my_mod/controllers.py") == [
        '9: note: Revealed type is "Any"',
        '12: error: "Request" has no attribute "webiste"; maybe "website"?  [attr-defined]',
    ]


CURSOR = """
from odoo import models

class Thing(models.Model):
    _name = "t.thing"

    def check(self) -> None:
        self.env.cr.execute("SELECT 1")
        rows = self.env.cr.fetchall()
        name = self.env.cr.dbname
        cr = self.env.cr
        cr.execute("SELECT %s", (name,))
        self.env.cr.execute()
"""


def test_env_cr_has_the_methods_of_a_cursor(run_mypy: RunMypy, odoo_version: int) -> None:
    result = run_mypy({"t_cr": {"models.py": CURSOR}})

    # Odoo 18 declares ``cr: BaseCursor``, which has no ``execute`` (Odoo 17: ``Any``;
    # Odoo 19+: a class that has the methods of a cursor).
    messages = result.messages("t_cr/models.py")
    assert [message.partition(": error: ")[0] for message in messages] == (
        ["13"] if odoo_version >= 18 else []
    )
    assert not any("attr-defined" in message for message in messages)


@pytest.mark.odoo(20)
def test_env_of_a_controller_is_an_environment(run_mypy: RunMypy) -> None:
    """Odoo 20 declares ``Controller.env`` as ``Environment | None``, like ``request.env``."""
    result = run_mypy(
        {
            "my_mod": {
                "controllers.py": """
                from odoo import http

                class Main(http.Controller):
                    @http.route("/my", type="http", auth="public")
                    def index(self):
                        reveal_type(self.env["res.partner"].search([]).name)
                        self.env["res.partnr"]
                """
            }
        }
    )

    messages = result.messages("controllers.py")
    assert messages[0] == '7: note: Revealed type is "str | Literal[False]"'
    assert len(messages) == 2, messages
    assert 'Unknown Odoo model "res.partnr"' in messages[1]


def test_optional_env_of_an_addons_own_class_stays_optional(run_mypy: RunMypy) -> None:
    """The plugin corrects what Odoo declares, not what the addon's author declared."""
    result = run_mypy(
        {
            "my_mod": {
                "listener.py": """
                from odoo.api import Environment

                class Listener:
                    env: Environment | None = None

                    def handle(self) -> None:
                        self.env["res.partner"]
                        if self.env is not None:
                            reveal_type(self.env["res.partner"])
                """
            }
        }
    )

    messages = result.messages("listener.py")
    assert len(messages) == 2, messages
    assert messages[0].startswith('8: error: Value of type "Environment | None" is not indexable')
    assert messages[1].startswith("10: note: Revealed type is ")


@pytest.mark.odoo(20)
def test_env_attribute_named_after_a_model_is_that_models_records(run_mypy: RunMypy) -> None:
    """``env.website`` (Odoo 20) is declared ``BaseModel``: records of no model."""
    source = """
    from odoo import http

    class Main(http.Controller):
        @http.route("/my", type="http", auth="public")
        def index(self):
            reveal_type(self.env.website.name)
            self.env.website.nope
    """
    result = run_mypy({"my_mod": {"controllers.py": source}})

    messages = result.messages("controllers.py")
    assert messages[0] == '7: note: Revealed type is "str"'
    assert len(messages) == 2, messages
    assert '"Website" has no attribute "nope"' in messages[1]
