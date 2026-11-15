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


def test_cache_depends_removed_reports_model_outside_dependencies(run_mypy: RunMypy) -> None:
    run_mypy({"dep_mod": _dep(), "my_mod": {"depends": ["dep_mod"], "user.py": ENV_USER}})

    result = run_mypy({"my_mod": {"depends": ["base"], "user.py": ENV_USER}}, check=["my_mod"])

    assert result.messages("my_mod/user.py") == [
        '8: error: Odoo model "x.thing" is defined in addon "dep_mod", which is not in'
        ' the dependencies of "my_mod"  [odoo-model-depends]',
        '8: note: Revealed type is "Any"',
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


DELEGATING = """
from odoo import fields, models

class Child(models.Model):
    _name = "x.child"
    _inherits = {"x.thing": "thing_id"}

    thing_id = fields.Many2one("x.thing", required=True)
"""

DELEGATED_USER = """
from odoo import models

class User(models.Model):
    _name = "x.user"

    def action(self) -> None:
        reveal_type(self.env["x.child"].extra)
"""

USERS_EXTRA = """
from odoo import fields, models

class Users(models.Model):
    _inherit = "res.users"

    x_level = fields.Integer()
"""

ENV_USER_FIELD = """
from odoo import models

class User(models.Model):
    _name = "x.user"

    def action(self) -> None:
        reveal_type(self.env.user.x_level)
"""


def test_cache_extension_of_delegated_parent_added_sees_new_field(run_mypy: RunMypy) -> None:
    dep = {**_dep(), "models/child.py": DELEGATING}
    dep["models/__init__.py"] = "from . import thing, child\n"
    run_mypy({"dep_mod": dep, "my_mod": {"depends": ["dep_mod"], "user.py": DELEGATED_USER}})
    dep["models/extra.py"] = EXTRA
    dep["models/__init__.py"] = "from . import thing, child, extra\n"

    result = run_mypy({"dep_mod": dep}, check=["my_mod"])

    assert result.messages("my_mod/user.py") == ['8: note: Revealed type is "int"']


def test_cache_res_users_extension_added_types_env_user_field(run_mypy: RunMypy) -> None:
    dep = {"__init__.py": "from . import models\n", "models/__init__.py": ""}
    run_mypy({"dep_mod": dep, "my_mod": {"depends": ["dep_mod"], "user.py": ENV_USER_FIELD}})
    dep = {**dep, "models/__init__.py": "from . import users\n", "models/users.py": USERS_EXTRA}

    result = run_mypy({"dep_mod": dep}, check=["my_mod"])

    assert result.messages("my_mod/user.py") == ['8: note: Revealed type is "int"']


def test_cache_addon_outside_closure_added_turns_unknown_into_depends(run_mypy: RunMypy) -> None:
    first = run_mypy({"my_mod": {"depends": ["base"], "user.py": ENV_USER}}, check=["my_mod"])
    assert first.messages("my_mod/user.py")[0].startswith('8: error: Unknown Odoo model "x.thing"')

    result = run_mypy({"dep_mod": _dep()}, check=["my_mod"])

    assert result.messages("my_mod/user.py") == [
        '8: error: Odoo model "x.thing" is defined in addon "dep_mod", which is not in'
        ' the dependencies of "my_mod"  [odoo-model-depends]',
        '8: note: Revealed type is "Any"',
    ]


COMPUTED = """
from odoo import fields, models

class Thing(models.Model):
    _name = "x.thing"

    total = fields.Float(compute="_compute_total")

    def _compute_total(self) -> None:
        pass
"""

SEARCHES = """
from odoo import models

class User(models.Model):
    _name = "x.user"

    def action(self) -> None:
        self.env["x.thing"].search([("total", "=", 1)])
        self.env["x.own"].search([("total", "=", 1)])
"""


def test_cache_field_argument_edited_in_another_module_rechecks_its_users(
    run_mypy: RunMypy,
) -> None:
    # A field's type does not change with ``store=``; the addons' fingerprints tell mypy
    # (outside the daemon, which uses the fields digest of hooks.model_classes).
    def addons(arguments: str) -> dict[str, dict[str, object]]:
        thing = COMPUTED.replace('compute="_compute_total"', arguments)
        own = thing.replace("x.thing", "x.own")
        return {
            "dep_mod": {"models.py": thing},
            "my_mod": {"depends": ["dep_mod"], "own.py": own, "user.py": SEARCHES},
        }

    first = run_mypy(addons('compute="_compute_total"'))
    second = run_mypy(addons('compute="_compute_total", store=True'))

    assert [line.split(" cannot be searched")[0] for line in first.messages("user.py")] == [
        '8: error: Field "total" of Odoo model "x.thing"',
        '9: error: Field "total" of Odoo model "x.own"',
    ]
    assert second.messages("user.py") == []


def test_cache_depends_removed_then_outside_addon_deleted_reports_unknown_model(
    run_mypy: RunMypy, tmp_path: Path
) -> None:
    # Which models a file names outside its closure is kept per file: it must follow
    # the closure, or the file no longer follows the addons outside it.
    addons = {"dep_mod": _dep(), "my_mod": {"depends": ["base", "dep_mod"], "user.py": ENV_USER}}
    run_mypy(addons, fresh_cache=True)
    manifest = tmp_path / "addons/my_mod/__manifest__.py"
    manifest.write_text(manifest.read_text().replace(", 'dep_mod'", ""))  # user.py unchanged
    outside = run_mypy({}, check=["my_mod"], fresh_cache=True)
    (tmp_path / "addons/dep_mod").rename(tmp_path / "dep_mod")  # off the addons path

    result = run_mypy({}, check=["my_mod"], fresh_cache=True)

    assert outside.messages("my_mod/user.py")[0].endswith("[odoo-model-depends]")
    assert result.messages("my_mod/user.py")[0].startswith('8: error: Unknown Odoo model "x.thing"')
