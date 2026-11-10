"""Unit tests for the two module spellings of custom addons (``<name>``, ``odoo.addons.<name>``)."""

from __future__ import annotations

from pathlib import Path

import pytest
from test_registry import _addon, _odoo

from odoo_typing.addons import AddonIndex
from odoo_typing.config import Settings
from odoo_typing.registry import Registry

THING = 'from odoo import models\n\nclass Thing(models.Model):\n    _name = "x.thing"\n'


def _settings(root: Path, overlay: bool = False) -> Settings:
    odoo = _odoo(root)
    _addon(root / "addons", "my_mod", [], {"__init__.py": "from . import m\n", "m.py": THING})
    return Settings(
        odoo_path=odoo,
        addons_paths=(root / "addons",),
        overlay_root=root / ".odoo-root" if overlay else None,
    )


def _index(settings: Settings) -> AddonIndex:
    return Registry(settings).addons


@pytest.mark.parametrize("module", ["my_mod.m", "odoo.addons.my_mod.m", "my_mod"])
def test_addon_for_module_custom_addon_either_spelling_returns_addon(
    tmp_path: Path, module: str
) -> None:
    index = _index(_settings(tmp_path))

    addon = index.addon_for_module(module)

    assert addon is not None
    assert addon.name == "my_mod"


def test_addon_for_module_core_addon_bare_name_returns_none(tmp_path: Path) -> None:
    index = _index(_settings(tmp_path))

    assert index.addon_for_module("base.models") is None
    assert index.addon_for_module("odoo.addons.base.models") is index.addons["base"]


def test_addon_index_without_overlay_custom_prefix_is_bare_name(tmp_path: Path) -> None:
    index = _index(_settings(tmp_path))

    assert index.addons["my_mod"].module_prefix == "my_mod"
    assert index.addons["base"].module_prefix == "odoo.addons.base"


def test_addon_index_with_overlay_custom_prefix_is_odoo_addons(tmp_path: Path) -> None:
    index = _index(_settings(tmp_path, overlay=True))

    assert index.addons["my_mod"].module_prefix == "odoo.addons.my_mod"


@pytest.mark.parametrize(
    ("overlay", "module", "expected"),
    [
        (False, "odoo.addons.my_mod.m", "my_mod.m"),
        (False, "my_mod.m", "my_mod.m"),
        (True, "my_mod.m", "odoo.addons.my_mod.m"),
        (True, "my_mod", "odoo.addons.my_mod"),
        (True, "odoo.addons.base.models", "odoo.addons.base.models"),
        (True, "scripts.tool", "scripts.tool"),
    ],
)
def test_canonical_module_either_spelling_returns_configured_spelling(
    tmp_path: Path, overlay: bool, module: str, expected: str
) -> None:
    index = _index(_settings(tmp_path, overlay=overlay))

    assert index.canonical_module(module) == expected


@pytest.mark.parametrize("overlay", [False, True])
def test_contribution_either_spelling_returns_same_contribution(
    tmp_path: Path, overlay: bool
) -> None:
    registry = Registry(_settings(tmp_path, overlay=overlay))

    bare = registry.contribution("my_mod.m.Thing")
    canonical = registry.contribution("odoo.addons.my_mod.m.Thing")

    assert bare is not None
    assert bare is canonical
    prefix = "odoo.addons.my_mod" if overlay else "my_mod"
    assert bare.fullname == f"{prefix}.m.Thing"


def test_misnamed_alias_spelling_warns_once_per_addon(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    registry = Registry(_settings(tmp_path, overlay=True))

    first = registry.misnamed("my_mod.m")
    second = registry.misnamed("my_mod")

    assert (first, second) == (True, True)
    err = capsys.readouterr().err
    assert err.count("odoo-typing: warning") == 1
    assert '"my_mod.m"' in err
    assert '"odoo.addons.my_mod"' in err


@pytest.mark.parametrize("module", ["odoo.addons.my_mod.m", "odoo.addons.base", "tool", "odoo"])
def test_misnamed_configured_spelling_or_other_module_returns_false(
    tmp_path: Path, module: str, capsys: pytest.CaptureFixture[str]
) -> None:
    registry = Registry(_settings(tmp_path, overlay=True))

    assert registry.misnamed(module) is False
    assert capsys.readouterr().err == ""


def test_dependency_modules_with_overlay_uses_odoo_addons_names(tmp_path: Path) -> None:
    settings = _settings(tmp_path, overlay=True)
    _addon(
        tmp_path / "addons",
        "ext",
        ["my_mod"],
        {"__init__.py": "from . import e\n", "e.py": 'env["x.thing"]\n'},
    )
    registry = Registry(settings)
    overlay_file = tmp_path / ".odoo-root" / "odoo" / "addons" / "ext" / "e.py"
    overlay_file.parent.parent.mkdir(parents=True)
    overlay_file.parent.symlink_to(tmp_path / "addons" / "ext")

    modules = registry.dependency_modules("odoo.addons.ext.e", overlay_file)

    assert "odoo.addons.my_mod.m" in modules
    assert not any(name.startswith("my_mod") for name in modules)
