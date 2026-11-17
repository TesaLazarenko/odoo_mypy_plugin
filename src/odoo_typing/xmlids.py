"""XML ids of an addon's data files: what ``env.ref("module.name")`` refers to.

Odoo creates the records of the files listed under ``data`` and ``demo`` in the
manifest, each with an XML id (``ir.model.data``):

* XML: ``<record id="x" model="m">`` and the shortcuts ``<template>``
  (``ir.ui.view``), ``<menuitem>`` (``ir.ui.menu``), ``<act_window>``
  (``ir.actions.act_window``), ``<report>`` (``ir.actions.report``), ``<asset>``
  (``theme.ir.asset`` / ``ir.asset``);
* CSV: the ``id`` column of ``<model>.csv`` (``ir.model.access.csv``);
* ``<function model="ir.model.data" name="_update_xmlids">``: the ``'xml_id'`` keys of
  its literal dicts (``stock.stock_location_stock``), ids given to records the data
  made otherwise; their model is not written, so it is :data:`UNKNOWN_MODEL` and
  ``env.ref`` keeps its declared type.

An id without a module (``x``) belongs to the addon; ``other.x`` names a record of
another addon (an override). Odoo also makes ids for models (``model_res_partner``),
fields (``field_res_partner__name``) and the addon itself (``base.module_sale``); they
are not in the files, see :func:`automatic_model`.

The files are read as data, never executed; an unreadable or invalid file is skipped
(Odoo would fail to install the addon, which is not what this checks).

A scan records the stamps of the manifest and the data files (:mod:`odoo_typing.stamps`),
each taken before the file is read; a file that cannot be read gets
:data:`~odoo_typing.stamps.UNREADABLE`, so the scan is redone once it can be.

expat, which reads the XML files, knows no multi-byte encoding (``Shift_JIS``, ``GBK``),
which lxml, and so Odoo, reads: such a file is decoded first (:func:`_parse_xml`).
"""

from __future__ import annotations

import ast
import csv
import re
import sys
import warnings
from collections.abc import Iterator
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Final
from xml.etree import ElementTree

from mypy.errorcodes import ErrorCode

from odoo_typing.addons import UNREADABLE_MANIFEST, literal_manifest
from odoo_typing.stamps import UNREADABLE, Stamps, stamp

__all__ = [
    "ODOO_XMLID",
    "ODOO_XMLID_DEPENDS",
    "UNKNOWN_MODEL",
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
DEMO_KEY: Final = "demo"
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
# The XML declaration of a file, with its encoding (group 1); ASCII in every encoding
# expat refuses (they all extend ASCII).
DECLARATION: Final = re.compile(
    rb"""\A\s*<\?xml\s[^>]*?\bencoding\s*=\s*["']([A-Za-z][\w.:-]*)["'][^>]*\?>"""
)
MULTI_BYTE: Final = "multi-byte encodings are not supported"  # what expat says of them


@dataclass(frozen=True, slots=True)
class XmlId:
    """A record an addon's data files create.

    Attributes:
        name: Full XML id, ``module.name``.
        model: Model of the record.
        demo: Created by a ``demo`` file (only in a database with demo data).
    """

    name: str
    model: str
    demo: bool = False


@dataclass(frozen=True, slots=True)
class DataScan:
    """The XML ids of an addon's data files.

    Attributes:
        xml_ids: In file order.
        files: The data files read (their stamps tell when to scan again).
    """

    xml_ids: tuple[XmlId, ...]
    files: tuple[Path, ...]


def scan_data(
    addon_name: str, addon_path: Path, manifest: Path | None, stamps: Stamps | None = None
) -> DataScan:
    """Read the XML ids of the data files a manifest lists.

    Args:
        addon_name: The addon, the module of ids written without one.
        addon_path: Its directory (data file paths are relative to it).
        manifest: Its manifest file, if any.
        stamps: Filled with the stamps of the manifest and the listed files (also of a
            missing one), each taken before the file is read; a file already in it
            keeps its stamp.
    """
    recorded: Stamps = {} if stamps is None else stamps
    xml_ids: list[XmlId] = []
    files: list[Path] = []
    for relative, key in _data_files(manifest, recorded):
        path = addon_path / relative
        files.append(path)
        current = recorded.setdefault(str(path), stamp(path))
        found = _file_ids(path, addon_name)
        if found is None and current is not None:  # exists, but cannot be read
            recorded[str(path)] = UNREADABLE
        if key == DEMO_KEY:
            found = [replace(item, demo=True) for item in found or ()]
        xml_ids += found or ()
    return DataScan(tuple(xml_ids), tuple(files))


def automatic_model(name: str) -> str | None:
    """The model of an XML id Odoo creates itself (``model_x``, ``field_x__y``...)."""
    return next((model for pattern, model in AUTOMATIC if pattern.fullmatch(name)), None)


def _data_files(manifest: Path | None, stamps: Stamps) -> Iterator[tuple[str, str]]:
    if manifest is None:
        return
    current = stamps.setdefault(str(manifest), stamp(manifest))  # its lists count too
    try:
        text = manifest.read_text(encoding="utf-8")
    except OSError:
        if current is not None:
            stamps[str(manifest)] = UNREADABLE
        return
    except ValueError:  # not UTF-8
        return
    try:
        data = literal_manifest(text)
    except UNREADABLE_MANIFEST:
        return
    if not isinstance(data, dict):
        return
    for key in DATA_KEYS:
        entries = data.get(key, [])
        if isinstance(entries, list | tuple):
            yield from ((entry, key) for entry in entries if isinstance(entry, str))


UNKNOWN_MODEL: Final = ""
"""The model of an XML id given by ``_update_xmlids``: the data does not say it."""


def _qualified(xml_id: str, addon_name: str) -> str:
    return xml_id if "." in xml_id else f"{addon_name}.{xml_id}"


def _file_ids(path: Path, addon_name: str) -> list[XmlId] | None:
    """The XML ids of one data file (none for another kind); ``None``: cannot be read."""
    if path.suffix == ".xml":
        return _xml_ids(path, addon_name)
    if path.suffix == ".csv":
        return _csv_ids(path, addon_name)
    return []


def _xml_ids(path: Path, addon_name: str) -> list[XmlId] | None:
    try:
        root = _parse_xml(path)
    except OSError:
        return None
    # ValueError: bytes that are not of the declared encoding, or a NUL in the path;
    # LookupError: an encoding Python does not know.
    except (ElementTree.ParseError, ValueError, LookupError):
        return []
    found: list[XmlId] = []
    for element in root.iter():
        if _updates_xml_ids(element):
            updated = _updated(element)
            found.extend(XmlId(_qualified(name, addon_name), UNKNOWN_MODEL) for name in updated)
            continue
        xml_id = element.get("id")
        if not xml_id or not isinstance(element.tag, str):
            continue
        model = element.get("model") if element.tag == "record" else None
        model = model or SHORTCUT_MODELS.get(element.tag)
        if model:  # few models, many ids: one string per model (``ir.ui.view`` x 2233 on sale)
            found.append(XmlId(_qualified(xml_id, addon_name), sys.intern(model)))
    return found


def _updates_xml_ids(element: ElementTree.Element) -> bool:
    return (
        element.tag == "function"
        and element.get("model") == "ir.model.data"
        and element.get("name") == "_update_xmlids"
    )


def _updated(function: ElementTree.Element) -> Iterator[str]:
    """The ``'xml_id': '...'`` keys in the ``eval`` of a ``_update_xmlids`` call."""
    trees = (_expression(element.get("eval") or "") for element in function.iter())
    dicts = (
        node for tree in trees if tree for node in ast.walk(tree) if isinstance(node, ast.Dict)
    )
    for node in dicts:
        for key, value in zip(node.keys, node.values, strict=True):
            if _constant(key) == "xml_id" and (name := _constant(value)):
                yield name


def _expression(source: str) -> ast.Expression | None:
    """An ``eval`` attribute's expression, ``None`` when it is empty or not Python."""
    if not source.strip():
        return None
    try:
        with warnings.catch_warnings():  # no SyntaxWarning for an invalid escape
            warnings.simplefilter("ignore")
            return ast.parse(source.strip(), mode="eval")
    except (SyntaxError, ValueError, MemoryError, RecursionError):
        return None


def _constant(node: ast.expr | None) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def _parse_xml(path: Path) -> ElementTree.Element:
    """The root element of an XML file, also of one in a multi-byte encoding.

    expat refuses those (``Shift_JIS``, ``GBK``, ``EUC-JP``): the file is then decoded
    with Python's codec, and the text parsed without its declaration.

    Raises:
        OSError: If the file cannot be read.
        ElementTree.ParseError: If it is not well-formed (also ``ValueError``, or
            ``LookupError`` for an encoding Python does not know).
    """
    try:
        return ElementTree.parse(path).getroot()
    except ValueError as error:
        if MULTI_BYTE not in str(error):
            raise
    data = path.read_bytes()
    declaration = DECLARATION.match(data)
    if declaration is None:
        raise ValueError(f"{MULTI_BYTE}, and no declaration names one")
    text = data[declaration.end() :].decode(declaration.group(1).decode("ascii"))
    return ElementTree.fromstring(text)


def _csv_ids(path: Path, addon_name: str) -> list[XmlId] | None:
    model = path.stem
    try:
        with path.open(encoding="utf-8", newline="") as handle:
            rows = list(csv.DictReader(handle))
    except OSError:
        return None
    except (ValueError, csv.Error):  # not UTF-8 (UnicodeDecodeError), a NUL in the path
        return []
    return [XmlId(_qualified(row["id"], addon_name), model) for row in rows if row.get("id")]
