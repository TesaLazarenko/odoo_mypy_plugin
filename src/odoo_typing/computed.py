"""Text fields whose compute methods give every record a string.

``move_data = fields.Text(compute="_compute_move_data")`` of Odoo 17's
``account.automatic.entry.wizard`` is assigned ``json.dumps(...)`` in each branch of its
compute method, and the wizard reads ``json.loads(record.move_data)``. Odoo declares the
value ``str | False``, yet a read computes the field, and a compute method that leaves a
record without a value does not make it ``False``: ``Field.__get__`` raises "Compute
method failed to assign" for a field that is computed, not stored and read-only. So such
a field reads as a string when all that is assigned to it is one:

* the field is a ``Char``, ``Text`` or ``Html`` (what is assigned is converted by the
  field: only ``None`` and ``False`` become empty there), has a ``compute`` method, is
  not ``related`` and not stored;
* a class of the model defines the compute method and assigns the field in it;
* every value that the methods of the model's classes assign to the name is a string by
  its syntax or the call of a name whose declared return type is a string
  (:mod:`odoo_typing.scanner.assigned`), and the compute methods write to no name the
  scan cannot read. A ``Char`` or ``Text`` field also takes the true operand of ``a or
  b``, whatever its type: the field converts every value but ``None`` and ``False`` to
  its string (``str(value)``, bytes decoded), which a read gives back (``""`` too);
  an ``Html`` field without ``sanitize`` keeps the value as it is (Odoo 17-19).

For a field with an ``inverse`` or ``readonly=False``, Odoo does not raise: it fills in
``False`` for the records the compute method left without a value (``website.menu.url``
of Odoo 20, ``(page.url if page else menu.manual_url) or "#"`` in each branch). Such a
field reads as a string only when the compute method assigns it to each record on every
path (:func:`odoo_typing.scanner.assigned.filled_attributes`): the last class of the
model that defines the method does, or calls ``super()`` on every path and the class
before it does. A value that the method's records reach through fields is another
model's where the fields say so (``menu.page_id.write({"url": menu.url})``, see
:meth:`odoo_typing.registry.fields.ModelFields.computed_calls`).

The classes are those of the model as the reading addon sees it: an override of the
compute method in an addon of the closure counts, one outside does not. What the scan
cannot see is a value given from outside the model's classes (``create({"move_data":
False})``, an assignment in another model's method), as with ``required``: a field with
an inverse holds what such a write gives it until a read computes it again. Inside its
own compute method the field reads ``False`` until it is assigned, and inside its inverse
method it holds the value written, so reads there keep the declared type.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from typing import Final

from mypy.nodes import Decorator, FuncDef, MypyFile, OverloadedFuncDef, SymbolNode, TypeInfo
from mypy.types import (
    CallableType,
    Instance,
    LiteralType,
    Overloaded,
    Type,
    UnionType,
    get_proper_type,
)

from odoo_typing.scanner import Contribution, FieldInfo
from odoo_typing.scanner.assigned import ANY_NAME, OVERRIDDEN, TEXT, TRUTHY

__all__ = [
    "TEXT_FIELDS",
    "computed_calls",
    "fills",
    "raises_unassigned",
    "recomputed_text",
    "returns_text",
]

TEXT_FIELDS: Final = frozenset({"Char", "Text", "Html"})
CONVERTING: Final = frozenset({"Char", "Text"})  # str(value) of a value but None / False
# The arguments that decide whether a read computes the field and what it does without a value.
DECIDING: Final = frozenset({"compute", "store", "related", "inverse", "readonly"})
TEXT_CLASS: Final = "builtins.str"
BUILTINS: Final = "builtins"

Call = tuple[str, str]  # (module, dotted name as the module writes it)


def recomputed_text(info: FieldInfo) -> bool:
    """Whether a field is a text that a read computes (where it holds no value written)."""
    return (
        info.field_type in TEXT_FIELDS
        and info.compute is not None
        and info.related is None
        and not info.store
        and not info.dynamic & DECIDING
    )


def raises_unassigned(info: FieldInfo) -> bool:
    """Whether a read raises for a record the compute method gave no value (read-only)."""
    return info.inverse is None and info.readonly is not False


def computed_calls(
    info: FieldInfo,
    chain: Iterable[Contribution],
    reaches: Callable[[tuple[str, ...]], bool],
) -> tuple[Call, ...] | None:
    """The calls whose results a recomputed text field holds, if it holds only strings.

    Args:
        info: A field for which :func:`recomputed_text` holds.
        chain: The classes of its model, most derived first.
        reaches: Whether the records that the model's records reach through some fields
            may be of the model (or delegate the field to it).

    Returns:
        ``None`` if the field may be empty; else the calls that must give strings
        (:func:`returns_text`), none if every value is a string by its syntax.
    """
    chain = tuple(chain)
    calls: dict[Call, None] = {}
    computed = False
    for item in chain:
        if info.name in item.assigned_any:
            return None
        if any(name == info.name and reaches(path) for name, path in item.assigned_through):
            return None
        for method, name, kinds in item.assigned:
            if name == ANY_NAME and method == info.compute:
                return None
            if name != info.name:
                continue
            if TRUTHY in kinds and info.field_type not in CONVERTING:
                return None
            computed = computed or method == info.compute
            calls.update(
                dict.fromkeys((item.module, kind) for kind in kinds if kind not in {TEXT, TRUTHY})
            )
    if not computed or not (raises_unassigned(info) or fills(info, chain)):
        return None
    return tuple(calls)


def fills(info: FieldInfo, chain: Iterable[Contribution]) -> bool:
    """Whether the compute method of a field assigns it to each record on every path.

    The method that runs is the one the most derived class defines; one that calls
    ``super()`` on every path hands on to the class before it.
    """
    for item in chain:
        if info.compute not in item.methods:
            continue
        filled = next((names for method, names in item.filled if method == info.compute), ())
        if info.name in filled:
            return True
        if OVERRIDDEN not in filled:
            return False
    return False


def returns_text(modules: Mapping[str, MypyFile], module: str, dotted: str) -> bool:
    """Whether calling a name of a module gives a string, by what it is declared to return.

    ``json.dumps`` in a module that imports ``json``: the name is looked up as the module
    binds it (then in ``builtins``), its attributes in the modules and classes on the way.
    """
    tree = modules.get(module)
    if tree is None:
        return False
    first, *rest = dotted.split(".")
    symbol = tree.names.get(first)
    if symbol is None and BUILTINS in modules:
        symbol = modules[BUILTINS].names.get(first)
    node: SymbolNode | None = symbol.node if symbol is not None else None
    for part in rest:
        if isinstance(node, MypyFile):
            found = node.names.get(part)
        elif isinstance(node, TypeInfo):
            found = node.get(part)
        else:
            return False
        node = found.node if found is not None else None
    return _gives_text(node)


def _gives_text(node: SymbolNode | None) -> bool:
    if isinstance(node, TypeInfo):
        return node.has_base(TEXT_CLASS)  # ``str(x)``, ``Markup(x)``
    if isinstance(node, Decorator):
        declared = None if node.var.is_property else node.var.type
    elif isinstance(node, FuncDef | OverloadedFuncDef):
        declared = node.type
    else:
        return False
    signature = get_proper_type(declared)
    if isinstance(signature, CallableType):
        return _is_text(signature.ret_type)
    if isinstance(signature, Overloaded):
        return all(_is_text(item.ret_type) for item in signature.items)
    return False


def _is_text(typ: Type) -> bool:
    proper = get_proper_type(typ)
    if isinstance(proper, LiteralType):
        proper = proper.fallback
    if isinstance(proper, UnionType):
        return all(_is_text(item) for item in proper.items)
    return isinstance(proper, Instance) and proper.type.has_base(TEXT_CLASS)
