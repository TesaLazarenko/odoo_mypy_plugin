"""Unit tests of the XML ids of data files (roadmap item 4.11)."""

from __future__ import annotations

import os
import warnings
from pathlib import Path

import pytest
from pytest_mock import MockerFixture

from odoo_typing import xmlids
from odoo_typing.stamps import UNREADABLE, Stamps, stamp
from odoo_typing.xmlids import XmlId, automatic_model, scan_data

VIEWS = """<?xml version="1.0"?>
<odoo>
    <data>
        <record id="view_form" model="ir.ui.view">
            <field name="name">form</field>
        </record>
        <record id="base.main_partner" model="res.partner"/>
        <template id="portal_page"/>
        <menuitem id="menu_root" name="Root">
            <menuitem id="menu_child" name="Child"/>
        </menuitem>
        <record model="res.partner"><field name="name">no id</field></record>
    </data>
</odoo>
"""


def test_scan_data_reads_records_shortcuts_and_csv(tmp_path: Path) -> None:
    (tmp_path / "views").mkdir()
    (tmp_path / "views/views.xml").write_text(VIEWS)
    (tmp_path / "ir.model.access.csv").write_text("id,name\naccess_thing,thing\n")
    (tmp_path / "broken.xml").write_text("<odoo><record")
    manifest = tmp_path / "__manifest__.py"
    manifest.write_text(
        "{'data': ['views/views.xml', 'ir.model.access.csv', 'broken.xml'], "
        "'demo': ['missing.xml']}"
    )

    scan = scan_data("t_mod", tmp_path, manifest)

    assert scan.xml_ids == (
        XmlId("t_mod.view_form", "ir.ui.view"),
        XmlId("base.main_partner", "res.partner"),
        XmlId("t_mod.portal_page", "ir.ui.view"),
        XmlId("t_mod.menu_root", "ir.ui.menu"),
        XmlId("t_mod.menu_child", "ir.ui.menu"),
        XmlId("t_mod.access_thing", "ir.model.access"),
    )
    assert [path.name for path in scan.files] == [
        "views.xml",
        "ir.model.access.csv",
        "broken.xml",
        "missing.xml",
    ]


def test_automatic_model_of_generated_ids() -> None:
    assert automatic_model("model_res_partner") == "ir.model"
    assert automatic_model("field_res_partner__name") == "ir.model.fields"
    assert automatic_model("module_sale") == "ir.module.module"
    assert automatic_model("view_form") is None


RECORD = '<odoo><record id="kept" model="x.thing"/></odoo>'


def _manifest(tmp_path: Path, text: str) -> Path:
    manifest = tmp_path / "__manifest__.py"
    manifest.write_text(text)
    return manifest


@pytest.mark.parametrize(
    "declared",
    ["no-such-codec", "Shift_JIS"],
    ids=["unknown", "multi-byte-but-not-that-encoding"],
)
def test_scan_data_skips_file_in_an_encoding_that_cannot_be_read(
    tmp_path: Path, declared: str
) -> None:
    declaration = f'<?xml version="1.0" encoding="{declared}"?>\n'.encode()
    record = b'<odoo><record id="x" model="m"><field name="name">\x81</field></record></odoo>'
    (tmp_path / "jp.xml").write_bytes(declaration + record)  # 0x81 then "<": no Shift_JIS
    (tmp_path / "ok.xml").write_text(RECORD)
    manifest = _manifest(tmp_path, "{'data': ['jp.xml', 'ok.xml']}")

    scan = scan_data("t_mod", tmp_path, manifest)

    assert scan.xml_ids == (XmlId("t_mod.kept", "x.thing"),)


@pytest.mark.parametrize("encoding", ["Shift_JIS", "EUC-JP", "GBK"])
def test_scan_data_multi_byte_file_ids_are_found(tmp_path: Path, encoding: str) -> None:
    # expat reads no multi-byte encoding (Odoo's lxml does): the file is decoded first.
    text = (
        f"<?xml version='1.0' encoding='{encoding}'?>\n<odoo>\n"
        '    <record id="tax_jp" model="account.tax"><field name="name">\u7a0e</field></record>\n'
        "</odoo>\n"
    )
    (tmp_path / "jp.xml").write_bytes(text.encode(encoding))
    manifest = _manifest(tmp_path, "{'data': ['jp.xml']}")

    scan = scan_data("l10n_jp", tmp_path, manifest)

    assert scan.xml_ids == (XmlId("l10n_jp.tax_jp", "account.tax"),)


def test_scan_data_skips_data_entries_with_a_nul(tmp_path: Path) -> None:
    manifest = _manifest(tmp_path, "{'data': ['a\\x00.xml', 'b\\x00.csv']}")

    scan = scan_data("t_mod", tmp_path, manifest)

    assert scan.xml_ids == ()
    assert len(scan.files) == 2


@pytest.mark.parametrize(
    "text",
    ["{[]: 1}", "{'data': ['a.xml'], 'x': {[]}}", "1+" * 100_000 + "1", "-" * 100_000 + "1"],
    ids=["unhashable-key", "unhashable-member", "recursion", "parser-stack"],
)
def test_scan_data_manifest_that_is_no_literal_lists_no_files(tmp_path: Path, text: str) -> None:
    manifest = _manifest(tmp_path, text)

    scan = scan_data("t_mod", tmp_path, manifest)

    assert (scan.xml_ids, scan.files) == ((), ())


def test_scan_data_manifest_with_invalid_escape_is_read_under_w_error(tmp_path: Path) -> None:
    (tmp_path / "ok.xml").write_text(RECORD)
    manifest = _manifest(tmp_path, "{'data': ['ok.xml'], 'summary': 'Matches \\d'}")

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("error")  # python -W error
        scan = scan_data("t_mod", tmp_path, manifest)

    assert caught == []
    assert scan.xml_ids == (XmlId("t_mod.kept", "x.thing"),)


def test_scan_data_records_stamps_taken_before_each_read(
    tmp_path: Path, mocker: MockerFixture
) -> None:
    data = tmp_path / "ok.xml"
    data.write_text(RECORD)
    manifest = _manifest(tmp_path, "{'data': ['ok.xml'], 'demo': ['missing.xml']}")
    before = {str(manifest): stamp(manifest), str(data): stamp(data)}
    read = xmlids._xml_ids

    def read_then_saved(path: Path, addon_name: str) -> list[XmlId] | None:
        found = read(path, addon_name)
        if path == data:  # saved right after it was read
            data.write_text(RECORD.replace("kept", "changed"))
            os.utime(data, ns=(10**18, 10**18))
        return found

    mocker.patch.object(xmlids, "_xml_ids", side_effect=read_then_saved)
    stamps: Stamps = {}

    scan_data("t_mod", tmp_path, manifest, stamps)

    assert stamps == {**before, str(tmp_path / "missing.xml"): None}


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads any file")
def test_scan_data_unreadable_file_gets_a_stamp_no_file_has(tmp_path: Path) -> None:
    data = tmp_path / "ok.csv"
    data.write_text("id,name\naccess,x\n")
    manifest = _manifest(tmp_path, "{'data': ['ok.csv']}")
    stamps: Stamps = {}
    data.chmod(0)
    try:
        scan = scan_data("t_mod", tmp_path, manifest, stamps)
    finally:
        data.chmod(0o644)

    assert scan.xml_ids == ()
    assert stamps[str(data)] == UNREADABLE
