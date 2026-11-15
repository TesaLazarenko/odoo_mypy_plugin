"""The text index of the files that name models (``odoo.values.*`` keys, stage 7)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from pytest_mock import MockerFixture

from odoo_typing.addons import Addon
from odoo_typing.records import Record
from odoo_typing.registry import class_index
from odoo_typing.registry.class_index import ClassFileIndex, module_of

NOT_ROOT = pytest.mark.skipif(os.geteuid() == 0, reason="root reads any file")


class _Keeper:
    """Records kept in memory, as the scan store keeps them on disk."""

    def __init__(self) -> None:
        self.records: dict[str, Record] = {}

    def load_record(self, key: str) -> Record | None:
        return self.records.get(key)

    def save_record(self, key: str, record: Record) -> None:
        self.records[key] = record


def _write(path: Path, source: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source)
    os.utime(path.parent, ns=(10**18 + len(source), 10**18 + len(source)))  # a new stamp


def _shop(tmp_path: Path) -> Addon:
    path = tmp_path / "shop"
    _write(path / "models" / "order.py", '_name = "shop.order"\n_inherit = ["mail.thread"]\n')
    _write(path / "tests" / "test_order.py", '_name = "test.model"\n')
    _write(path / "tools.py", "X = 1\n")
    return Addon(name="shop", path=path, depends=("base",), module_prefix="odoo.addons.shop")


def test_index_finds_the_files_that_name_a_model(tmp_path: Path) -> None:
    shop = _shop(tmp_path)

    index = ClassFileIndex([shop], None)

    order = shop.path / "models" / "order.py"
    assert index.files("shop.order") == (("shop", order),)
    assert index.files("mail.thread") == (("shop", order),)
    assert index.model_names() == {"shop.order", "mail.thread"}  # not tests


def test_kept_index_reads_only_what_changed(tmp_path: Path, mocker: MockerFixture) -> None:
    shop = _shop(tmp_path)
    keeper = _Keeper()
    ClassFileIndex([shop], keeper).model_names()
    read = mocker.spy(class_index, "read_source")

    unchanged = ClassFileIndex([shop], keeper).files("shop.order")
    reads_unchanged = read.call_count
    _write(shop.path / "models" / "line.py", '_name = "shop.line"\n')  # a new file
    tools = shop.path / "tools.py"
    tools.write_text('_inherit = "shop.order"\n')  # a file that names a model now
    os.utime(tools, ns=(10**18, 10**18))
    (shop.path / "models" / "order.py").unlink()
    os.utime(shop.path / "models", ns=(2 * 10**18, 2 * 10**18))
    changed = ClassFileIndex([shop], keeper)

    assert unchanged == (("shop", shop.path / "models" / "order.py"),)
    assert reads_unchanged == 0
    assert changed.files("shop.order") == (("shop", tools),)
    assert changed.files("shop.line") == (("shop", shop.path / "models" / "line.py"),)
    assert changed.files("mail.thread") == ()
    assert read.call_count == 2  # line.py and tools.py


def test_index_skips_files_that_cannot_be_read(tmp_path: Path) -> None:
    shop = _shop(tmp_path)
    (shop.path / "broken.py").symlink_to(tmp_path / "missing.py")

    index = ClassFileIndex([shop], None)

    assert index.files("shop.order")


def test_module_of_a_file_of_an_addon(tmp_path: Path) -> None:
    shop = _shop(tmp_path)

    names = [
        module_of(shop, shop.path / "models" / "order.py"),
        module_of(shop, shop.path / "models" / "__init__.py"),
        module_of(shop, tmp_path / "other.py"),
    ]

    assert names == ["odoo.addons.shop.models.order", "odoo.addons.shop.models", None]


def test_index_reads_names_the_scanner_evaluates(tmp_path: Path) -> None:
    shop = _shop(tmp_path)
    forms = shop.path / "models" / "forms.py"
    source = 'MODEL = "shop.const"\nclass A:\n    _name: str = "shop.annotated"\n'
    source += "class B:\n    _name = MODEL\nclass C:\n    _inherit = _name\n"
    _write(forms, source)

    index = ClassFileIndex([shop], None)

    assert index.files("shop.annotated") == (("shop", forms),)
    assert index.files("shop.const") == (("shop", forms),)


def test_index_does_not_follow_symlink_loops(tmp_path: Path) -> None:
    shop = _shop(tmp_path)
    (shop.path / "models" / "up").symlink_to(shop.path)  # back to the addon's directory

    index = ClassFileIndex([shop], None)

    assert index.files("shop.order") == (("shop", shop.path / "models" / "order.py"),)


def test_index_entry_that_cannot_be_stat_ed_skips_only_that_entry(tmp_path: Path) -> None:
    shop = _shop(tmp_path)
    models = shop.path / "models"
    for number in range(20):
        _write(models / f"part_{number}.py", f'_name = "shop.part{number}"\n')
    (models / "loop").symlink_to("loop")  # a link to itself
    (models / "loop.py").symlink_to("loop.py")

    index = ClassFileIndex([shop], None)

    assert {f"shop.part{number}" for number in range(20)} <= index.model_names()


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads any file")
def test_kept_index_reads_a_file_again_once_readable(tmp_path: Path) -> None:
    shop = _shop(tmp_path)
    keeper = _Keeper()
    order = shop.path / "models" / "order.py"
    order.chmod(0)
    try:
        unreadable = ClassFileIndex([shop], keeper).files("shop.order")
    finally:
        order.chmod(0o644)  # a permission fix: the same mtime and size

    readable = ClassFileIndex([shop], keeper).files("shop.order")

    assert unreadable == ()
    assert readable == (("shop", order),)


def test_index_walks_sibling_alias_symlinks(tmp_path: Path) -> None:
    shop = _shop(tmp_path)
    (shop.path / "aaa_compat").symlink_to(shop.path / "models")  # not a loop: walked too

    index = ClassFileIndex([shop], None)

    assert index.files("shop.order") == (
        ("shop", shop.path / "aaa_compat" / "order.py"),
        ("shop", shop.path / "models" / "order.py"),
    )


@NOT_ROOT
def test_kept_index_lists_a_directory_again_once_listable(tmp_path: Path) -> None:
    shop = _shop(tmp_path)
    keeper = _Keeper()
    models = shop.path / "models"
    models.chmod(0o311)  # searched, but not listed
    try:
        locked = ClassFileIndex([shop], keeper).files("shop.order")
    finally:
        models.chmod(0o755)  # a permission fix: the same mtime and size

    fixed = ClassFileIndex([shop], keeper).files("shop.order")

    assert locked == ()
    assert fixed == (("shop", models / "order.py"),)


@NOT_ROOT
def test_kept_index_still_unreadable_file_is_not_read_again(
    tmp_path: Path, mocker: MockerFixture
) -> None:
    shop = _shop(tmp_path)
    keeper = _Keeper()
    order = shop.path / "models" / "order.py"
    order.chmod(0)
    try:
        ClassFileIndex([shop], keeper).model_names()
        read = mocker.spy(class_index, "read_source")
        index = ClassFileIndex([shop], keeper)
        index.model_names()
    finally:
        order.chmod(0o644)

    assert read.call_count == 0
    assert index.changed() is True  # readable now


@NOT_ROOT
@pytest.mark.parametrize(("entry", "mode"), [("models", 0o311), ("models/order.py", 0)])
def test_index_changed_unreadable_entry_returns_false_until_readable(
    tmp_path: Path, entry: str, mode: int
) -> None:
    shop = _shop(tmp_path)
    path = shop.path / entry
    before = path.stat().st_mode
    path.chmod(mode)
    try:
        index = ClassFileIndex([shop], None)
        index.model_names()
        locked = index.changed()
    finally:
        path.chmod(before)

    assert locked is False  # a daemon keeps its registry
    assert index.changed() is True
