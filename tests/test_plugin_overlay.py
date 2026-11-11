"""End-to-end tests of the overlay root: custom addons imported as ``odoo.addons.<name>``.

Odoo 18 only makes an addon importable as ``odoo.addons.<name>``, so addons import each
other (and their own ``tests.common``) that way. With an ``odoo-typing-root`` overlay on
``mypy_path`` these imports resolve, and every addon file has exactly that one name.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest
from conftest import _assert_no_crash, write_addon
from mypy import api as mypy_api
from mypy import build
from mypy.main import process_options

from odoo_typing.config import load_settings
from odoo_typing.overlay import build_overlay

PYPROJECT = """\
[tool.mypy]
plugins = ["odoo_typing.plugin"]
mypy_path = ["{root}"]
check_untyped_defs = true
show_error_codes = true
show_error_context = false
show_traceback = true

[[tool.mypy.overrides]]
module = ["odoo", "odoo.*"]
follow_imports = "silent"

[tool.odoo_typing]
odoo_path = "{odoo_path}"
odoo_version = {odoo_version}
addons_path = ["addons"]
overlay_root = ".odoo-root"
"""

OV_BASE = {
    "__init__.py": "from . import models\n",
    "models/__init__.py": "from . import thing\n",
    "models/thing.py": """
        from odoo import fields, models

        def helper() -> int:
            return 1

        class Thing(models.Model):
            _name = "ov.thing"
            _description = "Thing"

            size = fields.Integer()
    """,
    "tests/__init__.py": "from . import common\n",
    "tests/common.py": """
        from odoo.tests.common import TransactionCase

        class ThingCase(TransactionCase):
            def make(self) -> int:
                return 1
    """,
}

OV_EXT = {
    "depends": ["ov_base"],
    "__init__.py": "from . import models\n",
    "models/__init__.py": "from . import ext\n",
    "models/ext.py": """
        from odoo import fields, models
        from odoo.addons.ov_base.models.thing import Thing, helper

        class ThingExt(models.Model):
            _inherit = "ov.thing"

            label = fields.Char()

            def act(self, other: Thing) -> None:
                reveal_type(helper())
                reveal_type(other)
                reveal_type(self.size)
                reveal_type(self.env["ov.thing"])
                wrong: str = helper()
    """,
    "tests/test_ext.py": """
        from odoo.addons.ov_base.tests.common import ThingCase
        from odoo.addons.ov_ext.models.ext import ThingExt

        class TestExt(ThingCase):
            def test_it(self, record: ThingExt) -> None:
                reveal_type(self.make())
                reveal_type(record.label)
    """,
}


@dataclass(frozen=True)
class Project:
    root: Path  # the project dir (pyproject.toml, addons/, .odoo-root/)
    config: Path

    @property
    def overlay_addons(self) -> Path:
        return self.root / ".odoo-root" / "odoo" / "addons"


@pytest.fixture(scope="module")
def project(
    odoo_path: Path, odoo_root: Path, odoo_version: int, tmp_path_factory: pytest.TempPathFactory
) -> Project:
    del odoo_root  # requested for its skip: no profile for this Odoo version yet
    root = tmp_path_factory.mktemp("overlay_project")
    write_addon(root / "addons" / "ov_base", OV_BASE)
    write_addon(root / "addons" / "ov_ext", OV_EXT)
    config = root / "pyproject.toml"
    config.write_text(
        PYPROJECT.format(root=root / ".odoo-root", odoo_path=odoo_path, odoo_version=odoo_version)
    )
    build_overlay(load_settings(str(config), require_overlay=False), root / ".odoo-root")
    return Project(root=root, config=config)


def _run(
    project: Project, cache: Path, capfd: pytest.CaptureFixture[str], *targets: str
) -> tuple[list[str], str]:
    stdout, stderr, status = mypy_api.run(
        ["--config-file", str(project.config), "--cache-dir", str(cache), *targets]
    )
    captured = capfd.readouterr()
    _assert_no_crash(status, stdout, stderr, captured.out, captured.err)
    return stdout.splitlines(), captured.err


def _messages(lines: list[str], filename: str) -> list[str]:
    marker = f"/{filename}:"
    return [line.split(marker, 1)[1] for line in lines if marker in line]


EXT_MESSAGES = [
    ' note: Revealed type is "int"',
    ' note: Revealed type is "odoo.addons.ov_base.models.thing.Thing"',
    ' note: Revealed type is "int"',
    ' note: Revealed type is "odoo.addons.ov_ext.models.ext.ThingExt"',
    ' error: Incompatible types in assignment (expression has type "int", variable has type'
    ' "str")  [assignment]',
]


@pytest.fixture(scope="module")
def cache(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.mktemp("overlay_cache")


def test_overlay_path_target_cross_addon_imports_resolve_and_are_typed(
    project: Project, cache: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    lines, err = _run(project, cache, capfd, str(project.overlay_addons / "ov_ext"))

    assert not [line for line in lines if "import-not-found" in line], lines
    assert _messages(lines, "ext.py") == [
        f"{line}:{message}" for line, message in zip(range(11, 16), EXT_MESSAGES, strict=True)
    ]
    assert _messages(lines, "test_ext.py") == [
        '7: note: Revealed type is "int"',
        '8: note: Revealed type is "str | Literal[False]"',
    ]
    assert err == ""


def test_overlay_path_target_each_file_has_one_module_name(project: Project, cache: Path) -> None:
    target = str(project.overlay_addons / "ov_ext")
    sources, options = process_options(
        ["--config-file", str(project.config), "--cache-dir", str(cache), target]
    )

    result = build.build(sources, options)

    names = {name: state.path for name, state in result.graph.items() if "ov_" in name}
    assert all(name.startswith("odoo.addons.ov_") for name in names), sorted(names)
    real = [Path(path).resolve() for path in names.values() if path]
    assert len(real) == len(set(real))
    assert {"odoo.addons.ov_base.models.thing", "odoo.addons.ov_ext.tests.test_ext"} <= set(names)


def test_overlay_package_target_reports_errors_of_checked_addon(
    project: Project, cache: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    lines, _ = _run(project, cache, capfd, "-p", "odoo.addons.ov_ext")

    assert [message for message in _messages(lines, "ext.py") if "error" in message] == [
        f"15:{EXT_MESSAGES[-1]}"
    ]
    assert not [line for line in lines if "import-not-found" in line], lines


def test_overlay_real_path_target_warns_about_module_name(
    project: Project, tmp_path: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    _, err = _run(project, tmp_path / "cache", capfd, str(project.root / "addons" / "ov_ext"))

    assert err.count("odoo-typing: warning") == 1
    assert 'module "ov_ext' in err
    assert '"odoo.addons.ov_ext"' in err
