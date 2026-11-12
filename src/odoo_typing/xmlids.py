"""XML ids of an addon's data files: what ``env.ref("module.name")`` refers to.

Odoo creates the records of the files listed under ``data`` and ``demo`` in the
manifest, each with an XML id (``ir.model.data``):

* XML: ``<record id="x" model="m">`` and the shortcuts ``<template>``
  (``ir.ui.view``), ``<menuitem>`` (``ir.ui.menu``), ``<act_window>``
  (``ir.actions.act_window``), ``<report>`` (``ir.actions.report``), ``<asset>``
  (``theme.ir.asset`` / ``ir.asset``);
* CSV: the ``id`` column of ``<model>.csv`` (``ir.model.access.csv``).

An id without a module (``x``) belongs to the addon; ``other.x`` names a record of
another addon (an override). Odoo also makes ids for models (``model_res_partner``),
fields (``field_res_partner__name``) and the addon itself (``base.module_sale``); they
are not in the files, see :func:`automatic_model`.

The files are read as data, never executed; an unreadable or invalid file is skipped
(Odoo would fail to install the addon, which is not what this checks).
"""

from __future__ import annotations

import ast
import csv
import re
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Final
from xml.etree import ElementTree

from mypy.errorcodes import ErrorCode

__all__ = [
    "ODOO_XMLID",
    "ODOO_XMLID_DEPENDS",
    "DataScan",
    "XmlId",
    "automatic_model",
    "scan_data",
]

ODOO_XMLID = ErrorCode("odoo-xmlid", "Check the XML ids of env.ref()", "Odoo")
ODOO_XMLID_DEPENDS = ErrorCode(
    "odoo-xmlid-depends",
    "Check that XML ids are created by the addon's dependencies",
    "Odoo",
    sub_code_of=ODOO_XMLID,
)

# Manifest keys whose files Odoo loads (``demo`` only with demo data).
DATA_KEYS: Final = ("data", "demo")
SHORTCUT_MODELS: Final = {
    "template": "ir.ui.view",
    "menuitem": "ir.ui.menu",
    "act_window": "ir.actions.act_window",
    "report": "ir.actions.report",
    "asset": "ir.asset",
}
AUTOMATIC: Final = (
    (re.compile(r"model_[a-z0-9_]+"), "ir.model"),
    (re.compile(r"field_[a-z0-9_]+__[a-z0-9_]+"), "ir.model.fields"),
    (re.compile(r"module_[a-z0-9_]+"), "ir.module.module"),
)


@dataclass(frozen=True, slots=True)
class XmlId:
    """A record an addon's data files create.

    Attributes:
        name: Full XML id, ``module.name``.
        model: Model of the record.
    """

    name: str
    model: str


@dataclass(frozen=True, slots=True)
class DataScan:
    """The XML ids of an addon's data files.

    Attributes:
        xml_ids: In file order.
        files: The data files read (their stamps tell when to scan again).
    """

    xml_ids: tuple[XmlId, ...]
    files: tuple[Path, ...]


def scan_data(addon_name: str, addon_path: Path, manifest: Path | None) -> DataScan:
    """Read the XML ids of the data files a manifest lists.

    Args:
        addon_name: The addon, the module of ids written without one.
        addon_path: Its directory (data file paths are relative to it).
        manifest: Its manifest file, if any.
    """
    xml_ids: list[XmlId] = []
    files: list[Path] = []
    for relative in _data_files(manifest):
        path = addon_path / relative
        files.append(path)
        if path.suffix == ".xml":
            xml_ids += _xml_ids(path, addon_name)
        elif path.suffix == ".csv":
            xml_ids += _csv_ids(path, addon_name)
    return DataScan(tuple(xml_ids), tuple(files))


def automatic_model(name: str) -> str | None:
    """The model of an XML id Odoo creates itself (``model_x``, ``field_x__y``...)."""
    return next((model for pattern, model in AUTOMATIC if pattern.fullmatch(name)), None)


def _data_files(manifest: Path | None) -> Iterator[str]:
    if manifest is None:
        return
    try:
        data = ast.literal_eval(manifest.read_text(encoding="utf-8"))
    except (OSError, SyntaxError, ValueError, UnicodeDecodeError):
        return
    if not isinstance(data, dict):
        return
    for key in DATA_KEYS:
        entries = data.get(key, [])
        if isinstance(entries, list | tuple):
            yield from (entry for entry in entries if isinstance(entry, str))


def _qualified(xml_id: str, addon_name: str) -> str:
    return xml_id if "." in xml_id else f"{addon_name}.{xml_id}"


def _xml_ids(path: Path, addon_name: str) -> list[XmlId]:
    try:
        root = ElementTree.parse(path).getroot()
    except (OSError, ElementTree.ParseError):
        return []
    found: list[XmlId] = []
    for element in root.iter():
        xml_id = element.get("id")
        if not xml_id or not isinstance(element.tag, str):
            continue
        model = element.get("model") if element.tag == "record" else None
        model = model or SHORTCUT_MODELS.get(element.tag)
        if model:
            found.append(XmlId(_qualified(xml_id, addon_name), model))
    return found


def _csv_ids(path: Path, addon_name: str) -> list[XmlId]:
    model = path.stem
    try:
        with path.open(encoding="utf-8", newline="") as handle:
            rows = list(csv.DictReader(handle))
    except (OSError, UnicodeDecodeError, csv.Error):
        return []
    return [XmlId(_qualified(row["id"], addon_name), model) for row in rows if row.get("id")]
