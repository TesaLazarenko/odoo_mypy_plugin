"""Compare the plugin's scan of field definitions and methods with a runtime dump.

    uv run python tools/fields/compare.py runtime.json /usr/lib/python3/dist-packages/odoo
    uv run python tools/fields/compare.py --merged merged.json /usr/lib/python3/dist-packages/odoo

``runtime.json`` comes from ``tools/fields/dump_runtime.py`` run on the same Odoo. For
every class both know, each argument the runtime gives a field is compared with the
scan; an argument the scan marks dynamic (or the runtime cannot name, a lambda) is
counted apart. Prints the totals and examples of each kind of difference.

With ``--merged``, the dump comes from ``tools/fields/dump_registry.py``: the merged
``_fields`` of every model of a top addon's registry, compared with the plugin's
``Registry.fields`` as that addon sees it.
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
from odoo_typing.vals import stored

DYNAMIC = "<dynamic>"
EXAMPLES = int(os.environ.get("EXAMPLES", "3"))  # examples printed per kind
WIDTH = 200  # longest example printed
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


class Report:
    """Counts of each kind of finding, with a few examples of each."""

    def __init__(self) -> None:
        self.counts: Counter[str] = Counter()
        self.examples: defaultdict[str, list[str]] = defaultdict(list)

    def note(self, kind: str, text: str) -> None:
        """Count a finding of ``kind`` and keep ``text`` as an example."""
        self.counts[kind] += 1
        if len(self.examples[kind]) < EXAMPLES:
            self.examples[kind].append(text if len(text) <= WIDTH else text[: WIDTH - 3] + "...")

    def print(self) -> None:
        """Print the totals and the examples."""
        for kind, count in sorted(self.counts.items()):
            print(f"{count:7}  {kind}")
            for text in self.examples.get(kind, []):
                print(f"         e.g. {text}")


def compare(runtime: list[list[object]], classes: Mapping[str, Contribution]) -> None:
    """Print how the scan of each class differs from the runtime dump (totals, examples)."""
    report = Report()
    counts, note = report.counts, report.note
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
            _compare_field(report, f"{fullname}.{name}", info, field_type, arguments)
        for name in set(scanned) - {field[0] for field in fields}:
            note("field only in scan", f"{fullname}.{name}")
        if set(methods) != set(item.methods):
            missing = sorted(set(methods) - set(item.methods))
            extra = sorted(set(item.methods) - set(methods))
            note("methods differ", f"{fullname}: missing {missing[:5]} extra {extra[:5]}")
    report.print()


def _compare_field(
    report: Report, where: str, info: FieldInfo, field_type: str, arguments: dict[str, object]
) -> None:
    """Count how the scan of one field differs from its runtime dump."""
    counts, note = report.counts, report.note
    counts["fields"] += 1
    if info.field_type != field_type:
        note("field class differs", f"{where}: {info.field_type} != {field_type}")
    for argument, value in arguments.items():
        mine = scanned_value(info, argument)
        if value == DYNAMIC:
            counts[f"dynamic at runtime {argument}"] += 1
        elif mine == DYNAMIC:
            note(f"dynamic in scan {argument}", f"{where}: {value!r}")
        elif mine == value:
            counts["arguments equal"] += 1
        else:
            note(f"{argument} differs", f"{where}: {mine!r} != {value!r}")
    for argument in _scanned_arguments(info) - set(arguments):
        note(f"{argument} only in scan", where)


def merged_value(info: FieldInfo, argument: str) -> object:
    """The plugin's value of an argument of a merged field, in the merged dump's format."""
    if argument == "comodel_name":
        return DYNAMIC if "comodel" in info.dynamic else info.comodel
    if argument == "store":
        if "store" in info.dynamic or (info.store is None and "compute" in info.dynamic):
            return DYNAMIC
        return stored(info)
    if argument == "selection":
        if "selection" in info.dynamic:
            return DYNAMIC
        return list(info.selection) if info.selection is not None else info.selection_method
    return DYNAMIC if argument in info.dynamic else getattr(info, argument)


MERGED_ARGUMENTS = ("comodel_name", "inverse_name", "related", "store", "selection")


def compare_merged(dumps: Mapping[str, object], registry: Registry) -> Report:
    """How the plugin's merged fields differ from a ``dump_registry.py`` dump."""
    report = Report()
    for top, dump in dumps.items():
        assert isinstance(dump, dict)
        runtime = dump["models"]
        assert isinstance(runtime, dict)
        known = set(registry.models(top))
        for model in sorted(known - set(runtime)):
            report.note("model only in plugin", f"{top}: {model}")
        for model, fields in sorted(runtime.items()):
            if model not in known:
                report.note("model missing in plugin", f"{top}: {model}")
                continue
            report.counts["models"] += 1
            abstract = not registry.concrete(model, top)
            mine = registry.fields(model, top)
            _compare_fields(report, f"{top}: {model}", fields, mine, abstract)
    return report


def _compare_fields(
    report: Report,
    where: str,
    runtime: Mapping[str, Mapping[str, object]],
    mine: Mapping[str, FieldInfo],
    abstract: bool,
) -> None:
    # Deliberate: a mixin gets the service and delegated fields of the models using it.
    only = "field only in plugin (abstract model)" if abstract else "field only in plugin"
    for name in sorted(set(mine) - set(runtime)):
        report.note(only, f"{where}.{name}")
    for name, entry in sorted(runtime.items()):
        info = mine.get(name)
        if info is None:
            report.note("field missing in plugin", f"{where}.{name}")
            continue
        report.counts["fields"] += 1
        if info.field_type != entry["type"]:
            report.note(
                "field class differs", f"{where}.{name}: {info.field_type} != {entry['type']}"
            )
        for argument in MERGED_ARGUMENTS:
            value, own = entry.get(argument), merged_value(info, argument)
            if value == DYNAMIC:
                report.counts[f"dynamic at runtime {argument}"] += 1
            elif own == DYNAMIC:
                report.note(f"dynamic in plugin {argument}", f"{where}.{name}: {value!r}")
            elif own == value:
                report.counts["arguments equal"] += 1
            elif isinstance(own, list) and isinstance(value, list) and sorted(own) == sorted(value):
                report.note(f"{argument} order differs", f"{where}.{name}")
            else:
                report.note(f"{argument} differs", f"{where}.{name}: {own!r} != {value!r}")


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
    parser.add_argument("--merged", action="store_true", help="a dump_registry.py dump")
    parser.add_argument("runtime", type=Path)
    parser.add_argument("odoo_path", type=Path)
    args = parser.parse_args()
    dump = json.loads(args.runtime.read_text())
    if args.merged:
        registry = Registry(Settings(odoo_path=args.odoo_path, addons_paths=()))
        compare_merged(dump, registry).print()
    else:
        compare(dump["classes"], scanned_classes(args.odoo_path))


if __name__ == "__main__":
    main()
