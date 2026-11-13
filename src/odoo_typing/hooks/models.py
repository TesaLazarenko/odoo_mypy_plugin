"""The state the hook groups share, and the model classes as addons see them.

:class:`Models` holds the settings, the profile of the Odoo version and the registry
(the plugin replaces them in place when the daemon sees a changed configuration or
input file) and turns the registry's view of a model into a mypy type. Each group of
hooks is a :class:`Hooks` around it.
"""

from __future__ import annotations

from collections.abc import Callable
from functools import partial
from typing import Final

from mypy.errorcodes import ErrorCode
from mypy.nodes import Context, MypyFile, TypeInfo
from mypy.plugin import CheckerPluginInterface
from mypy.types import AnyType, Instance, TypeOfAny

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
from odoo_typing.registry import Registry

__all__ = [
    "ODOO_MODEL",
    "ODOO_MODEL_DEPENDS",
    "PENDING",
    "Hooks",
    "Models",
    "Pending",
    "ViewType",
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
        self.env:``, :mod:`odoo_typing.guards`): the records are ``Any`` there.

        Returns:
            ``Any`` if an addon outside the dependencies defines it (reported with
            :data:`ODOO_MODEL_DEPENDS`) or the line is guarded; ``None`` for a name
            nothing defines.
        """
        if self.registry.guarded(api.path, model, context.line):
            return AnyType(TypeOfAny.special_form)  # ``if "m" in self.env:``
        outside = self.registry.addons_outside_defining(model, addon_name)
        if outside:
            message = (
                f'Odoo model "{model}" is defined in {not_in_dependencies(outside, addon_name)}'
            )
            api.fail(message, context, code=ODOO_MODEL_DEPENDS)
            return AnyType(TypeOfAny.special_form)
        api.fail(f'Unknown Odoo model "{model}"', context, code=ODOO_MODEL)
        return None

    def comodel_at_use_site(
        self, comodel: Instance, owner: TypeInfo | None, api: CheckerPluginInterface
    ) -> Instance | None:
        """Resolve a field's comodel from the addon that reads the field.

        The reading addon's view is used if mypy loads all the comodel's classes in it
        with the reading module (:meth:`Registry.loaded_with`); otherwise the view of
        the record's own class, if they are loaded with that class's module.
        """
        model = self.model_of(comodel.type)
        if model is None:
            return None
        module = type_checker(api).tree.fullname
        views: list[tuple[str | None, str, str | None]] = [
            (self.registry.addon_name_for_path(api.path), module, api.path)
        ]
        if owner is not None:
            views.append((self.addon_of_class(owner), owner.module_name, None))
        for addon_name, host, path in views:
            if addon_name is not None and self.registry.loaded_with(model, addon_name, host, path):
                resolved = self.view_type(model, addon_name, api)
                return resolved if isinstance(resolved, Instance) else None
        return None

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

    def _local_values(self, api: CheckerPluginInterface) -> LocalValues | None:
        """The variables of the function being checked (kept for its next calls)."""
        function = type_checker(api).scope.current_function()
        if function is None:
            return None
        if self._local is None or self._local.function is not function:
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
