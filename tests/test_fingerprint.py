"""Unit tests of the stamps and stored records behind ``report_config_data``."""

from __future__ import annotations

import dataclasses
import os
from pathlib import Path

import pytest
from pytest_mock import MockerFixture

from odoo_typing import records
from odoo_typing.config import Settings
from odoo_typing.fingerprint import Fingerprints
from odoo_typing.records import RecordStore, load_index
from odoo_typing.registry import Registry
from odoo_typing.registry import scans as registry_scans
from odoo_typing.stamps import index_stamps, stamp, unchanged

MODEL = 'from odoo import models\n\nclass A(models.Model):\n    _name = "x.a"\n'
NOT_ROOT = pytest.mark.skipif(os.geteuid() == 0, reason="root reads every file")


def _odoo(tmp_path: Path) -> Settings:
    """A fake Odoo with addons ``base`` and ``my_mod`` (depending on base)."""
    odoo = tmp_path / "odoo"
    for name, depends in (("base", []), ("my_mod", ["base"])):
        addon = odoo / "addons" / name
        addon.mkdir(parents=True)
        (addon / "__manifest__.py").write_text(f"{{'depends': {depends!r}}}")
        (addon / "__init__.py").write_text("from . import models\n")
        (addon / "models.py").write_text(MODEL.replace("x.a", f"x.{name}"))
    (odoo / "release.py").write_text("")
    return Settings(odoo_path=odoo, addons_paths=())


def _touch(path: Path, text: str) -> None:
    """Rewrite a file with a later mtime (some file systems have a coarse clock)."""
    before = path.stat().st_mtime_ns
    path.write_text(text)
    os.utime(path, ns=(before + 10**9, before + 10**9))


def _fingerprints(settings: Settings, store: RecordStore) -> Fingerprints:
    registry = Registry(settings, lambda: load_index(settings, store))
    return Fingerprints(registry, store, settings)


def test_unchanged_file_edited_returns_false(tmp_path: Path) -> None:
    path = tmp_path / "a.py"
    path.write_text("x = 1\n")
    stamps = {str(path): stamp(path)}
    _touch(path, "x = 2\n")

    assert unchanged(stamps) is False


def test_index_stamps_manifest_created_in_plain_directory_changes(tmp_path: Path) -> None:
    (tmp_path / "addons" / "later").mkdir(parents=True)
    stamps = index_stamps([tmp_path / "addons"])
    (tmp_path / "addons" / "later" / "__manifest__.py").write_text("{}")
    os.utime(tmp_path / "addons" / "later", ns=(1, 1))

    assert unchanged(stamps) is False


def test_record_store_corrupt_record_loads_none(tmp_path: Path) -> None:
    store = RecordStore(tmp_path)
    store.save("r.json", {"a": 1})
    (tmp_path / "r.json").write_text("{not json")

    assert store.load("r.json") is None


def test_record_store_too_deeply_nested_record_loads_none(tmp_path: Path) -> None:
    store = RecordStore(tmp_path)
    (tmp_path / "r.json").write_text("[" * 200_000 + "]" * 200_000)

    assert store.load("r.json") is None


def test_record_store_without_directory_keeps_nothing(tmp_path: Path) -> None:
    store = RecordStore.for_cache_dir(os.devnull)
    store.save("r.json", {"a": 1})

    assert store.load("r.json") is None
    assert RecordStore.shared(None) is None  # scan_cache_dir = ""


def test_load_index_unchanged_roots_reuses_stored_index(
    tmp_path: Path, mocker: MockerFixture
) -> None:
    settings = _odoo(tmp_path)
    store = RecordStore(tmp_path / "cache")
    first, _ = load_index(settings, store)
    read = mocker.spy(records, "read_index")

    second, _ = load_index(settings, store)

    assert (read.call_count, second.addons) == (0, first.addons)


def test_load_index_manifest_edited_reads_index_again(tmp_path: Path) -> None:
    settings = _odoo(tmp_path)
    store = RecordStore(tmp_path / "cache")
    load_index(settings, store)
    _touch(settings.odoo_path / "addons/my_mod/__manifest__.py", "{'depends': []}")

    index, _ = load_index(settings, store)

    assert index.addons["my_mod"].depends == ()


def test_addon_digest_warm_store_parses_nothing(tmp_path: Path, mocker: MockerFixture) -> None:
    settings = _odoo(tmp_path)
    store = RecordStore(tmp_path / "cache")
    expected = _fingerprints(settings, store).addon_digest("my_mod")
    scan = mocker.spy(Registry, "scan_result")

    digest = _fingerprints(settings, store).addon_digest("my_mod")

    assert (digest, scan.call_count) == (expected, 0)


METHOD = "\n    def f(self) -> int:\n        return {}\n"


def test_addon_digest_method_body_edited_stays_equal(tmp_path: Path) -> None:
    settings = _odoo(tmp_path)
    store = RecordStore(tmp_path / "cache")
    models = settings.odoo_path / "addons/base/models.py"
    _touch(models, MODEL.replace("x.a", "x.base") + METHOD.format(1))
    expected = _fingerprints(settings, store).addon_digest("my_mod")
    _touch(models, MODEL.replace("x.a", "x.base") + METHOD.format(2))

    assert _fingerprints(settings, store).addon_digest("my_mod") == expected


GUARDED = '\n    def f(self):\n{}        if "x.other" in self.env:\n            return 1\n'


def test_addon_digest_guard_moved_in_dependency_stays_equal(tmp_path: Path) -> None:
    settings = _odoo(tmp_path)
    store = RecordStore(tmp_path / "cache")
    models = settings.odoo_path / "addons/base/models.py"
    _touch(models, MODEL.replace("x.a", "x.base") + GUARDED.format(""))
    expected = _fingerprints(settings, store).addon_digest("my_mod")
    _touch(models, MODEL.replace("x.a", "x.base") + GUARDED.format("        pass\n"))

    assert _fingerprints(settings, store).addon_digest("my_mod") == expected


def test_addon_digest_method_added_in_dependency_changes(tmp_path: Path) -> None:
    # compute="f" and the like are checked against the methods of the model.
    settings = _odoo(tmp_path)
    store = RecordStore(tmp_path / "cache")
    before = _fingerprints(settings, store).addon_digest("my_mod")
    source = MODEL.replace("x.a", "x.base") + METHOD.format(1)
    _touch(settings.odoo_path / "addons/base/models.py", source)

    assert _fingerprints(settings, store).addon_digest("my_mod") != before


def test_addon_digest_class_added_in_dependency_changes(tmp_path: Path) -> None:
    settings = _odoo(tmp_path)
    store = RecordStore(tmp_path / "cache")
    before = _fingerprints(settings, store).addon_digest("my_mod")
    source = MODEL.replace("x.a", "x.base") + MODEL.replace("A", "B").replace("x.a", "x.b")
    _touch(settings.odoo_path / "addons/base/models.py", source)

    assert _fingerprints(settings, store).addon_digest("my_mod") != before


def test_module_data_file_outside_addons_returns_none(tmp_path: Path) -> None:
    settings = _odoo(tmp_path)
    data = _fingerprints(settings, RecordStore(None)).module_data("tool", str(tmp_path / "t.py"))

    assert data is None


def test_module_data_addon_module_returns_addon_digest(tmp_path: Path) -> None:
    settings = _odoo(tmp_path)
    prints = _fingerprints(settings, RecordStore(None))
    path = str(settings.odoo_path / "addons/my_mod/models.py")

    data = prints.module_data("odoo.addons.my_mod.models", path)

    assert data is not None
    assert data.startswith(prints.addon_digest("my_mod") + "-")


def test_module_data_option_changed_returns_other_data(tmp_path: Path) -> None:
    settings = _odoo(tmp_path)
    path = str(settings.odoo_path / "addons/my_mod/models.py")
    base = dataclasses.replace(settings, dynamic_env="base")

    default = _fingerprints(settings, RecordStore(None)).module_data(
        "odoo.addons.my_mod.models", path
    )
    changed = _fingerprints(base, RecordStore(None)).module_data("odoo.addons.my_mod.models", path)

    assert default != changed


def test_registry_inputs_changed_scanned_file_edited_returns_true(tmp_path: Path) -> None:
    settings = _odoo(tmp_path)
    registry = Registry(settings)
    registry.contribution("odoo.addons.my_mod.models.A")
    _touch(settings.odoo_path / "addons/my_mod/models.py", MODEL + "\nX = 1\n")

    assert registry.inputs_changed() is True


def test_registry_inputs_changed_nothing_edited_returns_false(tmp_path: Path) -> None:
    settings = _odoo(tmp_path)
    registry = Registry(settings)
    registry.contribution("odoo.addons.my_mod.models.A")

    assert registry.inputs_changed() is False


def test_registry_inputs_changed_manifest_edited_returns_true(tmp_path: Path) -> None:
    settings = _odoo(tmp_path)
    registry = Registry(settings)
    registry.addon_name_for_path(settings.odoo_path / "addons/my_mod/models.py")
    _touch(settings.odoo_path / "addons/my_mod/__manifest__.py", "{'depends': []}")

    assert registry.inputs_changed() is True


def _custom(tmp_path: Path) -> Settings:
    """``_odoo`` plus an addons_path root with ``cust`` (depending on my_mod)."""
    settings = _odoo(tmp_path)
    addon = tmp_path / "custom" / "cust"
    addon.mkdir(parents=True)
    (addon / "__manifest__.py").write_text("{'depends': ['my_mod']}")
    (addon / "__init__.py").write_text("")
    return Settings(odoo_path=settings.odoo_path, addons_paths=(tmp_path / "custom",))


def test_addon_digest_overlay_root_set_changes(tmp_path: Path) -> None:
    simple = _custom(tmp_path)
    overlay = Settings(simple.odoo_path, simple.addons_paths, overlay_root=tmp_path / "root")
    store = RecordStore(tmp_path / "cache")
    before = _fingerprints(simple, store).addon_digest("cust")

    after = _fingerprints(overlay, store).addon_digest("cust")

    assert after != before


def test_load_index_stored_overlay_index_keeps_both_spellings(tmp_path: Path) -> None:
    simple = _custom(tmp_path)
    overlay = Settings(simple.odoo_path, simple.addons_paths, overlay_root=tmp_path / "root")
    store = RecordStore(tmp_path / "cache")
    load_index(simple, store)
    load_index(overlay, store)

    index, _ = load_index(overlay, store)

    assert (
        index.addons["cust"].module_prefix,
        index.canonical_module("cust.models"),
        index.addon_for_module("base.models"),
    ) == ("odoo.addons.cust", "odoo.addons.cust.models", None)


def test_module_data_file_with_values_annotation_follows_the_list_of_addons(
    tmp_path: Path,
) -> None:
    odoo = _odoo(tmp_path).odoo_path
    root = tmp_path / "addons"
    for name, source in (("user", 'V: "odoo.values.x_base" = {}\n'), ("plain", "X = 1\n")):
        addon = root / name
        addon.mkdir(parents=True)
        (addon / "__manifest__.py").write_text("{'depends': ['base']}")
        (addon / "__init__.py").write_text("from . import m\n")
        (addon / "m.py").write_text(source)
    settings = Settings(odoo_path=odoo, addons_paths=(root,))
    store = RecordStore(tmp_path / "store")

    def data(name: str) -> str | None:
        return _fingerprints(settings, store).module_data(f"{name}.m", str(root / name / "m.py"))

    before = (data("user"), data("plain"))
    extra = root / "extra"
    extra.mkdir()
    (extra / "__manifest__.py").write_text("{'depends': ['base']}")
    os.utime(root, ns=(10**18, 10**18))  # a new stamp of the root even within the clock
    after = (data("user"), data("plain"))

    assert after[0] != before[0]  # odoo.values keys may come from the new addon
    assert after[1] == before[1]


def test_addon_digest_manifest_edited_after_index_read_is_not_kept(tmp_path: Path) -> None:
    settings = _odoo(tmp_path)
    extra = settings.odoo_path / "addons/extra"
    extra.mkdir()
    (extra / "__manifest__.py").write_text("{'depends': ['base']}")
    (extra / "__init__.py").write_text("")
    store = RecordStore(tmp_path / "cache")
    prints = _fingerprints(settings, store)
    prints.addon_digest("base")  # reads the addon index
    _touch(settings.odoo_path / "addons/my_mod/__manifest__.py", "{'depends': ['base', 'extra']}")
    prints.addon_digest("my_mod")  # from the index read before the edit

    kept = _fingerprints(settings, store).addon_digest("my_mod")

    assert kept == _fingerprints(settings, RecordStore(None)).addon_digest("my_mod")


def test_addon_digest_addon_created_after_index_read_is_not_kept(tmp_path: Path) -> None:
    settings = _odoo(tmp_path)
    _touch(settings.odoo_path / "addons/my_mod/__manifest__.py", "{'depends': ['base', 'later']}")
    store = RecordStore(tmp_path / "cache")
    prints = _fingerprints(settings, store)
    prints.addon_digest("base")  # reads the addon index
    later = settings.odoo_path / "addons/later"
    later.mkdir()
    (later / "__manifest__.py").write_text("{'depends': ['base']}")
    (later / "__init__.py").write_text("")
    os.utime(later.parent, ns=(10**18, 10**18))  # a new stamp of the root even within the clock
    prints.addon_digest("my_mod")  # from the index read before ``later`` existed

    kept = _fingerprints(settings, store).addon_digest("my_mod")

    assert kept == _fingerprints(settings, RecordStore(None)).addon_digest("my_mod")


def _custom_module(tmp_path: Path, source: str) -> tuple[Settings, Path]:
    """``_custom`` with ``cust/m.py`` (imported by the addon)."""
    settings = _custom(tmp_path)
    cust = tmp_path / "custom" / "cust"
    (cust / "__init__.py").write_text("from . import m\n")
    (cust / "m.py").write_text(source)
    return settings, cust / "m.py"


def test_module_data_file_edited_after_scan_is_not_kept(tmp_path: Path) -> None:
    settings, path = _custom_module(tmp_path, "X = 1\n")
    store = RecordStore(tmp_path / "cache")
    prints = _fingerprints(settings, store)
    prints.addon_digest("cust")  # scans the addon: m.py names no model yet
    _touch(path, 'def f(env):\n    env["x.nowhere"]\n')
    prints.module_data("cust.m", str(path))

    kept = _fingerprints(settings, store).module_data("cust.m", str(path))

    assert kept == _fingerprints(settings, RecordStore(None)).module_data("cust.m", str(path))


def test_module_data_outside_part_follows_closure_change(tmp_path: Path) -> None:
    settings, path = _custom_module(tmp_path, 'def f(env):\n    env["x.dep"]\n')
    dep = tmp_path / "custom" / "dep"
    dep.mkdir()
    (dep / "__manifest__.py").write_text("{'depends': ['base']}")
    (dep / "__init__.py").write_text("from . import models\n")
    (dep / "models.py").write_text(MODEL.replace("x.a", "x.dep"))
    manifest = tmp_path / "custom" / "cust" / "__manifest__.py"
    _touch(manifest, "{'depends': ['my_mod', 'dep']}")
    store = RecordStore(tmp_path / "cache")
    _fingerprints(settings, store).module_data("cust.m", str(path))
    _touch(manifest, "{'depends': ['my_mod']}")  # x.dep is now outside the closure

    kept = _fingerprints(settings, store).module_data("cust.m", str(path))

    assert kept == _fingerprints(settings, RecordStore(None)).module_data("cust.m", str(path))


def test_module_data_odoo_module_follows_odoo_version(tmp_path: Path) -> None:
    settings = _odoo(tmp_path)
    path = str(settings.odoo_path / "models.py")
    other = dataclasses.replace(settings, odoo_version=18)

    data = _fingerprints(settings, RecordStore(None)).module_data("odoo.models", path)
    changed = _fingerprints(other, RecordStore(None)).module_data("odoo.models", path)

    assert data is not None
    assert changed is not None
    assert data != changed


def test_module_data_odoo_modules_of_symlinked_layouts_follow_odoo_version(
    tmp_path: Path,
) -> None:
    settings = dataclasses.replace(_odoo(tmp_path), odoo_version=19)
    odoo = settings.odoo_path
    (odoo / "http.py").write_text("")
    (tmp_path / "src" / "orm").mkdir(parents=True)
    (tmp_path / "src" / "orm" / "models.py").write_text("")
    (odoo / "orm").symlink_to(tmp_path / "src" / "orm")  # fetch.sh: a subpackage elsewhere
    overlay = tmp_path / "overlay" / "odoo"  # odoo-typing-root: a directory of symlinks
    overlay.mkdir(parents=True)
    for entry in ("http.py", "orm"):
        (overlay / entry).symlink_to(odoo / entry)
    prints = _fingerprints(settings, RecordStore(None))

    data = [
        prints.module_data("odoo", str(overlay / "__init__.py")),
        prints.module_data("odoo.http", str(overlay / "http.py")),
        prints.module_data("odoo.orm.models", str(overlay / "orm" / "models.py")),
        prints.module_data("odoo.orm.models", str(odoo / "orm" / "models.py")),
        prints.module_data("odoo_helpers", str(tmp_path / "odoo_helpers.py")),
    ]

    assert data == [*["odoo_version=19"] * 4, None]


@NOT_ROOT
def test_registry_inputs_changed_unreadable_file_returns_false_until_readable(
    tmp_path: Path,
) -> None:
    settings = _odoo(tmp_path)
    models = settings.odoo_path / "addons/my_mod/models.py"
    registry = Registry(settings)
    models.chmod(0)
    try:
        registry.contribution("odoo.addons.my_mod.models.A")
        locked = registry.inputs_changed()
    finally:
        models.chmod(0o644)  # a permission fix: the same mtime and size

    assert locked is False  # a daemon keeps its registry
    assert registry.inputs_changed() is True


@NOT_ROOT
def test_registry_inputs_changed_unsearchable_addon_returns_false_until_searchable(
    tmp_path: Path,
) -> None:
    settings = _odoo(tmp_path)
    addon = settings.odoo_path / "addons/my_mod"
    registry = Registry(settings)
    addon.chmod(0o600)  # listed, but its manifest cannot be looked up
    try:
        registry.addon_name_for_path(settings.odoo_path / "addons/base/models.py")
        locked = registry.inputs_changed()
    finally:
        addon.chmod(0o755)

    assert locked is False
    assert registry.inputs_changed() is True


@NOT_ROOT
def test_addon_digest_still_unreadable_file_reuses_the_stored_records(
    tmp_path: Path, mocker: MockerFixture
) -> None:
    settings = _odoo(tmp_path)
    models = settings.odoo_path / "addons/my_mod/models.py"
    store = RecordStore(tmp_path / "cache")
    models.chmod(0)
    try:
        first = _fingerprints(settings, store).addon_digest("my_mod")
        scan = mocker.spy(registry_scans, "scan_addon")
        second = _fingerprints(settings, store).addon_digest("my_mod")
    finally:
        models.chmod(0o644)

    fixed = _fingerprints(settings, store).addon_digest("my_mod")

    assert scan.call_count == 1  # my_mod, once readable
    assert second == first
    assert fixed != first
