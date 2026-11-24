"""The scan of an addon's Python files in Rust (``odoo_scan_rs``), where it is installed.

The extension gives the same scan as :mod:`odoo_typing.scanner.walker` (checked on every
addon of Odoo 17-20); the data files are still read here. ``None`` where Python must
scan the addon: the extension is missing or turned off (``ODOO_TYPING_NATIVE=0``), or a
file it cannot read (bytes that are not UTF-8, syntax newer than this Python, a file it
may not read).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

try:
    import odoo_scan_rs  # type: ignore[import-untyped]  # the optional Rust extension
except ImportError:  # pragma: no cover - the extension is optional
    odoo_scan_rs = None
from odoo_typing.addons import Addon, LoadKey, find_manifest
from odoo_typing.frozen import frozen
from odoo_typing.guards import Guard
from odoo_typing.scanner.results import (
    AddonScan,
    Contribution,
    FieldClass,
    FieldInfo,
    ModuleScan,
    UnlinkedField,
)
from odoo_typing.stamps import Stamps
from odoo_typing.xmlids import scan_data


def _field(item: Any) -> FieldInfo:  # noqa: ANN401 (the shape the extension gives)
    (
        (unlinked, name, field_type),
        (comodel, inverse_name, selection, selection_method, selection_add),
        (related, compute, inverse, search),
        (readonly, required, store, delegate, compute_sql, attachment),
        (dynamic, given, positional),
    ) = item
    values = {
        "name": name,
        "field_type": field_type,
        "comodel": comodel,
        "inverse_name": inverse_name,
        "selection": tuple(selection) if selection is not None else None,
        "selection_method": selection_method,
        "selection_add": tuple(selection_add) if selection_add is not None else None,
        "related": related,
        "compute": compute,
        "inverse": inverse,
        "search": search,
        "readonly": readonly,
        "required": required,
        "store": store,
        "delegate": delegate,
        "compute_sql": compute_sql,
        "attachment": attachment,
        "dynamic": frozen(dynamic),
        "given": tuple(given),
    }
    if unlinked:
        return UnlinkedField(
            **values,
            positional=tuple(
                (text, tuple(keys) if keys is not None else None, method)
                for text, keys, method in positional
            ),
        )
    return FieldInfo(**values)


def _contribution(item: Any) -> Contribution:  # noqa: ANN401
    (
        (addon, module, class_name, model, parents, position),
        (comodels, fields, methods, delegates, inherits),
        (log_access, parent_store, abstract),
        (value_keys, dispatches, passed),
        (assigned, assigned_any, assigned_through, filled, super_returns),
    ) = item
    return Contribution(
        addon=addon,
        module=module,
        class_name=class_name,
        model=model,
        parents=tuple(parents),
        position=tuple(position),
        comodels=tuple(comodels),
        fields=tuple(map(_field, fields)),
        methods=tuple(methods),
        delegates=tuple(delegates),
        inherits=tuple(map(tuple, inherits)),
        log_access=log_access,
        parent_store=parent_store,
        abstract=abstract,
        value_keys=tuple(value_keys),
        dispatches=tuple(
            (method, parameter, tuple(models)) for method, parameter, models in dispatches
        ),
        passed=tuple(
            (callee, parameter, tuple(path), kind) for callee, parameter, path, kind in passed
        ),
        assigned=tuple((method, name, tuple(kinds)) for method, name, kinds in assigned),
        assigned_any=tuple(assigned_any),
        assigned_through=tuple((name, tuple(path)) for name, path in assigned_through),
        filled=tuple((method, tuple(names)) for method, names in filled),
        super_returns=tuple(super_returns),
    )


def rust_scan_addon(
    addon: Addon, load_key: LoadKey, stamps: Stamps | None = None
) -> AddonScan | None:
    """``scan_addon`` with the Python files scanned in Rust; ``None`` where Python must."""
    if odoo_scan_rs is None or os.environ.get("ODOO_TYPING_NATIVE") == "0":
        return None
    found = odoo_scan_rs.scan_addon_modules(
        str(addon.path), addon.module_prefix, addon.name, load_key
    )
    if found is None:
        return None
    modules, found_stamps = found
    stamps = {} if stamps is None else stamps
    stamps.update(found_stamps)
    scans = tuple(
        ModuleScan(
            module=module,
            path=Path(path),
            contributions=tuple(map(_contribution, contributions)),
            referenced_models=frozen(referenced),
            guards=tuple(Guard(*guard) for guard in guards),
            annotations=frozen(map(tuple, annotations)),
            classes=tuple(
                FieldClass(name, tuple(bases), converts, arguments, tuple(passes))
                for name, bases, converts, arguments, passes in classes
            ),
        )
        for module, path, contributions, referenced, guards, annotations, classes in modules
    )
    data = scan_data(addon.name, addon.path, find_manifest(addon.path), stamps)
    return AddonScan(modules=scans, errors=(), data=data)


def native_references(
    source: bytes,
) -> tuple[frozenset[str], tuple[Guard, ...], frozenset[tuple[str, str]]] | None:
    """The models, guards and annotation aliases of a file, read in Rust; ``None`` if not."""
    if odoo_scan_rs is None or os.environ.get("ODOO_TYPING_NATIVE") == "0":
        return None
    found = odoo_scan_rs.file_references(source)
    if found is None:
        return None
    referenced, guards, annotations = found
    return (
        frozen(referenced),
        tuple(Guard(*guard) for guard in guards),
        frozen(map(tuple, annotations)),
    )


def native_file_classes(addon: Addon, path: Path) -> tuple[Contribution, ...] | None:
    """``scan_file_classes`` in Rust; ``None`` where Python must read the file."""
    if odoo_scan_rs is None or os.environ.get("ODOO_TYPING_NATIVE") == "0":
        return None
    found = odoo_scan_rs.file_classes(str(path), str(addon.path), addon.module_prefix, addon.name)
    return None if found is None else tuple(map(_contribution, found))
