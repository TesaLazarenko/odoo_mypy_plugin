"""Unit tests for addon discovery, scanning and the model registry (no mypy run)."""

from __future__ import annotations

import ast
import textwrap
from collections.abc import Mapping
from pathlib import Path

import pytest
from pytest_mock import MockerFixture

from odoo_typing import addons as addons_module
from odoo_typing.config import Settings
from odoo_typing.registry import Registry


def _addon(root: Path, name: str, depends: list[str], files: Mapping[str, str | bytes]) -> None:
    path = root / name
    path.mkdir(parents=True)
    (path / "__manifest__.py").write_text(repr({"name": name, "depends": depends}))
    for relative, source in files.items():
        target = path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(source, bytes):
            target.write_bytes(source)
        else:
            target.write_text(textwrap.dedent(source))


def _odoo(root: Path) -> Path:
    odoo = root / "odoo"
    (odoo / "addons").mkdir(parents=True)
    (odoo / "release.py").write_text("")
    _addon(odoo / "addons", "base", [], {"__init__.py": ""})
    return odoo


def _registry(root: Path, addons: Mapping[str, Mapping[str, str | bytes]]) -> Registry:
    """Registry over an empty ``base`` plus the given addons (each depending on base)."""
    odoo = _odoo(root)
    (root / "addons").mkdir()
    for name, files in addons.items():
        _addon(root / "addons", name, [], files)
    return Registry(Settings(odoo_path=odoo, addons_paths=(root / "addons",)))


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


# -- source files are looked up by resolved path, parsed at most once ([19], [49]) ----------

REFS = """
    class P(models.Model):
        _inherit = "res.partner"
        x = fields.Many2one("res.users")
"""


def test_referenced_models_symlinked_path_reuses_scan_without_parsing(
    registry: Registry, tmp_path: Path, mocker: MockerFixture
) -> None:
    registry.model_tips("res.partner", "left")  # scans "left"
    (tmp_path / "link").symlink_to(tmp_path / "addons")
    parse = mocker.spy(ast, "parse")

    models = registry.referenced_models(tmp_path / "link" / "left" / "m.py")

    assert (models, parse.call_count) == (frozenset(), 0)


def test_referenced_models_relative_path_reuses_scan_without_parsing(
    registry: Registry, tmp_path: Path, mocker: MockerFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    registry.model_tips("res.partner", "left")
    monkeypatch.chdir(tmp_path)
    parse = mocker.spy(ast, "parse")

    registry.referenced_models("addons/left/m.py")

    assert parse.call_count == 0


def test_referenced_models_file_of_unscanned_addon_scans_it_once(
    tmp_path: Path, mocker: MockerFixture
) -> None:
    registry = _registry(tmp_path, {"refs": {"__init__.py": "from . import m\n", "m.py": REFS}})
    parse = mocker.spy(ast, "parse")

    models = registry.referenced_models(tmp_path / "addons" / "refs" / "m.py")

    assert models == frozenset({"res.users"})
    calls = parse.call_args_list
    sources = [call.kwargs["filename"] for call in calls if "filename" in call.kwargs]
    assert sorted(sources) == sorted(set(sources))  # manifests are parsed without a name


def test_referenced_models_file_not_imported_by_addon_parses_it(tmp_path: Path) -> None:
    registry = _registry(tmp_path, {"refs": {"__init__.py": "", "tests/test_x.py": REFS}})

    models = registry.referenced_models(tmp_path / "addons" / "refs" / "tests" / "test_x.py")

    assert models == frozenset({"res.users"})


# -- repeated model_tips queries are answered from a cache ([20]) ---------------------------


def test_model_tips_repeated_query_answered_from_cache(
    registry: Registry, mocker: MockerFixture
) -> None:
    first = registry.model_tips("res.partner", "top")
    compute = mocker.spy(registry, "_compute_tips")

    second = registry.model_tips("res.partner", "top")

    assert (second, compute.call_count) == (first, 0)


# -- manifests are read on first use, not when the registry is created ([23]) ---------------


def test_registry_init_reads_no_manifest(tmp_path: Path, mocker: MockerFixture) -> None:
    read_depends = mocker.spy(addons_module, "read_depends")
    registry = _registry(tmp_path, {})
    assert read_depends.call_count == 0

    registry.model_tips("res.partner", "base")

    assert read_depends.call_count > 0


# -- sources are parsed as bytes; unparsable files are reported ([51]) ----------------------

THING = 'class Thing(models.Model):\n    _name = "x.thing"\n'


@pytest.mark.parametrize(
    "source",
    [
        b"\xef\xbb\xbf" + THING.encode(),
        b"# -*- coding: latin-1 -*-\n" + THING.encode() + b"    _description = 'Caf\xe9'\n",
    ],
    ids=["utf8-bom", "latin1-cookie"],
)
def test_scan_bom_or_coding_cookie_file_registers_model(tmp_path: Path, source: bytes) -> None:
    registry = _registry(tmp_path, {"enc": {"__init__.py": "from . import m\n", "m.py": source}})

    tips = registry.model_tips("x.thing", "enc")

    assert [tip.fullname for tip in tips] == ["enc.m.Thing"]


def test_scan_syntax_error_warns_with_file_and_scans_the_rest(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    files: dict[str, str | bytes] = {
        "__init__.py": "from . import broken, m\n",
        "broken.py": "x = (\n",
        "m.py": THING,
    }
    registry = _registry(tmp_path, {"enc": files})

    tips = registry.model_tips("x.thing", "enc")

    assert [tip.fullname for tip in tips] == ["enc.m.Thing"]
    err = capsys.readouterr().err
    assert str(tmp_path / "addons" / "enc" / "broken.py") in err
    assert "line 1" in err
    assert err.count("broken.py") == 1


def test_addon_for_path_symlinked_addon_dir_finds_addon(tmp_path: Path) -> None:
    registry = _registry(tmp_path, {})
    _addon(tmp_path / "elsewhere", "linked", [], {"__init__.py": "", "models/m.py": ""})
    (tmp_path / "addons" / "linked").symlink_to(tmp_path / "elsewhere" / "linked")

    addon = registry.addons.addon_for_path(tmp_path / "addons" / "linked" / "models" / "m.py")

    assert addon is not None
    assert (addon.name, addon.path) == ("linked", (tmp_path / "elsewhere" / "linked").resolve())


def test_addon_for_path_file_outside_addons_returns_none(tmp_path: Path) -> None:
    registry = _registry(tmp_path, {"mod": {"__init__.py": ""}})
    (tmp_path / "scripts").mkdir()

    assert registry.addon_name_for_path(tmp_path / "scripts" / "tool.py") is None
    assert registry.addon_name_for_path(tmp_path / "odoo" / "http.py") is None
