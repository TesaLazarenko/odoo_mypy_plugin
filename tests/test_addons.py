"""Unit tests of addon discovery on what users' disks hold: broken manifests, locked dirs."""

from __future__ import annotations

import os
import warnings
from collections.abc import Iterator
from pathlib import Path

import pytest
from pytest_mock import MockerFixture

from odoo_typing import records
from odoo_typing.addons import AddonIndex, find_manifest, read_depends
from odoo_typing.config import Settings
from odoo_typing.records import RecordStore, load_index
from odoo_typing.stamps import UNREADABLE, index_stamps, unchanged

NOT_ROOT = pytest.mark.skipif(os.geteuid() == 0, reason="root reads every file")


@pytest.mark.parametrize(
    "source",
    [
        "{[]: 1}",
        "{'depends': ['sale'], 'x': {[]}}",
        "{'depends': [1" + "+1" * 100_000 + "]}",
        "{'depends': [" + "-" * 100_000 + "1]}",
    ],
    ids=["unhashable-key", "unhashable-set-member", "long-expression", "deep-expression"],
)
def test_read_depends_manifest_literal_eval_rejects_returns_empty(
    tmp_path: Path, source: str
) -> None:
    # TypeError, RecursionError, MemoryError: a work-in-progress manifest of any addon.
    manifest = tmp_path / "__manifest__.py"
    manifest.write_text(source)

    assert read_depends(manifest) == ()


@pytest.fixture
def locked_root(tmp_path: Path) -> Iterator[Path]:
    """An addons root with an addon and a directory nobody may read (``lost+found``)."""
    if os.geteuid() == 0:
        pytest.skip("root reads every directory")
    root = tmp_path / "addons"
    (root / "my_mod").mkdir(parents=True)
    (root / "my_mod" / "__manifest__.py").write_text("{'depends': ['base']}")
    locked = root / "a_locked"  # listed before my_mod in name order
    locked.mkdir()
    locked.chmod(0)
    try:
        yield root
    finally:
        locked.chmod(0o700)


def test_find_manifest_unreadable_directory_returns_none(locked_root: Path) -> None:
    assert find_manifest(locked_root / "a_locked") is None


def test_addon_index_unreadable_directory_is_no_addon(tmp_path: Path, locked_root: Path) -> None:
    (tmp_path / "odoo" / "addons").mkdir(parents=True)

    index = AddonIndex(tmp_path / "odoo", [locked_root])

    assert list(index.addons) == ["my_mod"]


def test_index_stamps_unreadable_directory_is_stamped_as_a_directory(locked_root: Path) -> None:
    stamps = index_stamps([locked_root])

    assert set(stamps) == {
        str(locked_root),
        str(locked_root / "a_locked"),
        str(locked_root / "my_mod" / "__manifest__.py"),
    }


def _settings(tmp_path: Path) -> Settings:
    """Odoo with ``base``, and an addons root with ``shop`` (depending on base and sale)."""
    base = tmp_path / "odoo" / "addons" / "base"
    base.mkdir(parents=True)
    (base / "__manifest__.py").write_text("{'depends': []}")
    shop = tmp_path / "custom" / "shop"
    shop.mkdir(parents=True)
    (shop / "__manifest__.py").write_text("{'depends': ['base', 'sale']}")
    return Settings(odoo_path=tmp_path / "odoo", addons_paths=(tmp_path / "custom",))


@NOT_ROOT
def test_load_index_addon_is_found_once_its_directory_is_searchable(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    shop = tmp_path / "custom" / "shop"
    store = RecordStore(tmp_path / "cache")
    shop.chmod(0o600)  # listed, but its manifest cannot be looked up
    try:
        locked, _ = load_index(settings, store)
    finally:
        shop.chmod(0o755)  # a permission fix: the same mtime and size

    fixed, _ = load_index(settings, store)

    assert "shop" not in locked.addons
    assert "shop" in fixed.addons


@NOT_ROOT
def test_load_index_depends_are_read_once_the_manifest_is_readable(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    manifest = tmp_path / "custom" / "shop" / "__manifest__.py"
    store = RecordStore(tmp_path / "cache")
    manifest.chmod(0)
    try:
        locked, _ = load_index(settings, store)
    finally:
        manifest.chmod(0o644)

    fixed, _ = load_index(settings, store)

    assert locked.addons["shop"].depends == ()
    assert fixed.addons["shop"].depends == ("base", "sale")


@NOT_ROOT
def test_load_index_still_unreadable_entries_reuse_the_stored_index(
    tmp_path: Path, mocker: MockerFixture
) -> None:
    settings = _settings(tmp_path)
    lab = tmp_path / "custom" / "lab"
    lab.mkdir()
    (lab / "__manifest__.py").write_text("{'depends': ['base']}")
    manifest = tmp_path / "custom" / "shop" / "__manifest__.py"
    store = RecordStore(tmp_path / "cache")
    lab.chmod(0o600)
    manifest.chmod(0)
    try:
        load_index(settings, store)
        read = mocker.spy(records, "read_index")
        index, _ = load_index(settings, store)
    finally:
        lab.chmod(0o755)
        manifest.chmod(0o644)

    assert read.call_count == 0
    assert list(index.addons) == ["base", "shop"]


@NOT_ROOT
def test_unchanged_root_that_cannot_be_listed_is_unchanged_until_listable(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    root = tmp_path / "custom"
    root.chmod(0o300)  # searched, but not listed
    try:
        stamps = index_stamps([root])
        locked = unchanged(stamps)
        index = AddonIndex(settings.odoo_path, [root])
    finally:
        root.chmod(0o755)

    assert stamps == {str(root): UNREADABLE}
    assert locked is True
    assert list(index.addons) == ["base"]
    assert unchanged(stamps) is False


def test_index_stamps_dot_directories_are_no_addons(tmp_path: Path) -> None:
    root = tmp_path / "addons"
    for name in (".git", "__pycache__", "shop"):
        (root / name).mkdir(parents=True)
    (root / "shop" / "__manifest__.py").write_text("{}")
    stamps = index_stamps([root])
    for name in (".git", "__pycache__"):  # git status, an import: entries change in them
        os.utime(root / name, ns=(1, 1))

    assert unchanged(stamps) is True
    assert set(stamps) == {str(root), str(root / "shop" / "__manifest__.py")}


def test_addon_index_manifest_in_a_dot_directory_is_no_addon(tmp_path: Path) -> None:
    (tmp_path / "odoo" / "addons").mkdir(parents=True)
    root = tmp_path / "addons"
    for name in (".hidden", "__pycache__", "shop"):
        (root / name).mkdir(parents=True)
        (root / name / "__manifest__.py").write_text("{}")

    index = AddonIndex(tmp_path / "odoo", [root])

    assert list(index.addons) == ["shop"]


def test_read_depends_invalid_escape_is_read_under_w_error(tmp_path: Path) -> None:
    manifest = tmp_path / "__manifest__.py"
    manifest.write_text("{'summary': 'Matches \\d+', 'depends': ['base', 'sale']}")

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("error")  # python -W error
        depends = read_depends(manifest)

    assert caught == []
    assert depends == ("base", "sale")
