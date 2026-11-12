"""Unit tests of the addon scans kept on disk (roadmap [21])."""

from __future__ import annotations

from pathlib import Path

from pytest_mock import MockerFixture
from test_fingerprint import MODEL, _odoo, _touch

from odoo_typing.records import RecordStore, index_key, load_index
from odoo_typing.registry import Registry
from odoo_typing.registry import scans as registry_scans
from odoo_typing.scan_store import ScanStore, decode, encode
from odoo_typing.scanner import AddonScan, scan_addon

DATA = '<odoo><record id="thing" model="x.base"/></odoo>'
RICH = (
    "from odoo import api, fields, models\n\nSTATES = [('a', 'A')]\n\n"
    'class Thing(models.Model):\n    _name = "x.base"\n'
    '    _inherits = {"x.other": "other_id"}\n'
    "    state = fields.Selection(STATES)\n"
    '    other_id = fields.Many2one("x.other", delegate=True)\n'
    '    def f(self):\n        if "x.opt" in self.env:\n            pass\n'
)


def _registry(settings, store: RecordStore) -> Registry:  # type: ignore[no-untyped-def]
    return Registry(
        settings, lambda: load_index(settings, store), ScanStore(store, index_key(settings))
    )


def test_encode_decode_round_trip_of_a_real_scan(tmp_path: Path) -> None:
    settings = _odoo(tmp_path)
    base = settings.odoo_path / "addons/base"
    (base / "models.py").write_text(RICH)
    (base / "data.xml").write_text(DATA)
    (base / "__manifest__.py").write_text("{'depends': [], 'data': ['data.xml']}")
    (base / "broken.py").write_text("x = (\n")
    (base / "__init__.py").write_text("from . import models, broken\n")
    index, _ = load_index(settings, RecordStore(None))
    scan = scan_addon(index.addons["base"], index.load_key("base"))

    decoded = decode(encode(scan))

    assert isinstance(decoded, AddonScan)
    assert (decoded.modules, decoded.data) == (scan.modules, scan.data)
    assert scan.modules[0].guards and scan.data.xml_ids  # the round trip covers them
    assert [(e.path, e.reason) for e in decoded.errors] == [(e.path, e.reason) for e in scan.errors]


def test_registry_reads_unchanged_scans_back_and_rescans_edited_addon(
    tmp_path: Path, mocker: MockerFixture
) -> None:
    settings = _odoo(tmp_path)
    store = RecordStore(tmp_path / "cache")
    first = _registry(settings, store)
    expected = first.fields("x.my_mod", "my_mod")
    scan = mocker.spy(registry_scans, "scan_addon")

    second = _registry(settings, store)
    assert second.fields("x.my_mod", "my_mod") == expected
    assert scan.call_count == 0
    _touch(settings.odoo_path / "addons/my_mod/models.py", MODEL.replace("x.a", "x.my_mod2"))

    third = _registry(settings, store)
    third.fields("x.base", "my_mod")

    assert [call.args[0].name for call in scan.call_args_list] == ["my_mod"]
    assert third.view_class("x.my_mod2", "my_mod") is not None
