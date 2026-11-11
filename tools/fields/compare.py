"""Compare the plugin's scan of field definitions and methods with a runtime dump.

    uv run python tools/fields/compare.py runtime.json /usr/lib/python3/dist-packages/odoo

``runtime.json`` comes from ``tools/fields/dump_runtime.py`` run on the same Odoo. For
every class both know, each argument the runtime gives a field is compared with the
scan; an argument the scan marks dynamic (or the runtime cannot name, a lambda) is
counted apart. Prints the totals and examples of each kind of difference.
"""

from __future__ import annotations

import argparse
import json
import os
from collections import Counter, defaultdict
from collections.abc import Mapping
from pathlib import Path

from odoo_typing.config import Settings
from odoo_typing.registry import Registry
from odoo_typing.scanner import Contribution, FieldInfo

DYNAMIC = "<dynamic>"
EXAMPLES = int(os.environ.get("EXAMPLES", "3"))  # examples printed per kind
# Runtime argument name -> FieldInfo attribute.
ATTRIBUTES = {"comodel_name": "comodel"}


def scanned_classes(odoo_path: Path) -> dict[str, Contribution]:
    """Every class the scan finds in the addons of an Odoo, by full name."""
    registry = Registry(Settings(odoo_path=odoo_path, addons_paths=()))
    classes: dict[str, Contribution] = {}
    for name in registry.addons.addons:
        scan, _ = registry.scan_result(name)
        for module in scan.modules:
            classes.update((item.fullname, item) for item in module.contributions)
    return classes


def scanned_value(info: FieldInfo, argument: str) -> object:
    """The scan's value of a runtime argument, in the dump's format."""
    attribute = ATTRIBUTES.get(argument, argument)
    if attribute in info.dynamic:
        return DYNAMIC
    if argument == "selection":
        keys = info.selection
        return list(keys) if keys is not None else info.selection_method
    if argument == "selection_add":
        return list(info.selection_add) if info.selection_add is not None else None
    return getattr(info, attribute)


def compare(runtime: list[list[object]], classes: Mapping[str, Contribution]) -> None:
    """Print how the scan of each class differs from the runtime dump (totals, examples)."""
    counts: Counter[str] = Counter()
    examples: defaultdict[str, list[str]] = defaultdict(list)

    def note(kind: str, text: str) -> None:
        counts[kind] += 1
        if len(examples[kind]) < EXAMPLES:
            examples[kind].append(text)

    for module, qualname, fields, methods in runtime:
        fullname = f"{module}.{qualname}"
        item = classes.get(fullname)
        if item is None:
            note("class missing in scan", fullname)
            continue
        counts["classes"] += 1
        scanned = {info.name: info for info in item.fields}
        assert isinstance(fields, list) and isinstance(methods, list)
        for name, field_type, arguments in fields:
            info = scanned.get(name)
            if info is None:
                note("field missing in scan", f"{fullname}.{name}")
                continue
            counts["fields"] += 1
            if info.field_type != field_type:
                note("field class differs", f"{fullname}.{name}: {info.field_type} != {field_type}")
            for argument, value in arguments.items():
                mine = scanned_value(info, argument)
                if value == DYNAMIC:
                    counts[f"dynamic at runtime {argument}"] += 1
                elif mine == DYNAMIC:
                    note(f"dynamic in scan {argument}", f"{fullname}.{name}: {value!r}")
                elif mine == value:
                    counts["arguments equal"] += 1
                else:
                    note(f"{argument} differs", f"{fullname}.{name}: {mine!r} != {value!r}")
            for argument in _scanned_arguments(info) - set(arguments):
                note(f"{argument} only in scan", f"{fullname}.{name}")
        for name in set(scanned) - {field[0] for field in fields}:
            note("field only in scan", f"{fullname}.{name}")
        if set(methods) != set(item.methods):
            missing = sorted(set(methods) - set(item.methods))
            extra = sorted(set(item.methods) - set(methods))
            note("methods differ", f"{fullname}: missing {missing[:5]} extra {extra[:5]}")
    for kind, count in sorted(counts.items()):
        print(f"{count:7}  {kind}")
        for text in examples.get(kind, []):
            print(f"         e.g. {text}")


def _scanned_arguments(info: FieldInfo) -> set[str]:
    names = {
        "comodel_name": info.comodel,
        "inverse_name": info.inverse_name,
        "related": info.related,
        "compute": info.compute,
        "inverse": info.inverse,
        "search": info.search,
        "readonly": info.readonly,
        "required": info.required,
        "store": info.store,
        "delegate": info.delegate,
        "selection": info.selection if info.selection is not None else info.selection_method,
        "selection_add": info.selection_add,
    }
    return {name for name, value in names.items() if value is not None}


def main() -> None:
    """Compare a dump file with the scan of an Odoo directory."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("runtime", type=Path)
    parser.add_argument("odoo_path", type=Path)
    args = parser.parse_args()
    runtime = json.loads(args.runtime.read_text())["classes"]
    compare(runtime, scanned_classes(args.odoo_path))


if __name__ == "__main__":
    main()
