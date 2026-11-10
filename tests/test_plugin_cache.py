"""mypy's incremental cache must not serve results built from an outdated registry.

Each test runs mypy twice on the same cache with an edit in between, and compares the
second run with what a cold run gives. The registry's inputs (manifests, the classes
other files contribute, the configured addons paths) are not imports of the checked
file, so only the plugin's per-module config data tells mypy that a cached module is
out of date.
"""

from __future__ import annotations

import re
from pathlib import Path

from conftest import MypyResult, RunMypy, write_addon

THING = """
from odoo import fields, models

class Thing(models.Model):
    _name = "x.thing"

    name = fields.Char()

    def helper(self) -> int:
        return 1
"""

EXTRA = """
from odoo import fields, models

class ThingExtra(models.Model):
    _inherit = "x.thing"

    extra = fields.Integer()
"""

USER = """
from odoo import models

class User(models.Model):
    _name = "x.user"

    def action(self) -> None:
        reveal_type(self.env["x.thing"].extra)
"""

ENV_USER = """
from odoo import models

class User(models.Model):
    _name = "x.user"

    def action(self) -> None:
        reveal_type(self.env["x.thing"])
"""

ABANDONED = re.compile(r"Metadata abandoned for (\S+?): (.*)")
PARSED = re.compile(r"Parsing \S+ \((\S+)\)")


def _dep(*extensions: str) -> dict[str, object]:
    """``dep_mod``: defines x.thing; each name in ``extensions`` is a module with EXTRA."""
    spec: dict[str, object] = {"models/__init__.py": "", "models/thing.py": THING}
    imports = ["thing", *extensions]
    spec["models/__init__.py"] = "".join(f"from . import {name}\n" for name in imports)
    spec["__init__.py"] = "from . import models\n"
    for name in extensions:
        spec[f"models/{name}.py"] = EXTRA
    return spec


def _abandoned(result: MypyResult) -> dict[str, str]:
    """Modules whose cache entry mypy dropped, with the reason."""
    return dict(ABANDONED.findall(result.stderr))


def test_cache_extension_added_in_dependency_addon_sees_new_field(run_mypy: RunMypy) -> None:
    run_mypy({"dep_mod": _dep(), "my_mod": {"depends": ["dep_mod"], "user.py": USER}})

    result = run_mypy({"dep_mod": _dep("extra")}, check=["my_mod"])

    assert result.messages("my_mod/user.py") == ['8: note: Revealed type is "int"']


def test_cache_depends_added_sees_field_of_new_dependency(run_mypy: RunMypy) -> None:
    addons = {"dep_mod": _dep(), "ext_mod": {"depends": ["dep_mod"], "models.py": EXTRA}}
    run_mypy({**addons, "my_mod": {"depends": ["dep_mod"], "user.py": USER}})

    result = run_mypy({"my_mod": {"depends": ["ext_mod"], "user.py": USER}}, check=["my_mod"])

    assert result.messages("my_mod/user.py") == ['8: note: Revealed type is "int"']


def test_cache_depends_removed_reports_unknown_model(run_mypy: RunMypy) -> None:
    run_mypy({"dep_mod": _dep(), "my_mod": {"depends": ["dep_mod"], "user.py": ENV_USER}})

    result = run_mypy({"my_mod": {"depends": ["base"], "user.py": ENV_USER}}, check=["my_mod"])

    assert result.messages("my_mod/user.py") == [
        '8: error: Unknown Odoo model "x.thing"  [odoo-model]',
        '8: note: Revealed type is "odoo.models.BaseModel"',
    ]


def test_cache_addons_path_added_resolves_model(run_mypy: RunMypy, tmp_path: Path) -> None:
    other = tmp_path / "other"
    write_addon(other / "dep_mod", _dep())
    addons = {"my_mod": {"depends": ["dep_mod"], "user.py": ENV_USER}}
    run_mypy(addons, mypy_path=[other])

    result = run_mypy(addons, mypy_path=[other], addons_path=[tmp_path / "addons", other])

    assert result.messages("my_mod/user.py") == [
        '8: note: Revealed type is "dep_mod.models.thing.Thing"'
    ]


def test_cache_extension_module_deleted_reports_missing_field(
    run_mypy: RunMypy, tmp_path: Path
) -> None:
    run_mypy({"dep_mod": _dep("extra"), "my_mod": {"depends": ["dep_mod"], "user.py": USER}})
    (tmp_path / "addons/dep_mod/models/extra.py").unlink()

    result = run_mypy({"dep_mod": _dep()}, check=["my_mod"])

    assert result.lines[:2] == [
        f'{tmp_path}/addons/my_mod/user.py:8: error: "Thing" has no attribute "extra"  '
        "[attr-defined]",
        f'{tmp_path}/addons/my_mod/user.py:8: note: Revealed type is "Any"',
    ]


def test_cache_extension_module_moved_keeps_field(run_mypy: RunMypy, tmp_path: Path) -> None:
    run_mypy({"dep_mod": _dep("extra"), "my_mod": {"depends": ["dep_mod"], "user.py": USER}})
    (tmp_path / "addons/dep_mod/models/extra.py").unlink()

    result = run_mypy({"dep_mod": _dep("moved")}, check=["my_mod"])

    assert result.lines == [
        f'{tmp_path}/addons/my_mod/user.py:8: note: Revealed type is "int"',
        "Success: no issues found in 3 source files",
    ]


def test_cache_same_source_in_other_checkout_is_not_reused(run_mypy: RunMypy) -> None:
    run_mypy(
        {"dep_mod": _dep(), "my_mod": {"depends": ["dep_mod"], "user.py": ENV_USER}},
        workdir="one",
    )

    result = run_mypy({"my_mod": {"depends": ["base"], "user.py": ENV_USER}}, workdir="two")

    assert result.messages("my_mod/user.py")[0] == (
        '8: error: Unknown Odoo model "x.thing"  [odoo-model]'
    )


def test_cache_unchanged_rerun_keeps_every_module_fresh(run_mypy: RunMypy) -> None:
    addons = {"dep_mod": _dep("extra"), "my_mod": {"depends": ["dep_mod"], "user.py": USER}}
    run_mypy(addons)

    result = run_mypy(addons, flags=["-v"])

    assert (_abandoned(result), PARSED.findall(result.stderr)) == ({}, [])


def test_cache_method_body_edited_in_dependency_keeps_user_fresh(run_mypy: RunMypy) -> None:
    run_mypy({"dep_mod": _dep(), "my_mod": {"depends": ["dep_mod"], "user.py": ENV_USER}})
    edited = _dep()
    edited["models/thing.py"] = THING.replace("return 1", "return 1 + 1")

    result = run_mypy({"dep_mod": edited}, flags=["-v"])

    assert set(_abandoned(result)) == {"dep_mod.models.thing"}
