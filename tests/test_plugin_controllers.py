"""End-to-end tests of ``request.env`` in controllers (roadmap item 4.5)."""

from __future__ import annotations

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
