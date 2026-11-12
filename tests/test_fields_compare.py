"""Unit tests for ``tools/fields/compare.py --merged`` (no Odoo run, synthetic dump)."""

from __future__ import annotations

import textwrap
from pathlib import Path

import compare

from odoo_typing.config import Settings
from odoo_typing.registry import Registry


def _registry(root: Path) -> Registry:
    odoo = root / "odoo"
    base = odoo / "addons" / "base"
    base.mkdir(parents=True)
    (odoo / "release.py").write_text("")
    (base / "__manifest__.py").write_text(repr({"name": "base", "depends": []}))
    (base / "__init__.py").write_text("from . import partner\n")
    (base / "partner.py").write_text(
        textwrap.dedent(
            """
            from odoo import fields, models

            class Partner(models.Model):
                _name = "res.partner"
                name = fields.Char()
                parent_id = fields.Many2one("res.partner")
                kind = fields.Selection([("a", "A"), ("b", "B")])
                total = fields.Float(compute="_compute_total")
            """
        )
    )
    return Registry(Settings(odoo_path=odoo, addons_paths=()))


def _dump(registry: Registry) -> dict[str, object]:
    """A dump that agrees with the plugin on every field of every model."""
    models = {
        model: {
            name: {
                "type": info.field_type,
                **({"comodel_name": info.comodel} if info.comodel else {}),
                **({"related": info.related} if info.related else {}),
                "store": compare.stored(info),
                **({"selection": list(info.selection)} if info.selection else {}),
            }
            for name, info in registry.fields(model, "base").items()
        }
        for model in registry.models("base")
    }
    return {"base": {"addons": ["base"], "models": models}}


def test_compare_merged_agreeing_dump_reports_no_differences(tmp_path: Path) -> None:
    registry = _registry(tmp_path)

    report = compare.compare_merged(_dump(registry), registry)

    assert report.counts["models"] == 1
    assert report.counts["fields"] == len(registry.fields("res.partner", "base"))
    assert {kind for kind in report.counts if "differs" in kind or "only" in kind} == set()


def test_compare_merged_reports_each_kind_of_difference(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    dump = _dump(registry)
    models = dump["base"]["models"]  # type: ignore[index]
    partner = models["res.partner"]
    del partner["name"]
    partner["parent_id"]["comodel_name"] = "res.users"
    partner["kind"]["selection"] = ["a", "b", "c"]
    partner["total"]["store"] = True
    partner["extra"] = {"type": "Char", "store": True}
    models["res.users"] = {}

    report = compare.compare_merged(dump, registry)

    assert report.examples["field only in plugin"] == ["base: res.partner.name"]
    assert report.examples["field missing in plugin"] == ["base: res.partner.extra"]
    assert report.examples["model missing in plugin"] == ["base: res.users"]
    assert report.examples["comodel_name differs"] == [
        "base: res.partner.parent_id: 'res.partner' != 'res.users'"
    ]
    assert report.counts["selection differs"] == 1
    assert report.examples["store differs"] == ["base: res.partner.total: False != True"]


def test_compare_merged_dynamic_runtime_value_is_counted_apart(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    dump = _dump(registry)
    dump["base"]["models"]["res.partner"]["kind"]["selection"] = "<dynamic>"  # type: ignore[index]

    report = compare.compare_merged(dump, registry)

    assert report.counts["dynamic at runtime selection"] == 1
    assert "selection differs" not in report.counts
