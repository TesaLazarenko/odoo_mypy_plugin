"""Types for ``odoo.values.*`` and ``odoo.model.*`` annotations (type-analysis hooks).

See :mod:`odoo_typing.annotations` for the forms and how bare strings reach mypy. The
types are made during semantic analysis, in the view of the annotating file's addon:

* ``odoo.values.<model>``: a named ``TypedDict`` of the model's values
  (:func:`odoo_typing.classes.create_values_class`), whose keys are the fields any addon
  gives the model (:meth:`odoo_typing.registry.Registry.values_fields`);
* ``odoo.model.<model>``: the model's records, as ``env["<model>"]`` gives them
  (:meth:`odoo_typing.hooks.models.Models.view_type_in`).

An alias that names no model of the view is reported where it is written, as
``env[...]`` reports an unknown name. Files outside any addon keep plain mypy's view:
``Annotated[...]`` is its first argument there.
"""

from __future__ import annotations

from collections.abc import Callable
from difflib import get_close_matches

from mypy.errorcodes import VALID_TYPE
from mypy.nodes import MypyFile, TypeInfo
from mypy.plugin import AnalyzeTypeContext, MethodContext, MethodSigContext
from mypy.plugins.default import typed_dict_get_callback, typed_dict_get_signature_callback
from mypy.server.trigger import make_trigger, make_wildcard_trigger
from mypy.typeanal import TypeAnalyser
from mypy.types import (
    AnyType,
    CallableType,
    FunctionLike,
    Instance,
    Type,
    TypedDictType,
    TypeOfAny,
    UnboundType,
    get_proper_type,
)

# isort: split
# After mypy.types: mypy.semanal imports modules that import each other at import time.
from mypy.semanal import SemanticAnalyzer

from odoo_typing.annotations import TRIGGER, ItemTypes, annotation_alias, trigger_alias
from odoo_typing.classes import create_values_class, values_class_model
from odoo_typing.hooks.models import (
    ODOO_MODEL,
    ODOO_MODEL_DEPENDS,
    Hooks,
    Pending,
    not_in_dependencies,
)
from odoo_typing.records import digest
from odoo_typing.registry.annotations import normalized_alias
from odoo_typing.scanner import VALUES

__all__ = ["DICT_GET", "AnnotationHooks", "values_get", "values_get_signature"]

# ``vals.get(...)`` on ``odoo.values.*``: its ``dict`` base comes before ``Mapping`` in the
# MRO, so the method is ``dict.get``, which mypy's TypedDict hooks (``Mapping.get``) skip.
DICT_GET = "builtins.dict.get"

# Classes a ``dict`` annotation may be (``Annotated[dict, "odoo.values.x"]``).
DICT_CLASSES = frozenset(
    {"builtins.dict", "typing.Mapping", "typing.MutableMapping", "collections.abc.Mapping"}
)


class AnnotationHooks(Hooks):
    """Turn ``odoo.values.*`` / ``odoo.model.*`` annotations into types."""

    def annotated(self, fullname: str, ctx: AnalyzeTypeContext) -> Type:
        """``Annotated[...]``: the values of a model if its metadata names them, else mypy's."""
        alias = _values_alias(ctx.type)
        analyzer = _type_analyzer(ctx)
        if alias is None or analyzer is None or self._place(ctx) is None:
            default = (
                analyzer.try_analyze_special_unbound_type(ctx.type, fullname) if analyzer else None
            )
            return default if default is not None else AnyType(TypeOfAny.from_error)
        base = ctx.api.analyze_type(ctx.type.args[0])
        if not _is_dict(base):
            ctx.api.fail(
                f'"odoo.values.{alias}" needs a dict type in Annotated[...]',
                ctx.context,
                code=VALID_TYPE,
            )
            return base
        return self._values(alias, ctx)

    def rewritten(self, ctx: AnalyzeTypeContext) -> Type:
        """A rewritten bare-string annotation (``__build_class__["values", "x"]``)."""
        found = trigger_alias(ctx.type)
        if found is None:  # someone wrote ``x: __build_class__`` (what mypy would say)
            ctx.api.fail(
                'Function "builtins.__build_class__" is not valid as a type',
                ctx.context,
                code=VALID_TYPE,
            )
            return AnyType(TypeOfAny.from_error)
        kind, alias = found
        return self._values(alias, ctx) if kind == VALUES else self._records(alias, ctx)

    # -- types -------------------------------------------------------------------------------

    def _values(self, alias: str, ctx: AnalyzeTypeContext) -> Type:
        """The ``TypedDict`` of a model's values; ``dict[str, Any]`` if the alias is unknown."""
        place = self._place(ctx)
        model = self._model(alias, ctx) if place is not None else None
        api = ctx.api
        any_type = AnyType(TypeOfAny.special_form)
        dict_type = api.named_type("builtins.dict", [api.named_type("builtins.str", []), any_type])
        if place is None or model is None:
            return dict_type
        analyzer, tree, addon_name = place
        self._depend_on_view(analyzer, model, addon_name, keys=True)
        named = _named_finder(analyzer)
        items = ItemTypes(named, self._profile.base_model, self._profile.new_id)
        fields = self.registry.values_fields(model)
        typed_dict = api.named_type("typing._TypedDict", [])
        version = digest(sorted((name, sorted(classes)) for name, classes in fields.items()))
        return create_values_class(
            tree, model, items.items(fields), typed_dict, dict_type, version[:16]
        )

    def _records(self, alias: str, ctx: AnalyzeTypeContext) -> Type:
        """A model's records as the addon sees them (``env[model]``); ``Any`` if unknown."""
        place = self._place(ctx)
        model = self._model(alias, ctx) if place is not None else None
        if place is None or model is None:
            return AnyType(TypeOfAny.special_form)
        analyzer, tree, addon_name = place
        self._depend_on_view(analyzer, model, addon_name)
        registry = self.registry
        if not registry.loaded_with(model, addon_name, tree.fullname, tree.path):
            return AnyType(TypeOfAny.special_form)
        found = self._models.view_type_in(
            model, addon_name, analyzer.modules, tree, analyzer.is_incomplete_namespace
        )
        if isinstance(found, Pending):
            if not analyzer.final_iteration:
                analyzer.defer()  # a class of the model is not analyzed yet
            return AnyType(TypeOfAny.special_form)
        return found if found is not None else AnyType(TypeOfAny.special_form)

    # -- model names -------------------------------------------------------------------------

    def _model(self, alias: str, ctx: AnalyzeTypeContext) -> str | None:
        """The model an alias names in the file's addon view; reported if it names none."""
        place = self._place(ctx)
        if place is None:
            return None
        _, _, addon_name = place
        resolution = self.registry.resolve_alias(alias, addon_name)
        if resolution.model is not None:
            return resolution.model
        written = f'"odoo.{_kind(ctx.type)}.{alias}"'
        if resolution.candidates:
            names = ", ".join(f'"{name}"' for name in resolution.candidates)
            message = f"Odoo model alias {written} is ambiguous: {names}"
            ctx.api.fail(message, ctx.context, code=ODOO_MODEL)
        elif resolution.outside_model is not None:
            where = not_in_dependencies(resolution.outside, addon_name)
            message = f'Odoo model "{resolution.outside_model}" ({written}) is defined in {where}'
            ctx.api.fail(message, ctx.context, code=ODOO_MODEL_DEPENDS)
        else:
            message = f"Unknown Odoo model in {written}"
            known = [normalized_alias(name) for name in self.registry.models(addon_name)]
            matches = get_close_matches(normalized_alias(alias), known, n=1, cutoff=0.8)
            if matches:
                message += f'; did you mean "{matches[0]}"?'
            ctx.api.fail(message, ctx.context, code=ODOO_MODEL)
        return None

    def _place(self, ctx: AnalyzeTypeContext) -> tuple[SemanticAnalyzer, MypyFile, str] | None:
        """Mypy's analyzer, the module being analyzed and its addon (``None`` outside addons)."""
        analyzer = _semantic_analyzer(ctx)
        if analyzer is None:
            return None
        tree = analyzer.modules.get(analyzer.cur_mod_id)
        if tree is None:
            return None
        addon_name = self.registry.addon_name_for_path(tree.path)
        return (analyzer, tree, addon_name) if addon_name is not None else None

    def _depend_on_view(
        self, analyzer: SemanticAnalyzer, model: str, addon_name: str, keys: bool = False
    ) -> None:
        """Make the daemon analyze the annotation again when the model's classes change.

        The type is made during semantic analysis, which the daemon repeats only for
        targets whose dependencies changed: those are the classes of the model in the
        view, and the namespace of every module of their addons (where a field or a
        new class of the model shows). With ``keys`` (``odoo.values.*``), also the
        classes the keys come from and their modules, in any addon. A module mypy does
        not check (an addon outside the daemon's files) triggers nothing: what it
        changes shows after ``dmypy restart`` (docs/how-it-works.md, "Annotations").
        """
        view = self.registry.view_class(model, addon_name)
        chain = view.chain if view is not None else ()
        classes = {item.fullname for item in chain}
        addons = {item.addon for item in chain}
        modules = {module for name in addons for module in self.registry.addon_modules(name)}
        if keys:
            key_classes, key_modules = self.registry.values_sources(model)
            classes |= key_classes
            modules |= key_modules
        for name in sorted(classes):
            analyzer.add_plugin_dependency(make_trigger(name))
            analyzer.add_plugin_dependency(make_wildcard_trigger(name))  # a member changed
        for module in sorted(modules):
            analyzer.add_plugin_dependency(make_wildcard_trigger(module))


def _values_alias(typ: UnboundType) -> str | None:
    """The alias of the first ``odoo.values.*`` string in ``Annotated[...]`` metadata.

    The metadata of an annotation that the plugin rewrote is rewritten too (it is
    parsed as a type): ``__build_class__["values", "x"]``.
    """
    for meta in typ.args[1:]:
        if isinstance(meta, UnboundType):
            rewritten = meta.name == TRIGGER
            found = trigger_alias(meta) if rewritten else annotation_alias(meta.name)
            if found is not None and found[0] == VALUES:
                return found[1]
    return None


def _kind(typ: UnboundType) -> str:
    """``values`` or ``model``: what an annotation being analyzed stands for."""
    found = trigger_alias(typ)
    return found[0] if found is not None else VALUES


def _is_dict(typ: Type) -> bool:
    """Whether an analyzed type is a ``dict`` or ``Mapping`` (or ``Any``)."""
    proper = get_proper_type(typ)
    if isinstance(proper, AnyType):
        return True
    return isinstance(proper, Instance) and any(
        base.fullname in DICT_CLASSES for base in proper.type.mro
    )


def _type_analyzer(ctx: AnalyzeTypeContext) -> TypeAnalyser | None:
    return ctx.api if isinstance(ctx.api, TypeAnalyser) else None


def _semantic_analyzer(ctx: AnalyzeTypeContext) -> SemanticAnalyzer | None:
    """Mypy's semantic analyzer behind the type analyzer (the plugin interface hides it)."""
    analyzer = _type_analyzer(ctx)
    found = getattr(analyzer, "api", None)
    return found if isinstance(found, SemanticAnalyzer) else None


def _named_finder(analyzer: SemanticAnalyzer) -> Callable[[str], Instance | None]:
    """Find a class by full name, as an instance with ``Any`` for its type arguments."""

    def named(fullname: str) -> Instance | None:
        node = analyzer.lookup_fully_qualified_or_none(fullname)
        info = node.node if node is not None else None
        if not isinstance(info, TypeInfo):
            return None
        args: list[Type] = [AnyType(TypeOfAny.special_form)] * len(info.defn.type_vars)
        return Instance(info, args)

    return named


def values_get_signature(ctx: MethodSigContext) -> FunctionLike:
    """``get`` of ``odoo.values.*``: the signature mypy gives ``get`` of a ``TypedDict``."""
    if _is_values(ctx.type) and isinstance(ctx.default_signature, CallableType):
        return typed_dict_get_signature_callback(ctx)
    return ctx.default_signature


def values_get(ctx: MethodContext) -> Type:
    """``get`` of ``odoo.values.*``: the value type of the key, as for any ``TypedDict``."""
    return typed_dict_get_callback(ctx) if _is_values(ctx.type) else ctx.default_return_type


def _is_values(typ: Type) -> bool:
    proper = get_proper_type(typ)
    if not isinstance(proper, TypedDictType):
        return False
    return values_class_model(proper.fallback.type) is not None
