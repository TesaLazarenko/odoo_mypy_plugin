"""Unit tests of the addon scans kept on disk (roadmap [21])."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from pytest_mock import MockerFixture
from test_fingerprint import MODEL, _odoo, _touch

from odoo_typing.records import RecordStore, load_index, package_source_hash
from odoo_typing.registry import Registry
from odoo_typing.registry import scans as registry_scans
from odoo_typing.scan_store import DecodeError, ScanStore, decode, encode
from odoo_typing.scanner import AddonScan, scan_addon, walker

NOT_ROOT = pytest.mark.skipif(os.geteuid() == 0, reason="root reads any file")

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
    assert [(error.path, error.reason) for error in decoded.errors] == [
        (error.path, error.reason) for error in scan.errors
    ]


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


def test_registry_file_saved_while_scanned_is_scanned_again(
    tmp_path: Path, mocker: MockerFixture
) -> None:
    settings = _odoo(tmp_path)
    store = RecordStore(tmp_path / "cache")
    models = (settings.odoo_path / "addons/my_mod/models.py").resolve()
    read = walker.read_source

    def read_then_saved(path: Path) -> bytes:
        source = read(path)
        if path == models:  # saved by the editor right after the scan read it
            _touch(models, MODEL.replace("x.a", "x.my_mod_v2"))
        return source

    saving = mocker.patch.object(walker, "read_source", side_effect=read_then_saved)
    first = _registry(settings, store)
    first.fields("x.my_mod", "my_mod")
    changed = first.inputs_changed()
    mocker.stop(saving)

    second = _registry(settings, store)

    assert changed is True
    assert second.view_class("x.my_mod_v2", "my_mod") is not None


def test_registry_manifest_data_list_edited_rescans_addon(tmp_path: Path) -> None:
    settings = _odoo(tmp_path)
    addon = settings.odoo_path / "addons/my_mod"
    (addon / "a.xml").write_text('<odoo><record id="first" model="x.my_mod"/></odoo>')
    (addon / "b.xml").write_text('<odoo><record id="second" model="x.my_mod"/></odoo>')
    manifest = addon / "__manifest__.py"
    _touch(manifest, "{'depends': ['base'], 'data': ['a.xml']}")
    store = RecordStore(tmp_path / "cache")
    first = set(_registry(settings, store).xml_ids("my_mod"))
    _touch(manifest, "{'depends': ['base'], 'data': ['a.xml', 'b.xml']}")  # saved in place

    second = set(_registry(settings, store).xml_ids("my_mod"))

    assert "my_mod.second" not in first
    assert "my_mod.second" in second


def test_registry_package_init_added_to_imported_directory_rescans_addon(
    tmp_path: Path,
) -> None:
    settings = _odoo(tmp_path)
    addon = settings.odoo_path / "addons/my_mod"
    (addon / "wizard").mkdir()
    (addon / "wizard/w.py").write_text(MODEL.replace("x.a", "x.wizard"))
    _touch(addon / "__init__.py", "from . import models, wizard\n")
    store = RecordStore(tmp_path / "cache")
    first = _registry(settings, store).view_class("x.wizard", "my_mod")
    (addon / "wizard/__init__.py").write_text("from . import w\n")
    os.utime(addon / "wizard", ns=(10**18, 10**18))  # a new stamp even within the clock

    second = _registry(settings, store).view_class("x.wizard", "my_mod")

    assert first is None
    assert second is not None


def test_registry_records_of_another_interpreter_are_not_reused(
    tmp_path: Path, mocker: MockerFixture
) -> None:
    settings = _odoo(tmp_path)

    def registry() -> Registry:
        local = RecordStore.for_cache_dir(str(tmp_path / "cache"))
        return Registry(settings, scans=ScanStore(local, RecordStore.shared(tmp_path / "shared")))

    other = mocker.patch("odoo_typing.records.interpreter_tag", return_value="cpython-399")
    registry().fields("x.my_mod", "my_mod")
    mocker.stop(other)
    scan = mocker.spy(registry_scans, "scan_addon")

    registry().fields("x.my_mod", "my_mod")
    scanned = scan.call_count
    registry().fields("x.my_mod", "my_mod")

    assert (scanned, scan.call_count) == (2, 2)  # base and my_mod, once
    names = {path.name for path in (tmp_path / "shared").iterdir()}
    assert names == {"cpython-399", sys.implementation.cache_tag}


@NOT_ROOT
def test_registry_unreadable_file_is_scanned_again_once_readable(tmp_path: Path) -> None:
    settings = _odoo(tmp_path)
    models = settings.odoo_path / "addons/my_mod/models.py"
    store = RecordStore(tmp_path / "cache")
    models.chmod(0)
    try:
        first = _registry(settings, store).view_class("x.my_mod", "my_mod")
    finally:
        models.chmod(0o644)  # a permission fix: the same mtime and size

    second = _registry(settings, store).view_class("x.my_mod", "my_mod")

    assert first is None
    assert second is not None


@NOT_ROOT
def test_registry_unsearchable_package_is_reported_and_scanned_again(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    settings = _odoo(tmp_path)
    addon = settings.odoo_path / "addons/my_mod"
    (addon / "wizard").mkdir()
    (addon / "wizard/__init__.py").write_text(MODEL.replace("x.a", "x.wizard"))
    _touch(addon / "__init__.py", "from . import models, wizard\n")
    store = RecordStore(tmp_path / "cache")
    (addon / "wizard").chmod(0o600)  # listed, but its files cannot be looked up
    try:
        first = _registry(settings, store).view_class("x.wizard", "my_mod")
    finally:
        (addon / "wizard").chmod(0o755)
    warning = capsys.readouterr().err

    second = _registry(settings, store).view_class("x.wizard", "my_mod")

    assert first is None
    assert "wizard/__init__.py: Permission denied" in warning
    assert second is not None


@NOT_ROOT
def test_registry_unreadable_data_file_is_read_again_once_readable(tmp_path: Path) -> None:
    settings = _odoo(tmp_path)
    addon = settings.odoo_path / "addons/my_mod"
    data = addon / "a.xml"
    data.write_text('<odoo><record id="first" model="x.my_mod"/></odoo>')
    _touch(addon / "__manifest__.py", "{'depends': ['base'], 'data': ['a.xml']}")
    store = RecordStore(tmp_path / "cache")
    data.chmod(0)
    try:
        first = set(_registry(settings, store).xml_ids("my_mod"))
    finally:
        data.chmod(0o644)

    second = set(_registry(settings, store).xml_ids("my_mod"))

    assert "my_mod.first" not in first
    assert "my_mod.first" in second


def _nested(depth: int) -> object:
    value: object = []
    for _ in range(depth):
        value = [value]
    return value


@pytest.mark.parametrize(
    "value",
    [{"$set": [[1]]}, {"$dict": [[]]}, {"$dict": [[[1], 2]]}],
    ids=["unhashable-member", "short-pair", "unhashable-key"],
)
def test_decode_malformed_value_raises_decode_error(value: object) -> None:
    with pytest.raises(DecodeError):
        decode(value)


def test_decode_value_nested_beyond_recursion_limit_raises_decode_error() -> None:
    # Deeper than the limit of the moment: a mypy run in this process raises it (2**14).
    value = _nested(sys.getrecursionlimit() + 100)

    with pytest.raises(DecodeError):
        decode(value)


def test_scan_store_record_of_another_shape_is_a_miss(tmp_path: Path) -> None:
    settings = _odoo(tmp_path)
    store = RecordStore(tmp_path / "cache")
    registry = _registry(settings, store)
    registry.fields("x.my_mod", "my_mod")
    index = registry.addons
    key = next(path.name for path in (tmp_path / "cache").glob("addon-scan-my_mod-*"))
    record = store.load(key)
    assert record is not None
    store.save(key, {**record, "scan": {"$set": [[1]]}})  # this version, another shape

    found = ScanStore(store).load(index.addons["my_mod"], index.load_key("my_mod"))

    assert found is None


def test_load_index_manifest_edited_beside_symlink_loop_reads_index_again(
    tmp_path: Path,
) -> None:
    settings = _odoo(tmp_path)
    (settings.odoo_path / "addons/loop").symlink_to("loop")  # a link to itself
    store = RecordStore(tmp_path / "cache")
    load_index(settings, store)
    _touch(settings.odoo_path / "addons/my_mod/__manifest__.py", "{'depends': []}")

    index, _ = load_index(settings, store)

    assert index.addons["my_mod"].depends == ()


def test_package_source_hash_file_that_cannot_be_read_is_hashed_by_its_path(
    tmp_path: Path,
) -> None:
    package = tmp_path / "pkg"
    package.mkdir()
    (package / "a.py").write_text("X = 1\n")
    before = package_source_hash(package)
    (package / ".#a.py").symlink_to("user@host.4242:1700000000")  # an Emacs lock: dangling

    locked = package_source_hash(package)

    assert locked != before


@NOT_ROOT
def test_registry_still_unreadable_file_reuses_the_stored_scan(
    tmp_path: Path, mocker: MockerFixture, capsys: pytest.CaptureFixture[str]
) -> None:
    settings = _odoo(tmp_path)
    models = settings.odoo_path / "addons/my_mod/models.py"
    store = RecordStore(tmp_path / "cache")
    models.chmod(0)
    try:
        _registry(settings, store).view_class("x.my_mod", "my_mod")
        scan = mocker.spy(registry_scans, "scan_addon")
        capsys.readouterr()
        kept = _registry(settings, store).view_class("x.my_mod", "my_mod")
    finally:
        models.chmod(0o644)

    assert kept is None
    assert scan.call_count == 0
    assert "models.py: Permission denied" in capsys.readouterr().err  # still told
