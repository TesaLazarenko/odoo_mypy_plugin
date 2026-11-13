"""Unit tests of the addon scans kept on disk (roadmap [21])."""

from __future__ import annotations

from pathlib import Path

from pytest_mock import MockerFixture
from test_fingerprint import MODEL, _odoo, _touch

from odoo_typing.records import RecordStore, load_index, package_source_hash
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
    return Registry(settings, lambda: load_index(settings, store), ScanStore(store))


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


def test_package_source_hash_covers_subpackages(tmp_path: Path) -> None:
    package = tmp_path / "pkg"
    (package / "sub").mkdir(parents=True)
    (package / "__init__.py").write_text("")
    (package / "sub" / "module.py").write_text("A = 1\n")
    before = package_source_hash(package)

    (package / "sub" / "module.py").write_text("A = 2\n")
    changed = package_source_hash(package)
    (package / "sub" / "module.py").rename(package / "module.py")
    moved = package_source_hash(package)

    assert len({before, changed, moved}) == 3


def test_shared_store_spares_a_new_mypy_cache_the_scans(
    tmp_path: Path, mocker: MockerFixture
) -> None:
    settings = _odoo(tmp_path)
    shared = RecordStore(tmp_path / "shared")
    first = Registry(settings, scans=ScanStore(RecordStore(tmp_path / "cache1"), shared))
    expected = first.fields("x.my_mod", "my_mod")
    scan = mocker.spy(registry_scans, "scan_addon")

    second = Registry(settings, scans=ScanStore(RecordStore(tmp_path / "cache2"), shared))

    assert second.fields("x.my_mod", "my_mod") == expected
    assert scan.call_count == 0
    assert not (tmp_path / "cache1").exists()  # written to the shared store only


def test_unwritable_shared_store_falls_back_to_mypy_cache(tmp_path: Path) -> None:
    settings = _odoo(tmp_path)
    (tmp_path / "file").write_text("")
    local = RecordStore(tmp_path / "cache")
    shared = RecordStore(tmp_path / "file" / "shared")  # under a file: cannot be created
    registry = Registry(settings, scans=ScanStore(local, shared))
    registry.fields("x.my_mod", "my_mod")
    addon = registry.addons.addons["my_mod"]

    found = ScanStore(local, shared).load(addon, registry.addons.load_key("my_mod"))

    assert found is not None
    records = [path.name for path in (tmp_path / "cache").iterdir()]
    assert any(name.startswith("addon-scan-my_mod-") for name in records)


def test_unreached_files_read_back_until_they_change(tmp_path: Path, mocker: MockerFixture) -> None:
    settings = _odoo(tmp_path)
    test_file = settings.odoo_path / "addons/my_mod/tests/test_x.py"
    test_file.parent.mkdir()
    test_file.write_text('def f(env):\n    if "x.opt" in env:\n        env["x.base"]\n')
    store = RecordStore(tmp_path / "cache")
    first = _registry(settings, store).referenced_models(test_file)
    parse = mocker.spy(registry_scans, "parse_bytes")

    second = _registry(settings, store)
    again = second.referenced_models(test_file)
    guarded = second.guarded(test_file, "x.opt", 3)
    read_back = parse.call_count
    _touch(test_file, 'def f(env):\n    env["x.other"]\n')
    changed = _registry(settings, store).referenced_models(test_file)

    assert (first, again, guarded, read_back) == ({"x.base"}, {"x.base"}, True, 0)
    assert changed == {"x.other"}


def test_unreached_file_annotations_read_back(tmp_path: Path, mocker: MockerFixture) -> None:
    settings = _odoo(tmp_path)
    test_file = settings.odoo_path / "addons/my_mod/tests/test_y.py"
    test_file.parent.mkdir()
    test_file.write_text('VALS: "odoo.values.x_base" = {}\n')
    store = RecordStore(tmp_path / "cache")
    first = _registry(settings, store).annotation_aliases(test_file)
    parse = mocker.spy(registry_scans, "parse_bytes")

    again = _registry(settings, store).annotation_aliases(test_file)

    assert (first, again, parse.call_count) == ({("values", "x_base")},) * 2 + (0,)
