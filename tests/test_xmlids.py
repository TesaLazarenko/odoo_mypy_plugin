"""Unit tests of the XML ids of data files (roadmap item 4.11)."""

from __future__ import annotations

from pathlib import Path

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
