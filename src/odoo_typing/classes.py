"""mypy class objects for Odoo registry classes: MROs and merged view classes.

The registry (:mod:`odoo_typing.registry`) says which Odoo classes make up a model, in
which order. This module turns that into mypy ``TypeInfo`` state; it is the one place
that writes class state (MROs, bases, symbol-table entries, metadata). The only other
mypy nodes the plugin changes are the assignments of private class attributes, whose
declared type ``plugin._normalize_meta_attributes`` rewrites during semantic analysis.

* ``TypeInfo.mro`` is assigned directly. mypy derives an MRO from the direct bases with
  C3, but the bases of Odoo classes are computed per addon view, so their own MROs may
  order unrelated classes differently and C3 would fail where Odoo does not.
* Fields delegated with ``_inherits`` are entered in the child class's symbol table,
  as the parent's own field variables (so their types are inferred once, where they
  are declared; the cache stores them as cross references).
* A merged "view class" is a ``TypeInfo`` created in the symbol table of the module
  being checked, the way mypy itself stores ad hoc intersections
  (``checker.intersect_instances``): that keeps it in the module's cache entry, so a
  type that refers to it is reloaded correctly. A module reuses the class of a module
  it imports from, so that values passed between them keep one type. Its name starts
  with ``<subclass ``, which ``mypy.lookup.lookup_fully_qualified`` resolves even with
  dots in it.

Each Odoo class records its Python-only MRO (``metadata["odoo_typing"]``), so later
merges respect what Python itself guarantees about it.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from mypy.nodes import (
    GDEF,
    MDEF,
    Block,
    ClassDef,
    MypyFile,
    SymbolTable,
    SymbolTableNode,
    TypeInfo,
    Var,
)
from mypy.types import Instance
from mypy.typestate import type_state
from mypy.typevars import fill_typevars_with_any

from odoo_typing.graphs import c3_merge

__all__ = [
    "METADATA_KEY",
    "Lookup",
    "Merge",
    "add_delegated_fields",
    "create_view_class",
    "install_mro",
    "instance_of",
    "merge_mro",
    "record_python_mro",
    "view_class_name",
    "view_class_origin",
]

METADATA_KEY = "odoo_typing"
VIEW_CLASS_PREFIX = "<subclass of "

Lookup = Callable[[str], TypeInfo | None]


@dataclass(frozen=True, slots=True)
class Merge:
    """Result of merging a class's registry chain with Python's MROs.

    Attributes:
        order: The MRO, the class itself first. (Not named ``mro``: dataclasses would
            take ``type.mro`` for a default value.)
        conflict: Some order constraint had to be broken (Odoo's C3 would fail too).
    """

    order: list[TypeInfo]
    conflict: bool


def instance_of(info: TypeInfo) -> Instance:
    """An instance of a model class; type variables of generic mixins become ``Any``."""
    instance = fill_typevars_with_any(info)
    return instance if isinstance(instance, Instance) else Instance(info, [])


def record_python_mro(info: TypeInfo, lookup: Lookup) -> list[TypeInfo]:
    """The MRO Python gives a class from the bases written in its source.

    The written bases' own Python MROs are used, not the merged ones, and the result
    is recorded in the class's metadata for later merges.
    """
    written = [base.type for base in info.bases]
    merged, _ = c3_merge([*(_python_mro(base, lookup) for base in written), written])
    mro = [info, *merged]
    info.metadata[METADATA_KEY] = {"python_mro": [item.fullname for item in mro[1:]]}
    return mro


def _python_mro(info: TypeInfo, lookup: Lookup) -> list[TypeInfo]:
    """The Python-only MRO of a class: recorded for Odoo classes, else mypy's own."""
    names = info.metadata.get(METADATA_KEY, {}).get("python_mro")
    if not isinstance(names, list):
        return info.mro or [info]
    found = (lookup(name) for name in names)
    return [info, *(item for item in found if item is not None)]


def merge_mro(
    head: TypeInfo, python_mro: Sequence[TypeInfo], chain: Sequence[TypeInfo], lookup: Lookup
) -> Merge:
    """Linearize a class from its registry chain and the Python MROs of its classes.

    Python's constraints go first, then the registry order: like Python's C3 over
    Odoo's runtime registry class, whose bases are the model's classes (each with its
    own Python MRO) followed by the parents' registry classes.

    Args:
        head: The class.
        python_mro: Its own Python MRO (``[head]`` for a merged view class).
        chain: The registry's classes after ``head``, in registry order.
        lookup: Finds a class by full name.
    """
    lists = [list(python_mro), *(_python_mro(item, lookup) for item in chain), [head, *chain]]
    mro, conflict = c3_merge(lists)
    return Merge(order=mro, conflict=conflict)


def install_mro(info: TypeInfo, mro: list[TypeInfo], bases: list[Instance], partial: bool) -> None:
    """Give a class its MRO and direct bases, as ``mypy.mro.calculate_mro`` would.

    Args:
        info: The class.
        mro: Its MRO, itself first.
        bases: Its direct bases (mypy walks them for joins and type arguments).
        partial: Some classes of the chain are not part of this mypy run; unknown
            attributes then give ``Any``, as with a base class of type ``Any``.
    """
    info.bases = bases
    info.mro = mro
    info.fallback_to_any = partial or any(item.fallback_to_any for item in mro)
    type_state.reset_all_subtype_caches_for(info)  # what calculate_mro does too


def view_class_name(model: str, addon: str) -> str:
    """Name of the merged class of a model as an addon sees it."""
    return f'{VIEW_CLASS_PREFIX}"{model}" seen from "{addon}">'


def view_class_origin(info: TypeInfo) -> tuple[str, str] | None:
    """The ``(model, addon)`` a merged view class stands for, if it is one."""
    data = info.metadata.get(METADATA_KEY, {})
    model, addon = data.get("view_model"), data.get("view_addon")
    return (model, addon) if isinstance(model, str) and isinstance(addon, str) else None


def create_view_class(
    tree: MypyFile,
    model: str,
    addon: str,
    chain: Sequence[TypeInfo],
    bases: Sequence[TypeInfo],
    lookup: Lookup,
    partial: bool,
) -> TypeInfo:
    """Create (or return) the merged class of a model in the module being checked.

    Args:
        tree: The module being checked; the class goes into its symbol table.
        model: Odoo model name (also the class's short name, as at runtime).
        addon: The addon whose view the class has.
        chain: The model's classes in registry order (the ones loaded in this run).
        bases: Its direct bases: the tips of the model and of its parents.
        lookup: Finds a class by full name.
        partial: Some classes of the chain are not loaded in this run.
    """
    name = view_class_name(model, addon)
    existing = tree.names.get(name)
    if existing is not None and isinstance(existing.node, TypeInfo):
        return existing.node
    defn = ClassDef(model, Block([]))
    defn.fullname = f"{tree.fullname}.{name}"
    info = TypeInfo(SymbolTable(), defn, tree.fullname)
    defn.info = info
    info.metadata[METADATA_KEY] = {"view_model": model, "view_addon": addon}
    merge = merge_mro(info, [info], chain, lookup)
    install_mro(info, merge.order, [instance_of(base) for base in bases], partial)
    info.metaclass_type = info.calculate_metaclass_type()
    tree.names[name] = SymbolTableNode(GDEF, info, module_hidden=True, plugin_generated=True)
    return info


def add_delegated_fields(info: TypeInfo, sources: Sequence[tuple[TypeInfo, Sequence[str]]]) -> None:
    """Give a class the fields of the models it delegates to (``_inherits``).

    Like Odoo's ``_add_inherited_fields``, a field the class (or its MRO) already has
    wins, and so does the first source that has a name. Only field variables are
    added: Odoo does not delegate methods. Calling it again changes nothing.

    Args:
        info: The class.
        sources: The delegated classes, most derived first, each with the names of
            the fields it declares.
    """
    for source, names in sources:
        for name in names:
            node = source.names.get(name)
            if node is None or not isinstance(node.node, Var) or info.get(name) is not None:
                continue
            info.names[name] = SymbolTableNode(MDEF, node.node, plugin_generated=True)
