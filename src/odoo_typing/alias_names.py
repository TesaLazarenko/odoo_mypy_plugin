"""The names a bare ``odoo.<kind>.<model>`` annotation is looked up through.

mypy looks ``"odoo.values.res_partner"`` up as a qualified name: ``odoo`` in the module
(then in ``builtins``), ``values`` in what it found, ``res_partner`` in that. Neither
Odoo nor the module has those names, and the rewrite of :mod:`odoo_typing.annotations`
reaches only the statements of a module that has a regular class. So the names are
given to mypy where every module finds them:

* ``builtins`` gets a class ``odoo`` whose attributes ``values``, ``model`` and
  ``domain`` are ``Any``, for a module that binds no ``odoo`` of its own (a class, not a
  variable: ``odoo.tools`` in a module that forgot ``import odoo`` stays an error);
* the ``odoo`` package gets the same three as variables, for a module that imports it.

Through a name that is ``Any`` mypy accepts any further name, and makes a symbol whose
full name is the whole path (``builtins.odoo.values.res_partner``,
``odoo.values.res_partner``). It then asks the plugin for a type-analysis hook of that
full name: :func:`named_alias` tells which annotation it is, and the hook gives the
type (:meth:`odoo_typing.hooks.annotations.AnnotationHooks.named`). That works wherever
mypy analyzes a type: a module of plain functions, a class-syntax ``NamedTuple`` or
``TypedDict``, ``cast(...)``, a type alias, a class defined in a function.

The symbols are stored with their module in mypy's cache, so a module analyzed in a
later run finds them before any hook of the plugin is asked. They are bound from the
plugin's hook lookups (one dictionary lookup each once the tree is done): the first is
that of the first class of ``builtins`` itself.
"""

from __future__ import annotations

from typing import Final

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
from mypy.types import AnyType, Instance, TypeOfAny

from odoo_typing.scanner import KINDS, AnnotationAlias

__all__ = [
    "BUILTINS",
    "NAMED_PREFIXES",
    "ROOT",
    "bind_in_builtins",
    "bind_in_package",
    "named_alias",
]

ROOT: Final = "odoo"
BUILTINS: Final = "builtins"
OBJECT: Final = "object"
# The full names of the symbols mypy makes for the annotations start with these.
_PACKAGE_PREFIXES: Final = {f"{ROOT}.{kind}.": kind for kind in KINDS}
_BUILTINS_PREFIXES: Final = {f"{BUILTINS}.{ROOT}.{kind}.": kind for kind in KINDS}
NAMED_PREFIXES: Final = (*_PACKAGE_PREFIXES, *_BUILTINS_PREFIXES)


def named_alias(fullname: str) -> AnnotationAlias | None:
    """``("values", "res_partner")`` for the full name of an annotation's symbol."""
    for prefix, kind in (*_PACKAGE_PREFIXES.items(), *_BUILTINS_PREFIXES.items()):
        if fullname.startswith(prefix) and len(fullname) > len(prefix):
            return kind, fullname[len(prefix) :]
    return None


def bind_in_builtins(tree: MypyFile) -> bool:
    """Give ``builtins`` the class ``odoo``; ``False`` while ``object`` is not analyzed."""
    existing = tree.names.get(ROOT)
    if existing is not None:
        return True  # read from the cache, or bound in this tree before
    found = tree.names.get(OBJECT)
    base = found.node if found is not None else None
    if not isinstance(base, TypeInfo) or not base.mro:
        return False
    defn = ClassDef(ROOT, Block([]))
    defn.fullname = f"{tree.fullname}.{ROOT}"
    info = TypeInfo(SymbolTable(), defn, tree.fullname)
    defn.info = info
    info.bases = [Instance(base, [])]
    info.mro = [info, *base.mro]
    for kind in KINDS:
        variable = _any_variable(kind, f"{defn.fullname}.{kind}")
        variable.info = info
        info.names[kind] = SymbolTableNode(MDEF, variable, plugin_generated=True)
    tree.names[ROOT] = SymbolTableNode(GDEF, info, plugin_generated=True)
    return True


def bind_in_package(tree: MypyFile) -> None:
    """Give the ``odoo`` package the names ``values``, ``model`` and ``domain``.

    A name the package has itself is left alone.
    """
    for kind in KINDS:
        if kind not in tree.names:
            variable = _any_variable(kind, f"{tree.fullname}.{kind}")
            tree.names[kind] = SymbolTableNode(
                GDEF, variable, module_public=False, plugin_generated=True
            )


def _any_variable(name: str, fullname: str) -> Var:
    variable = Var(name, AnyType(TypeOfAny.special_form))
    variable._fullname = fullname  # mypy sets it the same way
    variable.is_ready = True
    return variable
