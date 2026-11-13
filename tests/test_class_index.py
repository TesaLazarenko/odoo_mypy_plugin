"""The text index of the files that name models (``odoo.values.*`` keys, stage 7)."""

from __future__ import annotations

import os
from pathlib import Path

from pytest_mock import MockerFixture

from odoo_typing.addons import Addon
from odoo_typing.records import Record
from odoo_typing.registry import class_index
from odoo_typing.registry.class_index import ClassFileIndex, module_of


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
