"""mypy class objects for Odoo registry classes: MROs and merged view classes.

The registry (:mod:`odoo_typing.registry`) says which Odoo classes make up a model, in
which order. This module turns that into mypy ``TypeInfo`` state; it is the one place
that writes class state (MROs, bases, symbol-table entries, metadata). The only other
mypy nodes the plugin changes are the assignments of private class attributes, whose
declared type ``hooks.meta_attributes.normalize_meta_attributes`` rewrites during semantic
analysis.

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
* The ``TypedDict`` of a model's values (``odoo.values.<model>``) is created the same
  way, in the module that uses the annotation (``<values of res_partner>``: no dots), and
  so is the marker of a model's domains (``odoo.domain.<model>``, ``<domain of
  res_partner>``): a subclass of ``list[Any]`` (and of ``Domain`` in Odoo 19+) whose
  metadata names the model, in a union with the types a domain has.

* Odoo 19+ declares relational fields as ``Field[BaseModel]``: the classes listed in
  the profile get a type variable for the comodel, as Odoo 18 declares them.

Each Odoo class records its Python-only MRO (``metadata["odoo_typing"]``), so later
merges respect what Python itself guarantees about it.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from mypy.mro import calculate_mro
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
from mypy.types import (
    AnyType,
    Instance,
    Type,
    TypedDictType,
    TypeOfAny,
    TypeVarId,
    TypeVarType,
    UnionType,
    get_proper_type,
)
from mypy.typestate import type_state
from mypy.typevars import fill_typevars_with_any

from odoo_typing.graphs import c3_merge

__all__ = [
    "METADATA_KEY",
    "Lookup",
    "Merge",
    "add_delegated_fields",
    "add_log_access_fields",
    "create_domain_class",
    "create_values_class",
    "create_view_class",
    "domain_class_model",
    "domain_type_model",
    "install_mro",
    "instance_of",
    "make_comodel_generic",
    "merge_mro",
    "record_python_mro",
    "values_class_model",
    "values_class_name",
    "view_class_name",
    "view_class_origin",
]

METADATA_KEY = "odoo_typing"
VIEW_CLASS_PREFIX = "<subclass of "
VALUES_CLASS_PREFIX = "<values of "
DOMAIN_CLASS_PREFIX = "<domain of "

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


def values_class_name(model: str, version: str) -> str:
    """Name of the ``TypedDict`` class of a model's values (``odoo.values.<model>``).

    Without dots: ``mypy.lookup.lookup_fully_qualified`` splits a full name on them
    (only ``<subclass `` names are kept whole), and the cache finds a class by its
    full name. Two models whose names differ only in ``.`` / ``_`` are never both
    resolved in one view (:meth:`odoo_typing.registry.Registry.resolve_alias`). The
    ``version`` (a digest of the keys) makes a class with other keys another class:
    the daemon merges a module analyzed again into its earlier symbols by full name,
    and would keep the earlier keys under the same name.
    """
    return f"{VALUES_CLASS_PREFIX}{model.replace('.', '_')} {version}>"


def values_class_model(info: TypeInfo) -> str | None:
    """The model whose values a ``TypedDict`` class stands for, if it is such a class."""
    model = info.metadata.get(METADATA_KEY, {}).get("values_model")
    return model if isinstance(model, str) else None


def create_values_class(
    tree: MypyFile,
    model: str,
    items: dict[str, Type],
    typed_dict: Instance,
    dict_base: Instance,
    version: str,
) -> Type:
    """Create (or reuse) the ``TypedDict`` of a model's values in a module; return its type.

    A named ``TypedDict`` (``total=False``), shown as ``"<model> values"``, so that
    mypy's messages name it rather than list hundreds of keys. Built the way mypy builds
    one: the class's own ``typeddict_type`` falls back to ``typing._TypedDict``, and the
    type of an annotation is its alias target, whose fallback is the class; a class that
    falls back to itself makes mypy recurse forever (on ``update()``, ``{**vals}``). Its
    second base, ``dict[str, Any]``, lets the values go where Odoo 19+ annotates
    ``create`` / ``write`` (and any function) with a ``dict``.

    Args:
        tree: The module that uses the annotation; the class goes into its symbol table.
        model: Odoo model name.
        items: Field name to the type of the values it takes.
        typed_dict: ``typing._TypedDict``.
        dict_base: ``dict[str, Any]``.
        version: Digest of the fields ``items`` was made from (part of the class name).
    """
    name = values_class_name(model, version)
    existing = tree.names.get(name)
    if existing is not None and isinstance(existing.node, TypeInfo):
        info = existing.node
    else:
        defn = ClassDef(f"{model} values", Block([]))
        defn.fullname = f"{tree.fullname}.{name}"
        info = TypeInfo(SymbolTable(), defn, tree.fullname)
        defn.info = info
        info.bases = [typed_dict, dict_base]
        calculate_mro(info)
        info.metadata[METADATA_KEY] = {"values_model": model}
        info.update_typeddict_type(TypedDictType(items, set(), set(), typed_dict))
        tree.names[name] = SymbolTableNode(GDEF, info, module_hidden=True, plugin_generated=True)
    assert info.special_alias is not None  # set by update_typeddict_type
    return info.special_alias.target


def create_domain_class(tree: MypyFile, model: str, bases: list[Instance]) -> Instance:
    """Create (or reuse) the marker of a model's domains in a module (``odoo.domain.<model>``).

    A class without members, shown as ``"<model> domain"``, whose metadata names the
    model; its bases (``list[Any]``, and ``Domain`` in Odoo 19+) let it stand in a union
    with them without narrowing anything they allow. Its name has no digest: nothing in
    it can change, so the daemon may merge it by name.

    Args:
        tree: The module that uses the annotation; the class goes into its symbol table.
        model: Odoo model name.
        bases: ``list[Any]``, and ``Domain`` where Odoo has it.
    """
    name = f"{DOMAIN_CLASS_PREFIX}{model.replace('.', '_')}>"
    existing = tree.names.get(name)
    if existing is not None and isinstance(existing.node, TypeInfo):
        return Instance(existing.node, [])
    defn = ClassDef(f"{model} domain", Block([]))
    defn.fullname = f"{tree.fullname}.{name}"
    info = TypeInfo(SymbolTable(), defn, tree.fullname)
    defn.info = info
    info.bases = bases
    calculate_mro(info)
    info.metadata[METADATA_KEY] = {"domain_model": model}
    tree.names[name] = SymbolTableNode(GDEF, info, module_hidden=True, plugin_generated=True)
    return Instance(info, [])


def domain_class_model(info: TypeInfo) -> str | None:
    """The model whose domains a marker class stands for, if it is such a class."""
    model = info.metadata.get(METADATA_KEY, {}).get("domain_model")
    return model if isinstance(model, str) else None


def domain_type_model(typ: Type | None) -> str | None:
    """The model a declared type is a domain of (``list[Any] | <res.partner domain>``)."""
    if typ is None:
        return None
    proper = get_proper_type(typ)
    for item in proper.items if isinstance(proper, UnionType) else [proper]:
        found = get_proper_type(item)
        if isinstance(found, Instance) and (model := domain_class_model(found.type)) is not None:
            return model
    return None


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


def add_log_access_fields(info: TypeInfo, field_types: dict[str, Instance]) -> None:
    """Declare the log-access fields Odoo adds at run time (``create_uid``...).

    A field the class or its MRO already has wins (a model may declare
    ``create_date`` itself). Calling it again changes nothing.

    Args:
        info: The model class.
        field_types: Field name to the field's descriptor type (``fields.Datetime``).
    """
    for name, field_type in field_types.items():
        if info.get(name) is not None:
            continue
        var = Var(name, field_type)
        var.info = info
        var._fullname = f"{info.fullname}.{name}"
        var.is_initialized_in_class = True
        info.names[name] = SymbolTableNode(MDEF, var, plugin_generated=True)


def make_comodel_generic(
    defn: ClassDef, upper_bound: Instance, is_field_class: Callable[[str], bool]
) -> None:
    """Make a field class generic in its comodel: ``Many2one`` becomes ``Many2one[M]``.

    Its field-class bases with one type variable get ``M`` as argument
    (``Field[BaseModel]`` becomes ``Field[M]``, ``_Relational`` becomes
    ``_Relational[M]``), so that the comodel reaches ``Field.__get__``. Runs each time
    mypy computes the class's MRO (it resets the type variables before).
    """
    info = defn.info
    if not defn.type_vars:
        defn.type_vars = [
            TypeVarType(
                "M",
                f"{info.fullname}.M",
                id=TypeVarId(1),
                values=[],
                upper_bound=upper_bound,
                default=AnyType(TypeOfAny.from_omitted_generics),
            )
        ]
        info.type_vars = []
        info.add_type_vars()
    comodel = defn.type_vars[0]
    info.bases = [
        base.copy_modified(args=[comodel])
        if is_field_class(base.type.fullname) and len(base.type.type_vars) == 1
        else base
        for base in info.bases
    ]
