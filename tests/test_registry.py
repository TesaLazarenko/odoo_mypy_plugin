"""Unit tests for addon discovery, scanning and the model registry (no mypy run)."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from odoo_typing.config import Settings
from odoo_typing.registry import Registry


def _addon(root: Path, name: str, depends: list[str], files: dict[str, str]) -> None:
    path = root / name
    path.mkdir(parents=True)
    (path / "__manifest__.py").write_text(repr({"name": name, "depends": depends}))
    for relative, source in files.items():
        target = path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(textwrap.dedent(source))


@pytest.fixture
def registry(tmp_path: Path) -> Registry:
    odoo = tmp_path / "odoo"
    (odoo / "addons").mkdir(parents=True)
    (odoo / "release.py").write_text("")
    _addon(
        odoo / "addons",
        "base",
        [],
        {
            "__init__.py": "from . import models\n",
            "models/__init__.py": "from . import partner\n",
            "models/partner.py": """
                class Partner(models.Model):
                    _name = "res.partner"
                    _inherit = ["mail.mixin"]

                class Mixin(models.AbstractModel):
                    _name = "mail.mixin"
            """,
        },
    )
    addons = tmp_path / "addons"
    extend = 'class P(models.Model):\n    _inherit = "res.partner"\n'
    for name, depends in {"left": [], "right": [], "top": ["left", "right"]}.items():
        _addon(addons, name, depends, {"__init__.py": "from . import m\n", "m.py": extend})
    _addon(addons, "unimported", [], {"__init__.py": "", "m.py": extend})
    return Registry(Settings(odoo_path=odoo, addons_paths=(addons,)))


def test_scan_follows_relative_imports_and_uses_core_prefix(registry: Registry) -> None:
    contribution = registry.contribution("odoo.addons.base.models.partner.Partner")

    assert contribution is not None
    assert contribution.model == "res.partner"
    assert contribution.parents == ("mail.mixin",)


def test_scan_ignores_files_not_imported_by_init(registry: Registry) -> None:
    assert registry.contribution("unimported.m.P") is None


def test_bases_of_new_model_are_its_mixins(registry: Registry) -> None:
    partner = registry.contribution("odoo.addons.base.models.partner.Partner")
    assert partner is not None

    assert [base.fullname for base in registry.bases(partner)] == [
        "odoo.addons.base.models.partner.Mixin"
    ]


def test_bases_of_extension_are_previous_classes_of_the_model(registry: Registry) -> None:
    left = registry.contribution("left.m.P")
    assert left is not None

    assert [base.fullname for base in registry.bases(left)] == [
        "odoo.addons.base.models.partner.Partner"
    ]


def test_model_tips_merges_unrelated_extensions(registry: Registry) -> None:
    tips = registry.model_tips("res.partner", "top")

    assert [tip.fullname for tip in tips] == ["top.m.P"]
    top = tips[0]
    assert sorted(base.fullname for base in registry.bases(top)) == ["left.m.P", "right.m.P"]


def test_model_tips_respects_dependency_view(registry: Registry) -> None:
    assert [tip.fullname for tip in registry.model_tips("res.partner", "left")] == ["left.m.P"]
    assert [tip.fullname for tip in registry.model_tips("res.partner", "base")] == [
        "odoo.addons.base.models.partner.Partner"
    ]
    assert registry.model_tips("res.partner", "unimported") == (
        registry.model_tips("res.partner", "base")
    )
