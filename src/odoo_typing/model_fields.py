"""The fields of a model's registry class, merged from its definitions as Odoo sets them up.

Odoo (``Field._get_attrs``, ``Selection._setup_attrs``, ``BaseModel._add_inherited_fields``):

- The field of a name is the value the registry class's MRO gives first; the definitions
  merged into it are the field definitions of that name in MRO order, up to the first
  class where the name is something else (a method).
- From the base definition to the most derived one, each argument a definition passes
  overrides the earlier value. A definition whose class the most derived one does not
  subclass (``Char`` redefined as ``Text``) drops everything gathered before it.
- ``selection`` replaces the keys; each ``selection_add`` adds its new keys.
- ``_inherits`` (and ``Many2one(delegate=True)``) adds each field of the delegated model
  that the model does not define, as ``related="<delegate field>.<name>"``.
- ``id``, ``display_name`` and the log-access fields are added to every model.

This module has the pure parts; :meth:`odoo_typing.registry.Registry.fields` feeds it
the classes of a model's view.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import replace
from typing import Final, TypeVar

from odoo_typing.frozen import frozen
from odoo_typing.scanner import Contribution, FieldInfo
from odoo_typing.scanner.fields import ANY_ARGUMENT

__all__ = [
    "LOG_ACCESS_FIELDS",
    "MAGIC_FIELDS",
    "MAGIC_FIELDS_17",
    "chain_definitions",
    "delegated_field",
    "delegation_fields",
    "literal_argument",
    "merge_definitions",
    "merge_keys",
    "service_definitions",
    "stored",
    "value_required",
]

ArgumentT = TypeVar("ArgumentT")
MAX_RELATED: Final = 8  # nested related= paths followed by value_required
# Field classes that subclass another field class (Odoo 18-20); a redefinition with a
# subclass keeps the arguments of the earlier definitions.
FIELD_PARENTS: Final = {"Image": "Binary", "Reference": "Selection", "Many2oneReference": "Integer"}
# Arguments one definition passes and a later one overrides.
OVERRIDABLE: Final = (
    "comodel",
    "inverse_name",
    "related",
    "compute",
    "inverse",
    "search",
    "readonly",
    "required",
    "store",
    "delegate",
    "compute_sql",
    "attachment",
)
# Added by MetaModel to every class that defines a model (Odoo 18+: ``display_name``
# has a search method; in Odoo 17 it has none, see :func:`service_definitions`).
MAGIC_FIELDS: Final = (
    FieldInfo("id", "Id"),
    FieldInfo(
        "display_name", "Char", compute="_compute_display_name", search="_search_display_name"
    ),
)
MAGIC_FIELDS_17: Final = (
    MAGIC_FIELDS[0],
    FieldInfo("display_name", "Char", compute="_compute_display_name"),
)
# Added with them unless ``_log_access`` is off. Odoo does not declare the dates required,
# but ``create`` and ``write`` set them on every record, so a saved record has them like
# the value of a required field.
LOG_ACCESS_FIELDS: Final = (
    FieldInfo("create_uid", "Many2one", comodel="res.users", readonly=True),
    FieldInfo("create_date", "Datetime", readonly=True, required=True),
    FieldInfo("write_uid", "Many2one", comodel="res.users", readonly=True),
    FieldInfo("write_date", "Datetime", readonly=True, required=True),
)


def chain_definitions(chain: Sequence[Contribution]) -> dict[str, list[FieldInfo]]:
    """The definitions of each field of a registry class, base first.

    Args:
        chain: The classes of the registry class, most derived first.

    Returns:
        Field name to its definitions, from the base one to the most derived; the
        names in the order the classes, base first, declare them.
    """
    found: dict[str, list[FieldInfo]] = {}
    stopped: set[str] = set()
    for item in chain:
        for info in item.fields:
            if info.name not in stopped:
                found.setdefault(info.name, []).append(info)
        stopped.update(item.methods)
    names = (info.name for item in reversed(chain) for info in item.fields)
    return {name: found[name][::-1] for name in dict.fromkeys(names) if name in found}


def merge_definitions(definitions: Sequence[FieldInfo]) -> FieldInfo:
    """The field the definitions of one name make, base definition first.

    The result passes every argument the merge resolves; ``selection_add`` is folded
    into ``selection``, and ``dynamic`` names what stays unknown.
    """
    tip = definitions[-1]
    merged: FieldInfo | None = None
    for definition in definitions:
        if not _compatible(tip.field_type, definition.field_type):
            merged = None
            continue
        merged = _override(merged, definition)
    assert merged is not None  # the tip is compatible with itself
    merged = replace(merged, field_type=tip.field_type, selection_add=None)
    # Most fields have one definition, which the merge gives back as it is: keep the scan's
    # object rather than an equal copy (3174 of 3770 merged fields on sale).
    return tip if merged == tip else merged


def _compatible(field_type: str, base_type: str) -> bool:
    """Whether a field class is (a subclass of) another one."""
    while field_type != base_type and field_type in FIELD_PARENTS:
        field_type = FIELD_PARENTS[field_type]
    return field_type == base_type


def merge_keys(*sequences: Sequence[str]) -> tuple[str, ...]:
    """Odoo's ``merge_sequences``: the union, in the partial order each sequence gives.

    A key that ``selection_add`` repeats is an anchor: the new keys before it go before
    it (``[("a", ...), ("old", ...)]`` puts ``a`` right before ``old``), the others last.
    """
    before: dict[str, list[str]] = {}  # key -> the keys that precede it in a sequence
    for sequence in sequences:
        previous: str | None = None
        for key in sequence:
            before.setdefault(key, [])
            if previous is not None:
                before[key].append(previous)
            previous = key
    result: list[str] = []
    visited: set[str] = set()

    def visit(key: str) -> None:
        if key not in visited:
            visited.add(key)
            for earlier in before[key]:
                visit(earlier)
            result.append(key)

    for key in before:
        visit(key)
    return tuple(result)


def _override(merged: FieldInfo | None, definition: FieldInfo) -> FieldInfo:
    """``merged`` with the arguments ``definition`` passes."""
    if merged is None:
        merged = FieldInfo(definition.name, definition.field_type)
    passed = {name for name, value in _arguments(definition).items() if value is not None}
    passed |= definition.dynamic & set(OVERRIDABLE)
    dynamic = (merged.dynamic - passed) | (definition.dynamic & passed)

    def take(old: ArgumentT | None, new: ArgumentT | None, name: str) -> ArgumentT | None:
        return new if name in passed else old

    selection, method = merged.selection, merged.selection_method
    if _passes_selection(definition):
        selection, method = definition.selection, definition.selection_method
        dynamic = (dynamic - {"selection"}) | (definition.dynamic & {"selection"})
    if definition.selection_add is not None and selection is not None:
        selection = merge_keys(selection, definition.selection_add)
    elif definition.selection_add is not None or "selection_add" in definition.dynamic:
        selection = None  # added to a selection the scan does not know
        dynamic |= {"selection"}
    return FieldInfo(
        merged.name,
        definition.field_type,
        comodel=take(merged.comodel, definition.comodel, "comodel"),
        inverse_name=take(merged.inverse_name, definition.inverse_name, "inverse_name"),
        selection=selection,
        selection_method=method,
        related=take(merged.related, definition.related, "related"),
        compute=take(merged.compute, definition.compute, "compute"),
        inverse=take(merged.inverse, definition.inverse, "inverse"),
        search=take(merged.search, definition.search, "search"),
        readonly=take(merged.readonly, definition.readonly, "readonly"),
        required=take(merged.required, definition.required, "required"),
        store=take(merged.store, definition.store, "store"),
        delegate=take(merged.delegate, definition.delegate, "delegate"),
        compute_sql=take(merged.compute_sql, definition.compute_sql, "compute_sql"),
        attachment=take(merged.attachment, definition.attachment, "attachment"),
        dynamic=frozen(dynamic - {"selection_add"}),
        given=_merged_given(merged.given, definition.given),
    )


def _merged_given(earlier: tuple[str, ...], later: tuple[str, ...]) -> tuple[str, ...]:
    """``FieldInfo.given`` of two definitions: an argument passed again replaces the first.

    Arguments the later definition passes without names (``*``) may be any of them: what
    the earlier ones passed is not known to hold then.
    """
    if not earlier or ANY_ARGUMENT in later or ANY_ARGUMENT in earlier:
        return later  # no literal of the earlier ones is known to hold
    if not later:
        return earlier
    merged = {item.partition("=")[0]: item for item in earlier}
    merged.update((item.partition("=")[0], item) for item in later)
    return tuple(sorted(merged.values()))


def literal_argument(info: FieldInfo, name: str) -> str | None:
    """The type of the true literal a field's definitions pass for an argument, if they do.

    ``"int"`` for ``size`` of ``fields.Char(size=5)``; ``None`` for an argument that is
    not passed, passed another value, or that may be among unnamed ones (``**kwargs``).
    """
    prefix = f"{name}="
    if ANY_ARGUMENT in info.given:
        return None
    return next((item[len(prefix) :] for item in info.given if item.startswith(prefix)), None)


def _arguments(info: FieldInfo) -> dict[str, str | bool | None]:
    """The overridable arguments of a definition by name (see :data:`OVERRIDABLE`)."""
    return {
        "comodel": info.comodel,
        "inverse_name": info.inverse_name,
        "related": info.related,
        "compute": info.compute,
        "inverse": info.inverse,
        "search": info.search,
        "readonly": info.readonly,
        "required": info.required,
        "store": info.store,
        "delegate": info.delegate,
        "compute_sql": info.compute_sql,
        "attachment": info.attachment,
    }


def _passes_selection(definition: FieldInfo) -> bool:
    return (
        definition.selection is not None
        or definition.selection_method is not None
        or "selection" in definition.dynamic
    )


def delegation_fields(
    chain: Sequence[Contribution], fields: Mapping[str, FieldInfo]
) -> dict[str, str]:
    """The merged ``_inherits`` of a registry class: delegated model to its field name.

    ``_build_model`` merges the ``_inherits`` of the classes (the most derived wins);
    each merged ``Many2one(delegate=True)`` adds its comodel.
    """
    found: dict[str, str] = {}
    for item in reversed(chain):
        found.update(item.inherits)
    for name, info in fields.items():
        if info.delegate and info.comodel:
            found[info.comodel] = name
    return found


def delegated_field(info: FieldInfo, delegate_field: str) -> FieldInfo:
    """The related field ``_add_inherited_fields`` makes for a delegated model's field.

    It is required when the delegated model's field is (``Field.setup_related``): a
    record always has its delegated record, so it reads that field's value.
    """
    return FieldInfo(
        info.name,
        info.field_type,
        comodel=info.comodel,
        selection=info.selection,
        selection_method=info.selection_method,
        related=f"{delegate_field}.{info.name}",
        readonly=info.readonly,
        required=info.required,
        dynamic=info.dynamic & {"comodel", "selection", "readonly", "required"},
    )


def service_definitions(log_access: bool, version: int | None = None) -> Iterable[FieldInfo]:
    """The fields Odoo adds to a model besides those its classes declare (in an Odoo version)."""
    yield from MAGIC_FIELDS_17 if version == 17 else MAGIC_FIELDS
    if log_access:
        yield from LOG_ACCESS_FIELDS


def stored(info: FieldInfo) -> bool:
    """Whether a field is stored: computed and related fields are not, by default."""
    if info.store is not None:
        return info.store
    return info.compute is None and info.related is None and not info.dynamic & {"compute"}


def value_required(
    info: FieldInfo,
    model: str,
    fields_of: Callable[[str], Mapping[str, FieldInfo]],
    computed: Callable[[str, FieldInfo], bool] | None = None,
    depth: int = 0,
) -> bool:
    """Whether a field of a saved record always has a value: required, or related to one.

    Odoo copies ``required`` only to the fields delegated through ``_inherits``. A
    ``related=`` path whose hops are required ``Many2one`` fields and whose last field is
    required (``product.template.attribute.value.name``, related to
    ``product_attribute_value_id.name``) reads a value just as surely. So does a field
    that ``computed`` says its compute methods always give a value
    (:mod:`odoo_typing.computed`), also as the last field of such a path.
    """
    if info.required is True:
        return True
    if computed is not None and computed(model, info):
        return True
    if info.required is not None or depth > MAX_RELATED:
        return False
    if not info.related or "related" in info.dynamic:
        return False
    current = model
    hops = info.related.split(".")
    for index, hop in enumerate(hops):
        field = fields_of(current).get(hop)
        if field is None or not value_required(field, current, fields_of, computed, depth + 1):
            return False
        if index < len(hops) - 1:
            if field.field_type != "Many2one" or not field.comodel or "comodel" in field.dynamic:
                return False
            current = field.comodel
    return True
