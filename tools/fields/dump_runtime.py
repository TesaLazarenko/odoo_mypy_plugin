"""Import every addon of an installed Odoo and dump what each model class declares.

Run with the interpreter of that Odoo (no database is needed), e.g. for the Odoo 18 deb:

    /usr/bin/python3 -I tools/fields/dump_runtime.py runtime.json

For each class Odoo registers (``MetaModel.module_to_models``): its module and name, the
fields its body declares (``_field_definitions``: class name and the arguments given),
and the names of the functions it defines. ``tools/fields/compare.py`` compares this
with the plugin's scan.
"""

from __future__ import annotations

import importlib
import json
import logging
import os
import sys
import types

logging.disable(logging.CRITICAL)

import odoo  # noqa: E402
from odoo.models import MetaModel  # noqa: E402
from odoo.modules import module as odoo_module  # noqa: E402
from odoo.tools import config  # noqa: E402

TEXTS = ("comodel_name", "inverse_name", "related")
METHODS = ("compute", "inverse", "search")
FLAGS = ("readonly", "required", "store", "delegate")
DYNAMIC = "<dynamic>"


def _method(value: object) -> str:
    if isinstance(value, str):
        return value
    name = getattr(value, "__name__", None)
    return name if name and name != "<lambda>" else DYNAMIC


def _keys(value: object) -> list[object] | str:
    if isinstance(value, list | tuple):
        return [pair[0] for pair in value]
    return DYNAMIC


def _arguments(args: dict[str, object]) -> dict[str, object]:
    found: dict[str, object] = {}
    for key in TEXTS:
        if key in args:
            found[key] = args[key] if isinstance(args[key], str) else DYNAMIC
    for key in METHODS:
        if key in args:
            found[key] = _method(args[key])
    for key in FLAGS:
        if key in args:
            found[key] = args[key] if isinstance(args[key], bool) else DYNAMIC
    if "selection" in args:
        value = args["selection"]
        found["selection"] = _keys(value) if isinstance(value, list | tuple) else _method(value)
    if "selection_add" in args:
        found["selection_add"] = _keys(args["selection_add"])
    return found


def _class(cls: type) -> list[object]:
    fields = [
        [field.name, type(field).__name__, _arguments(field._args__ or {})]
        for field in cls.__dict__.get("_field_definitions", ())
        if not (field._args__ or {}).get("automatic")
    ]
    kinds = (types.FunctionType, staticmethod, classmethod, property)
    methods = [name for name, value in vars(cls).items() if isinstance(value, kinds)]
    return [cls.__module__, cls.__qualname__, fields, methods]


def main(output: str) -> None:
    """Import the addons and write the dump to ``output``."""
    config.parse_config([])
    odoo_module.initialize_sys_path()
    addons_dir = os.path.join(os.path.dirname(odoo.__file__), "addons")
    names = sorted(
        name
        for name in os.listdir(addons_dir)
        if os.path.isfile(os.path.join(addons_dir, name, "__manifest__.py"))
    )
    failed = {}
    for name in names:
        try:
            importlib.import_module("odoo.addons." + name)
        except Exception as error:  # an addon whose imports fail here is left out
            failed[name] = repr(error)[:200]
    classes = [_class(cls) for found in MetaModel.module_to_models.values() for cls in found]
    with open(output, "w") as file:
        json.dump({"classes": classes, "failed": failed}, file)
    print(f"{len(classes)} classes; {len(failed)} addons failed to import", file=sys.stderr)


if __name__ == "__main__":
    main(sys.argv[1])
