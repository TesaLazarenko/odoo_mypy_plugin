"""The state the hook groups share, and the model classes as addons see them.

:class:`Models` holds the settings, the profile of the Odoo version and the registry
(the plugin replaces them in place when the daemon sees a changed configuration or
input file) and turns the registry's view of a model into a mypy type. Each group of
hooks is a :class:`Hooks` around it.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from fnmatch import fnmatchcase
from functools import partial
from typing import Final

from mypy.errorcodes import ErrorCode
from mypy.nodes import Context, MypyFile, TypeInfo
from mypy.options import Options
from mypy.plugin import CheckerPluginInterface, SemanticAnalyzerPluginInterface
from mypy.server.trigger import make_trigger, make_wildcard_trigger
from mypy.types import AnyType, Instance, Type, TypeOfAny

# isort: split
# After mypy.types: these modules import each other at import time.
from mypy.checker import TypeChecker
from mypy.semanal import SemanticAnalyzer

from odoo_typing.classes import (
    add_delegated_fields,
    create_view_class,
    instance_of,
    view_class_name,
    view_class_origin,
)
from odoo_typing.compat import OdooProfile
from odoo_typing.config import Settings
from odoo_typing.guards import field_guard
from odoo_typing.hooks.nodes import (
    Incomplete,
    class_infos,
    field_sources,
    imported_view_class,
    lookup_class,
    type_checker,
)
from odoo_typing.local_values import LocalValues
from odoo_typing.references import near_miss
from odoo_typing.registry import Registry
from odoo_typing.scanner import FieldInfo

__all__ = [
    "FIELDS_DIGEST",
    "ODOO_MODEL",
    "ODOO_MODEL_DEPENDS",
    "PENDING",
    "Hooks",
    "Models",
    "Pending",
    "ViewType",
    "errors_hidden",
    "fine_grained",
    "not_in_dependencies",
]

ODOO_MODEL = ErrorCode("odoo-model", "Check that Odoo model names exist", "Odoo")
ODOO_MODEL_DEPENDS = ErrorCode(
    "odoo-model-depends",
    "Check that Odoo models are defined in the addon's dependencies",
    "Odoo",
    sub_code_of=ODOO_MODEL,
)

ViewType = Instance | AnyType | None

# The hidden module attribute with a digest of the field definitions of the module's
# model classes (odoo_typing.hooks.model_classes), and the prefix of the trigger of a model
# view that a checked module's targets depend on (Hooks._depend_on).
FIELDS_DIGEST: Final = "__odoo_fields"
VIEW_TRIGGER: Final = "__odoo_view "


class Pending:
    """A view class whose classes semantic analysis has not finished (see view_type_in)."""


PENDING: Final = Pending()


class Models:
    """The settings, profile and registry of a run; a model's class as an addon sees it.

    Attributes:
        settings: The plugin settings.
        profile: The profile of the configured Odoo version.
        registry: The model registry (a new one when a file it read changed).
    """

    def __init__(self, settings: Settings, profile: OdooProfile, registry: Registry) -> None:
        self.settings = settings
        self.profile = profile
        self.registry = registry

    def view_type(self, model: str, addon_name: str, api: CheckerPluginInterface) -> ViewType:
        """The type of ``env[model]`` as seen from an addon.

        Returns:
            The model's last class if its MRO is the whole registry class; otherwise a
            merged class (created in the module being checked); ``Any`` if none of its
            classes is loaded in this mypy run; ``None`` if no visible addon defines it.
        """
        checker = type_checker(api)
        found = self.view_type_in(model, addon_name, checker.modules, checker.tree)
        return None if isinstance(found, Pending) else found

    def view_type_in(
        self,
        model: str,
        addon_name: str,
        modules: dict[str, MypyFile],
        tree: MypyFile,
        incomplete: Incomplete = lambda module: False,
    ) -> ViewType | Pending:
        """:meth:`view_type` for a module (``tree``), also during semantic analysis.

        Args:
            model: Odoo model name.
            addon_name: The view.
            modules: mypy's modules.
            tree: The module a merged class goes into.
            incomplete: Which modules semantic analysis has not finished yet (none, once
                type checking runs).

        Returns:
            As :meth:`view_type`, or :data:`PENDING` if a class it needs is not analyzed
            yet (only during semantic analysis: ask again on mypy's next pass).
        """
        view = self.registry.view_class(model, addon_name)
        if view is None:
            return None
        if view.tip is not None:
            tip = class_infos([view.tip], modules, incomplete)
            if tip.infos:
                return instance_of(tip.infos[0])
            if tip.pending:
                return PENDING
        chain = class_infos(view.chain, modules, incomplete)
        if chain.pending:
            return PENDING
        own = [item for item in view.chain if item.model == model]
        if not class_infos(own, modules).infos:
            return AnyType(TypeOfAny.special_form)  # e.g. --follow-imports=skip
        imported = imported_view_class(
            view_class_name(model, addon_name), tree, modules, self.registry.addons.addon_for_module
        )
        if imported is not None:
            return instance_of(imported)
        bases = class_infos(view.bases, modules, incomplete)
        delegated: list[list[tuple[TypeInfo, tuple[str, ...]]] | None] = [
            field_sources(self.registry.delegate_chain(delegate, addon_name), modules, incomplete)
            for delegate in self.registry.delegate_closure(view.chain, model, addon_name)
        ]
        if bases.pending or None in delegated:
            return PENDING
        lookup = partial(lookup_class, modules)
        merged = create_view_class(
            tree, model, addon_name, chain.infos, bases.infos, lookup, chain.missing
        )
        for sources in delegated:
            add_delegated_fields(merged, sources or [])
        return instance_of(merged)

    def env_model(self, model: str, addon_name: str, api: CheckerPluginInterface) -> ViewType:
        """A model's records as an addon sees them; ``Any`` if not loaded with the module."""
        module = type_checker(api).tree.fullname
        if not self.registry.loaded_with(model, addon_name, module, api.path):
            # ``env2 = self.env; env2["x"]``: the scan saw no model name, so no edge
            # makes mypy load the model's classes with this module.
            return AnyType(TypeOfAny.special_form)
        return self.view_type(model, addon_name, api)

    def unknown_model(
        self, model: str, addon_name: str, context: Context, api: CheckerPluginInterface
    ) -> AnyType | None:
        """Report a model name that no addon in the view defines.

        Not on a line that runs only when the model is installed (``if "m" in
        self.env:``, :mod:`odoo_typing.guards`), or only with an addon outside the
        dependencies (:meth:`_optional_code`): the records are ``Any`` there.

        Returns:
            ``Any`` if an addon outside the dependencies defines it (reported with
            :data:`ODOO_MODEL_DEPENDS`) or the line is guarded; ``None`` for a name
            nothing defines.
        """
        if self.registry.guarded(api.path, model, context.line):
            return AnyType(TypeOfAny.special_form)  # ``if "m" in self.env:``
        outside = self.registry.addons_outside_defining(model, addon_name)
        if self._optional_code(outside, addon_name, context.line, api.path):
            return AnyType(TypeOfAny.special_form)
        if outside:
            message = (
                f'Odoo model "{model}" is defined in {not_in_dependencies(outside, addon_name)}'
            )
            api.fail(message, context, code=ODOO_MODEL_DEPENDS)
            return AnyType(TypeOfAny.special_form)
        api.fail(f'Unknown Odoo model "{model}"', context, code=ODOO_MODEL)
        return None

    def _optional_code(
        self, outside: tuple[str, ...], addon_name: str, line: int, path: str
    ) -> bool:
        """Whether a line runs only with an addon outside the dependencies installed.

        Two signs (:mod:`odoo_typing.guards`): the line is under the guard of a field
        that no model of the dependencies has (``if "payment_provider_id" in
        lines._fields:``), or its function passes the name of an addon that defines the
        model to a call (``cls.ensure_installed("sale")``).
        """
        registry = self.registry
        guarded = registry.guarded_fields(path, line)
        if any(not registry.has_field_named(name, addon_name) for name in guarded):
            return True
        return bool(outside) and not registry.call_strings(path, line).isdisjoint(outside)

    def comodel_at_use_site(
        self, comodel: Instance, owner: TypeInfo | None, api: CheckerPluginInterface
    ) -> Type | None:
        """Resolve a field's comodel from the addon that reads the field.

        The reading addon's view is used if mypy loads all the comodel's classes in it
        with the reading module (:meth:`Registry.loaded_with`); otherwise the view of
        the record's own class, if they are loaded with that class's module.

        A view that is not the reader's has fewer classes when an addon the reader
        sees extends the model: ``partner.user_ids.notify_info()`` in an addon that
        depends on ``web_notify`` and never names ``res.users`` read the users as
        ``mail`` sees them, and the method was an unknown attribute. Such records are
        ``Any``: their class as the reader sees it is not there to be checked.

        Returns:
            The records' type; ``None`` to keep the declared comodel class.
        """
        model = self.model_of(comodel.type)
        if model is None:
            return None
        module = type_checker(api).tree.fullname
        reader = self.registry.addon_name_for_path(api.path)
        views: list[tuple[str | None, str, str | None]] = [(reader, module, api.path)]
        if owner is not None:
            views.append((self.addon_of_class(owner), owner.module_name, None))
        for addon_name, host, path in views:
            if addon_name is not None and self.registry.loaded_with(model, addon_name, host, path):
                resolved = self.view_type(model, addon_name, api)
                if isinstance(resolved, Instance):
                    return resolved if self._sees_as(model, addon_name, reader) else _unchecked()
                # Its classes are loaded but not all analyzed yet (mypy checks a file
                # before a module of its own addon that it never names): the next view.
        declared = self.addon_of_class(comodel.type)
        return None if self._sees_as(model, declared, reader) else _unchecked()

    def _sees_as(self, model: str, addon_name: str | None, reader: str | None) -> bool:
        """Whether an addon sees the same classes of a model as the reading addon does."""
        if reader is None or addon_name is None or addon_name == reader:
            return True
        mine = self.registry.view_class(model, reader)
        other = self.registry.view_class(model, addon_name)
        return mine is None or other is None or mine.chain == other.chain

    def addon_of_class(self, info: TypeInfo) -> str | None:
        """The addon whose view a model class has (its own, or a merged class's)."""
        origin = view_class_origin(info)
        if origin is not None:
            return origin[1]
        contribution = self.registry.contribution(info.fullname)
        return contribution.addon if contribution else None

    def model_of(self, info: TypeInfo) -> str | None:
        """The model a class belongs to (its own, or a merged class's)."""
        origin = view_class_origin(info)
        if origin is not None:
            return origin[0]
        contribution = self.registry.contribution(info.fullname)
        return contribution.model if contribution else None


def _unchecked() -> AnyType:
    """Records whose class, as the reading addon sees it, mypy has not loaded."""
    return AnyType(TypeOfAny.special_form)


def fine_grained(options: Options) -> bool:
    """Whether mypy keeps fine-grained dependencies (the daemon, ``--cache-fine-grained``).

    Only then do the daemon's dependencies of the checks matter: a full run checks every
    module whose addon fingerprint changed (``report_config_data``).
    """
    return bool(options.fine_grained_incremental or options.cache_fine_grained)


def errors_hidden(api: CheckerPluginInterface | SemanticAnalyzerPluginInterface) -> bool:
    """Whether mypy shows no error of the module being analyzed.

    ``follow_imports = "silent"`` (Odoo's own addons, in the usual configuration) and
    ``ignore_errors``: a check that only reports would do its work for nothing there
    (on sale, 3797 of 4329 field definitions). Work that types something still runs.
    """
    if api.options.ignore_errors:
        return True
    if isinstance(api, TypeChecker):
        return api.path in api.errors.ignored_files
    if isinstance(api, SemanticAnalyzer):
        return api.cur_mod_node.path in api.errors.ignored_files
    return False


def not_in_dependencies(addons: tuple[str, ...], addon: str) -> str:
    """``addon "sale", which is not in the dependencies of "my_addon"``."""
    names = ", ".join(f'"{name}"' for name in addons)
    where = f"addon {names}, which is" if len(addons) == 1 else f"addons {names}, which are"
    return f'{where} not in the dependencies of "{addon}"'


class Hooks:
    """A group of the plugin's hooks; it reads the shared state through these names."""

    def __init__(self, models: Models) -> None:
        self._models = models
        self._local: LocalValues | None = None

    def _fields_of(
        self, addon_name: str, api: CheckerPluginInterface
    ) -> Callable[[str], Mapping[str, FieldInfo]]:
        """The fields of a model as an addon sees it, for a check of the code being checked.

        Each model asked about becomes a dependency of the target being checked in the
        daemon (:meth:`_depend_on`).
        """

        def fields_of(model: str) -> Mapping[str, FieldInfo]:
            self._depend_on(model, addon_name, api)
            return self.registry.fields(model, addon_name)

        return fields_of

    def _depend_on(self, model: str, addon_name: str, api: CheckerPluginInterface) -> None:
        """Make the daemon check the current target again when a model's fields change.

        A check reads the fields of the models of a view, which other modules define: the
        daemon reruns a target only for the dependencies it has. The classes of the view
        of each model whose fields the target read (the model, the models it delegates
        to, the comodels of its ``related=`` fields) fire the change: ``<C>`` when a class
        goes away, ``<C[wildcard]>`` when one of its members changes, and
        ``<module.__odoo_fields>`` (:mod:`odoo_typing.hooks.model_classes`) when an
        argument of a field of the module changes (``store=``). They lead to one trigger
        per checked module and model view, ``<module.__odoo_view ...>``, on which the
        targets depend: mypy follows a trigger to the triggers that depend on it, so
        each target adds one dependency per model, not one per class.

        Only in fine-grained mode (the daemon, ``--cache-fine-grained``), and not in a
        module whose errors mypy does not show (``follow_imports = "silent"``): checking it
        again would show nothing. mypy reads these dependencies when it processes a whole
        module (see docs/how-it-works.md).
        """
        checker = type_checker(api)
        if not fine_grained(checker.options):
            return
        if checker.path in checker.errors.ignored_files:
            return  # follow_imports=silent (Odoo's own addons): its errors are not shown
        tree = checker.tree
        function = checker.scope.top_level_function()
        target = function.fullname if function is not None else tree.fullname
        deps = tree.plugin_deps
        view = make_trigger(f"{tree.fullname}.{VIEW_TRIGGER}{model}@{addon_name}")
        if view not in deps:
            self._link_view(view, model, addon_name, deps)
        deps[view].add(target)

    def _link_view(self, view: str, model: str, addon_name: str, deps: dict[str, set[str]]) -> None:
        """Make the triggers of a model view's classes fire a module's trigger of the view."""
        registry = self.registry
        deps[view] = set()
        for source in registry.field_sources(model, addon_name):
            found = registry.view_class(source, addon_name)
            for item in found.chain if found is not None else ():
                triggers = (
                    make_trigger(item.fullname),
                    make_wildcard_trigger(item.fullname),
                    make_trigger(f"{item.module}.{FIELDS_DIGEST}"),
                )
                for trigger in triggers:
                    deps.setdefault(trigger, set()).add(view)

    def _local_values(self, api: CheckerPluginInterface) -> LocalValues | None:
        """The variables of the function being checked (kept for its next calls).

        Kept for the function object and its body: the daemon reuses the object of a
        changed function with the new body (``astmerge``).
        """
        function = type_checker(api).scope.current_function()
        if function is None:
            return None
        local = self._local
        if local is None or local.function is not function or local.body is not function.body:
            self._local = LocalValues(function)
        return self._local

    @property
    def registry(self) -> Registry:
        """The model registry of the run."""
        return self._models.registry

    @property
    def _profile(self) -> OdooProfile:
        return self._models.profile

    @property
    def _settings(self) -> Settings:
        return self._models.settings

    def _field_guarded(self, path: str) -> Callable[[str, int], bool]:
        """Whether a field name is guarded on a line of a file (``"f" in self._fields``)."""
        return lambda name, line: self.registry.guarded(path, field_guard(name), line)

    def _field_ignored(self, name: str, fields: Iterable[str] = ()) -> bool:
        """Whether a name that is no field is not reported: it matches ``vals_ignore_keys``.

        ``x_*`` names fields made in the database (Studio, manual fields), which domains,
        ``mapped("x_studio_y")``, ``read`` and orders take as ``vals`` do; but a near miss
        of a declared field (``x_scroe`` for ``x_score``) is a typo, and is reported.
        """
        return any(
            fnmatchcase(name, pattern) for pattern in self._settings.vals_ignore_keys
        ) and not near_miss(name, fields)

    def _selection_elsewhere(self, addon_name: str) -> Callable[[str, str], frozenset[str]]:
        """The keys a selection field gets in any addon, also outside the closure.

        A key that only an addon outside the ``depends`` closure adds (``selection_add``,
        ``("code", "=", "stripe")`` in ``payment``) is not misspelt: the checks of
        selection keys leave it alone. Asked only for a key that is not in the view.
        """

        def keys(model: str, field: str) -> frozenset[str]:
            sources = self.registry.field_sources(model, addon_name)
            return self.registry.selection_keys_anywhere(sources, field)

        return keys
