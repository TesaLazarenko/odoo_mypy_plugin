"""mypy's cache must follow the plugin itself: a new version, and what it types in Odoo.

Like :mod:`test_plugin_cache`, each test runs mypy twice on the same cache, with a change
in between, and compares the second run with a cold one.
"""

from __future__ import annotations

import re

import pytest
from conftest import RunMypy

from odoo_typing import plugin as plugin_module
from odoo_typing.compat import profile_for

THING = """
from odoo import fields, models

class Thing(models.Model):
    _name = "x.thing"

    name = fields.Char()
"""

ABANDONED = re.compile(r"Metadata abandoned for (\S+?): (.*)")


def test_cache_new_plugin_version_rechecks_modules_outside_addons(
    run_mypy: RunMypy, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A module outside addons reports no config data: only the plugins snapshot (the
    # plugin module's file and ``__version__``) tells mypy that the plugin changed.
    addons = {"my_mod": {"models.py": THING}}
    scripts = {"helper.py": "x: int = 1\n"}
    run_mypy(addons, scripts=scripts, fresh_cache=True)
    monkeypatch.setattr(plugin_module, "__version__", "next")  # what a new version gives

    result = run_mypy(addons, scripts=scripts, fresh_cache=True, flags=["-v"])

    assert dict(ABANDONED.findall(result.stderr)).get("helper") == "plugins differ"


REQUEST_HELPER = """
from odoo.http import request


def helper() -> None:
    reveal_type(request)
"""


def test_cache_request_stays_typed_in_warm_run_of_module_without_classes(
    run_mypy: RunMypy, odoo_version: int
) -> None:
    # Only the edited module is checked again, and no class hook runs before it: Odoo's
    # own cache entry of odoo.http must carry the type the plugin gives ``request``.
    spec: dict[str, object] = {
        "__init__.py": "from . import controllers\n",
        "controllers/__init__.py": "from . import utils\n",
        "controllers/utils.py": REQUEST_HELPER,
    }
    cold = run_mypy({"my_mod": spec}, fresh_cache=True)
    spec["controllers/utils.py"] = REQUEST_HELPER + "# edited\n"

    warm = run_mypy({"my_mod": spec}, fresh_cache=True)

    profile = profile_for(odoo_version)
    assert profile is not None
    revealed = [f'6: note: Revealed type is "{profile.request_class}"']
    assert (cold.messages("utils.py"), warm.messages("utils.py")) == (revealed, revealed)
