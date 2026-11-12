"""Build Odoo's registry of an addon and its dependencies, and dump the merged ``_fields``.

Run with the interpreter of that Odoo; no database is needed: the registry is built in
memory with a cursor that answers every query with no rows (as for a database where
nothing is installed yet, so no manual models or fields), e.g. for the Odoo 18 deb:

    /usr/bin/python3 -I tools/fields/dump_registry.py merged.json crm sale_management

For each top addon: the addons of its dependency closure are loaded in Odoo's order
(depth in the dependency graph, then name), each with ``Registry.load``, then
``Registry.setup_models`` merges the fields as on a real database. Each field of each
model is dumped with its class and the arguments the plugin's merged view also knows
(after setup: defaults filled in). ``tools/fields/compare.py --merged`` compares the
dump with the plugin's ``Registry.fields`` as the top addon sees it.
"""

from __future__ import annotations

import ast
import importlib
import json
import logging
import os
import sys
from collections.abc import Callable, Iterable
from types import SimpleNamespace

logging.disable(logging.CRITICAL)

import odoo  # noqa: E402
from odoo import api, sql_db  # noqa: E402
from odoo.modules import module as odoo_module  # noqa: E402
from odoo.modules.registry import Registry  # noqa: E402
from odoo.tools import config  # noqa: E402

DYNAMIC = "<dynamic>"
DATABASE = "odoo_typing_stub"


class StubCursor:
    """A cursor of an empty database: every query succeeds and returns no rows."""

    rowcount = 0
    dbname = DATABASE

    def __init__(self) -> None:
        self.transaction: object = None

    def execute(self, *args: object, **kwargs: object) -> None:
        """Run nothing."""

    def fetchone(self) -> None:
        """No row."""

    def fetchall(self) -> list[object]:
        """No rows."""
        return []

    dictfetchall = fetchall
    dictfetchone = fetchone

    def close(self) -> None:
        """Nothing to close."""

    def __enter__(self) -> StubCursor:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()


def _manifest(addons_dirs: Iterable[str], name: str) -> dict[str, object] | None:
    for directory in addons_dirs:
        path = os.path.join(directory, name, "__manifest__.py")
        if os.path.isfile(path):
            with open(path) as file:
                return ast.literal_eval(file.read())
    return None


def load_order(top: str, manifest: Callable[[str], dict[str, object] | None]) -> list[str]:
    """The addons of ``top``'s closure in Odoo's loading order (depth, then name)."""
    depth: dict[str, int] = {}

    def visit(name: str, path: tuple[str, ...]) -> int:
        if name not in depth:
            if name in path:
                raise ValueError(f"dependency cycle through {name}")
            found = manifest(name)
            if found is None:
                raise ValueError(f"unknown addon {name}")
            listed = found.get("depends")
            depends = [str(dep) for dep in listed] if isinstance(listed, list) else []
            if not depends and name != "base":
                depends = ["base"]
            depth[name] = max((visit(dep, (*path, name)) + 1 for dep in depends), default=0)
        return depth[name]

    visit(top, ())
    return sorted(depth, key=lambda name: (depth[name], name))


def _name(value: object) -> object:
    if value is None or isinstance(value, str | bool):
        return value
    name = getattr(value, "__name__", None)
    return name if name and name != "<lambda>" else DYNAMIC


def _selection(field: object) -> object:
    value = getattr(field, "selection", None)
    if isinstance(value, list | tuple):
        return [pair[0] for pair in value]
    return _name(value)


def field_entry(field: object) -> dict[str, object]:
    """What the dump keeps of a set-up field."""
    entry: dict[str, object] = {"type": type(field).__name__}
    for key in ("comodel_name", "inverse_name", "related"):
        value = getattr(field, key, None)
        if value is not None:
            entry[key] = value if isinstance(value, str) else DYNAMIC
    for key in ("store", "readonly", "required", "inherited"):
        entry[key] = bool(getattr(field, key, False))
    entry["compute"] = _name(getattr(field, "compute", None))
    if entry["type"] in {"Selection", "Reference"}:
        entry["selection"] = _selection(field)
    return entry


def build(order: list[str]) -> Registry:
    """A registry with the addons of ``order`` loaded and set up."""
    registry = object.__new__(Registry)  # not ``Registry(name)``: that opens the database
    registry.init(DATABASE)
    cursor = StubCursor()
    cursor.transaction = api.Transaction(registry)
    for name in order:
        importlib.import_module("odoo.addons." + name)
        registry.load(cursor, SimpleNamespace(name=name))
    registry.setup_models(cursor)
    return registry


def main(output: str, tops: list[str]) -> None:
    """Dump the merged fields of each top addon's registry to ``output``."""
    config.parse_config([])
    odoo_module.initialize_sys_path()
    sql_db.db_connect = lambda *args, **kwargs: SimpleNamespace()
    Registry.cursor = lambda self, readonly=False: StubCursor()  # type: ignore[method-assign]
    addons_dirs = list(odoo.addons.__path__)
    dumps: dict[str, object] = {}
    for top in tops:
        order = load_order(top, lambda name: _manifest(addons_dirs, name))
        registry = build(order)
        models = {
            name: {fname: field_entry(field) for fname, field in model._fields.items()}
            for name, model in registry.models.items()
        }
        dumps[top] = {"addons": order, "models": models}
        print(f"{top}: {len(order)} addons, {len(models)} models", file=sys.stderr)
    with open(output, "w") as file:
        json.dump(dumps, file, indent=1, sort_keys=True)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2:])
