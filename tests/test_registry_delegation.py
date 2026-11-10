"""Unit tests of what the scan and the registry know about ``_inherits`` (no mypy run)."""

from __future__ import annotations

import ast
from pathlib import Path

from test_registry_mro import BASE, _cls, _files, _short
from test_registry_semantics import _build, _get

from odoo_typing.registry import Registry
from odoo_typing.scanner import referenced_models

PARENT = _cls("Parent", "t.parent", body="    color = fields.Integer()\n    plain = 1\n")
CHILD = _cls(
    "Child",
    "t.child",
    body=(
        '    _inherits = {"t.parent": "parent_id"}\n'
        '    parent_id = fields.Many2one("t.parent")\n'
        "    size: int = fields.Integer()\n"
        '    holder_id = fields.Many2one("t.holder", delegate=True)\n'
    ),
)
HOLDER = _cls("Holder", "t.holder", body="    label = fields.Char()\n")


def _registry(tmp_path: Path) -> Registry:
    return _build(
        tmp_path,
        base=BASE,
        addons={
            "t_core": ([], _files(PARENT + HOLDER + CHILD)),
            "t_ext": (["t_core"], _files(_cls("Parent", inherit=["t.parent"]))),
        },
    )


def test_scan_inherits_and_delegate_fields_are_delegates(tmp_path: Path) -> None:
    registry = _registry(tmp_path)

    child = _get(registry, "t_core.m.Child")

    assert child.delegates == ("t.parent", "t.holder")
    assert child.fields == ("parent_id", "size", "holder_id")
    assert _get(registry, "t_core.m.Parent").fields == ("color",)


def test_delegated_models_merge_chain_and_skip_own_model(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    child = _get(registry, "t_core.m.Child")

    delegates = registry.delegated_models(registry.linearization(child), "t.child")

    assert delegates == ("t.parent", "t.holder")
    assert registry.delegated_models(registry.linearization(child), "t.parent") == ("t.holder",)


def test_delegate_chain_is_the_view_of_the_reading_addon(tmp_path: Path) -> None:
    registry = _registry(tmp_path)

    assert _short(registry.delegate_chain("t.parent", "t_ext")) == [
        "t_ext.Parent",
        "t_core.Parent",
        "base.Base",
    ]
    assert registry.delegate_chain("t.unknown", "t_ext") == ()


def test_view_class_delegate_extended_outside_tips_view_needs_a_merged_class(
    tmp_path: Path,
) -> None:
    registry = _registry(tmp_path)

    ext_view = registry.view_class("t.child", "t_ext")
    core_view = registry.view_class("t.child", "t_core")

    assert ext_view is not None and core_view is not None
    assert ext_view.tip is None
    assert core_view.tip == _get(registry, "t_core.m.Child")


def test_view_modules_include_delegated_models(tmp_path: Path) -> None:
    registry = _registry(tmp_path)

    assert registry.view_modules("t.child", "t_ext") == frozenset(
        {"t_core.m", "t_ext.m", "odoo.addons.base.m"}
    )
    assert "t_ext.m" not in registry.class_modules(_get(registry, "t_core.m.Child"))


def test_addons_outside_defining_finds_definers_outside_the_closure(tmp_path: Path) -> None:
    registry = _registry(tmp_path)

    assert registry.addons_outside_defining("t.parent", "base") == ("t_core",)
    assert registry.addons_outside_defining("t.parent", "t_ext") == ()
    assert registry.addons_outside_defining("t.nowhere", "t_ext") == ()


def test_referenced_models_final_constants_and_env_records_are_found() -> None:
    source = (
        "from typing import Final\n"
        'MODEL: Final = "x.final"\n'
        'PLAIN = "x.plain"\n'
        "env[MODEL]\nenv[PLAIN]\n"
        "self.env.user\nenv.companies\nself.company\n"
    )

    models = referenced_models(ast.parse(source))

    assert models == {"x.final", "res.users", "res.company"}
