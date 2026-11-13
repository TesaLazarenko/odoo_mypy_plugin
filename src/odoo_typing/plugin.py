"""mypy plugin entry point.

Enable it in ``pyproject.toml``::

    [tool.mypy]
    plugins = ["odoo_typing.plugin"]

What it does:

* ``_inherit``: every model class gets the MRO of Odoo's registry class for its model
  (its model's earlier classes, then every class of its ``_inherit`` mixins and of
  ``base`` that its addon sees), so fields and methods defined elsewhere are visible
  on ``self`` and ``super()`` works. An ``_inherit`` that Odoo would reject at load
  time (unknown model, model outside the ``depends``, defined later, cycle) is an
  ``odoo-model`` error.
* ``env["model.name"]`` evaluates to that model's class as the addon sees it: its last
  class, or a merged class when unrelated addons extend it (or its mixins); unknown
  names are errors.
* ``fields.Many2one("x")`` / ``One2many`` / ``Many2many`` are declared with the class
  that defines the comodel; reading the field gives the comodel as the *reading* addon
  sees it, like Odoo returns the final registry class.
* A model name that only addons outside the ``depends`` closure define is an
  ``odoo-model-depends`` error (a sub-code of ``odoo-model``, so it can be disabled on
  its own); ``env[...]`` is then ``Any``. Lines that run only when the model is
  installed (``if "x" in self.env:``, :mod:`odoo_typing.guards`) report neither code.
* ``env.user`` / ``env.company`` / ``env.companies`` are ``res.users`` / ``res.company``
  records; ``env[NAME]`` accepts ``Final`` string constants.
* Methods keep their signatures through ``@api.private`` / ``@api.returns(...)``, and
  unannotated overrides of ``-> Self`` methods (``create``, ``search``, ``sudo``...)
  return the receiver (:mod:`odoo_typing.methods`).
* ``_inherits`` (and ``Many2one(delegate=True)``): the delegated models' fields, not
  their methods, are part of the child model's classes.
* ``fields.X(related="a.b", compute="_m", ...)``: each field of the path and each
  method name must exist in the declaring addon's view (``odoo-field-ref``), see
  :mod:`odoo_typing.references`; a relational field declared only with ``related=``
  gets the comodel of the path's last field. The same goes for the paths of
  ``@api.depends(...)`` and the field names of ``@api.constrains`` / ``onchange``.
* ``create({...})`` / ``write({...})``: the keys of a literal dict must be fields of the
  model (``odoo-vals-key``) and the values must fit them (``odoo-vals-type``), see
  :mod:`odoo_typing.vals`.
* ``search([...])`` and the other domain methods, ``domain=`` of relational fields: the
  paths, operators and selection values of literal terms (``odoo-domain``), see
  :mod:`odoo_typing.domains`.

* Incremental mode: each module's cache entry carries a digest of its addon closure
  (``report_config_data``, see :mod:`odoo_typing.fingerprint`); in the daemon, the
  registry starts over when a file it read changed.

Model names are resolved from the point of view of the addon that contains the
file. Files outside any addon (scripts, and Odoo's own ``odoo/*.py`` modules,
which mypy always analyzes) have no such view: the plugin leaves them alone, so
``env[...]`` keeps the declared ``BaseModel`` type and names are not checked
there. Resolving them against every installed addon would scan the whole
registry on each cold run, and could still pick an arbitrary extension class.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import partial

from mypy.nodes import MypyFile
from mypy.options import Options
from mypy.plugin import (
    AttributeContext,
    ClassDefContext,
    FunctionContext,
    MethodContext,
    MethodSigContext,
    Plugin,
    ReportConfigContext,
)
from mypy.types import FunctionLike, Type

from odoo_typing.compat import profile_for
from odoo_typing.config import ConfigError, Settings, load_settings
from odoo_typing.domains import DOMAIN_METHODS
from odoo_typing.fingerprint import Fingerprints
from odoo_typing.hooks.domains import DomainHooks
from odoo_typing.hooks.env import EnvHooks, request_env
from odoo_typing.hooks.field_definitions import FieldDefinitionHooks
from odoo_typing.hooks.field_values import FieldValueHooks
from odoo_typing.hooks.model_classes import ModelClassHooks
from odoo_typing.hooks.models import ODOO_MODEL, ODOO_MODEL_DEPENDS, Models
from odoo_typing.hooks.recordsets import RecordsetHooks
from odoo_typing.hooks.vals import ValsHooks
from odoo_typing.methods import (
    create_signature,
    identity_decorator,
    identity_factory,
    is_create,
    is_self_method,
    keep_receiver,
)
from odoo_typing.odoo_version import version_mismatch_warning
from odoo_typing.records import RecordStore, load_index
from odoo_typing.references import RECORD_PATH_METHODS
from odoo_typing.registry import Registry
from odoo_typing.scan_store import ScanStore
from odoo_typing.scanner import LOG_ACCESS_USERS
from odoo_typing.stamps import stamp
from odoo_typing.startup import (
    package_bases_warning,
    profile_or_exit,
    python_version_warning_or_exit,
    settings_or_exit,
    stale_overlay_warning,
)

__all__ = ["ODOO_MODEL", "ODOO_MODEL_DEPENDS", "OdooPlugin", "plugin"]

DEPENDENCY_PRIORITY = 10  # mypy.build.PRI_MED
# Methods whose first argument is ``vals`` (``create``: also a list of them;
# ``copy``: its ``default``).
VALS_METHODS = frozenset({"create", "write", "copy"})


class OdooPlugin(Plugin):
    """Teach mypy how the Odoo registry assembles models.

    The plugin keeps the state of a run and chooses the hook of each name; the hooks
    themselves are the groups of :mod:`odoo_typing.hooks`.
    """

    def __init__(self, options: Options) -> None:
        """Load settings and prepare the (lazy) model registry.

        A configuration problem ends mypy with the message and exit status 2, like
        mypy's own configuration errors (not as a crash of the plugin).
        """
        super().__init__(options)
        settings = settings_or_exit(options)
        profile = profile_or_exit(settings)
        interpreter = (sys.version_info[0], sys.version_info[1])
        if warning := python_version_warning_or_exit(profile, options, interpreter):
            print(warning, file=sys.stderr)
        if warning := version_mismatch_warning(profile.version, settings.odoo_path):
            print(warning, file=sys.stderr)
        if warning := package_bases_warning(profile, settings, options):
            print(warning, file=sys.stderr)
        self._store = RecordStore.for_cache_dir(options.cache_dir)
        self._hooks = _HookCache()
        self._config_stamp = stamp(options.config_file) if options.config_file else None
        self._inputs_checked = False
        self._overlay_checked = False
        models = Models(settings, profile, self._new_registry(settings))
        self._models = models
        self._classes = ModelClassHooks(models)
        self._env = EnvHooks(models)
        self._vals = ValsHooks(models)
        self._recordsets = RecordsetHooks(models)
        self._domains = DomainHooks(models, self._recordsets)
        self._fields = FieldDefinitionHooks(models, self._domains)
        self._values = FieldValueHooks(models)

    def _new_registry(self, settings: Settings) -> Registry:
        """A registry for the settings, and the fingerprints that follow it."""
        index = partial(load_index, settings, self._store)
        shared = RecordStore(settings.scan_cache_dir) if settings.scan_cache_dir else None
        scans = ScanStore(self._store, shared)
        registry = Registry(settings, index, scans)
        self._fingerprints = Fingerprints(registry, self._store, settings)
        return registry

    # -- incremental mode and the daemon ---------------------------------------------------

    def report_config_data(self, ctx: ReportConfigContext) -> str | None:
        """Make mypy's cache entry of a module depend on the registry facts it used.

        See :mod:`odoo_typing.fingerprint`: a digest of the module's addon closure
        (manifests and scanned classes), so an edit in another addon, a manifest or
        the addons paths makes mypy check the module again.
        """
        return self._fingerprints.module_data(ctx.id, ctx.path)

    def set_modules(self, modules: dict[str, MypyFile]) -> None:
        """Called by mypy when a set of modules is loaded (each daemon update too)."""
        super().set_modules(modules)
        self._inputs_checked = False

    def _refresh_registry(self) -> None:
        """Start over with a new registry if a file it read changed (daemon updates).

        Checked once per batch of modules mypy loads: the first parsed file of a
        daemon update triggers it, a normal run pays it once.
        """
        if self._inputs_checked:
            return
        self._inputs_checked = True
        models = self._models
        if self._config_changed() or models.registry.inputs_changed():
            models.registry = self._new_registry(models.settings)
            self._hooks = _HookCache()  # hooks found with the old registry or settings
        if not self._overlay_checked:
            self._overlay_checked = True
            if warning := stale_overlay_warning(models.settings, models.registry):
                print(warning, file=sys.stderr)

    def _config_changed(self) -> bool:
        """Reload the settings if mypy's config file changed (``dmypy`` keeps the plugin).

        dmypy restarts itself when mypy's own options change, not ``[tool.odoo_typing]``.
        A config that is now invalid is reported and the previous settings are kept.
        """
        config_file = self.options.config_file
        current = stamp(config_file) if config_file else None
        if current == self._config_stamp:
            return False
        self._config_stamp = current
        try:
            settings = load_settings(config_file, self.options.mypy_path)
        except ConfigError as exc:
            print(f"odoo-typing: error: {exc} (keeping the previous settings)", file=sys.stderr)
            return False
        profile = profile_for(settings.odoo_version) if settings.odoo_version else None
        if profile is None:
            print(
                "odoo-typing: error: unsupported odoo_version (keeping the previous settings)",
                file=sys.stderr,
            )
            return False
        self._models.settings, self._models.profile = settings, profile
        self._classes.request_typed = False
        return True

    # -- dependencies ----------------------------------------------------------------------

    def get_additional_deps(self, file: MypyFile) -> list[tuple[int, str, int]]:
        """Make mypy load the modules that define the models a file relies on.

        Without these edges mypy may type-check a file before the model classes it
        refers to (``_inherit``, ``env[...]``, comodels) are analyzed, or not load them
        at all. Files outside any addon get none, since nothing is resolved in them,
        nor do addon files that mypy names in the other spelling (``my_addon.x``
        while the overlay root makes it ``odoo.addons.my_addon.x``): their edges would
        make mypy analyze the addon a second time (:meth:`Registry.misnamed` warns).
        """
        self._refresh_registry()
        registry = self._models.registry
        if registry.misnamed(file.fullname):
            return []  # never make mypy load the addon again under its configured name
        modules = set(registry.dependency_modules(file.fullname, file.path))
        modules.discard(file.fullname)
        return [(DEPENDENCY_PRIORITY, module, -1) for module in sorted(modules)]

    # -- hook lookup -----------------------------------------------------------------------
    # mypy asks for a hook at every call, attribute and class (about 200,000 times on the
    # account addon); the answer depends only on the name, the profile and the registry,
    # so it is kept per name until either changes (:class:`_HookCache`).

    def get_customize_class_mro_hook(
        self, fullname: str
    ) -> Callable[[ClassDefContext], None] | None:
        """Give Odoo model classes the MRO of their registry class."""
        found = self._hooks.mro
        if fullname not in found:
            found[fullname] = self._find_customize_class_mro_hook(fullname)
        return found[fullname]

    def get_method_hook(self, fullname: str) -> Callable[[MethodContext], Type] | None:
        """Type ``env[...]`` and recordset methods; check vals, domains and field paths."""
        found = self._hooks.method
        if fullname not in found:
            found[fullname] = self._find_method_hook(fullname)
        return found[fullname]

    def get_function_hook(self, fullname: str) -> Callable[[FunctionContext], Type] | None:
        """Bind relational field constructors to their comodel; see through decorators."""
        found = self._hooks.function
        if fullname not in found:
            found[fullname] = self._find_function_hook(fullname)
        return found[fullname]

    def get_method_signature_hook(
        self, fullname: str
    ) -> Callable[[MethodSigContext], FunctionLike] | None:
        """Type reading a relational field; check assigned values; let ``create`` take a dict."""
        found = self._hooks.signature
        if fullname not in found:
            found[fullname] = self._find_method_signature_hook(fullname)
        return found[fullname]

    def get_attribute_hook(self, fullname: str) -> Callable[[AttributeContext], Type] | None:
        """Resolve record-typed attributes of model classes from the reading addon."""
        found = self._hooks.attribute
        if fullname not in found:
            found[fullname] = self._find_attribute_hook(fullname)
        return found[fullname]

    # -- semantic analysis: _inherit -------------------------------------------------------

    def _find_customize_class_mro_hook(
        self, fullname: str
    ) -> Callable[[ClassDefContext], None] | None:
        """Give Odoo model classes the MRO of their registry class."""
        if fullname in self._models.profile.comodel_generic:
            return self._classes.comodel_generic
        contribution = self._models.registry.contribution(fullname)
        if contribution is None:
            return None
        return partial(self._classes.merge_bases, contribution)

    def get_base_class_hook(self, fullname: str) -> Callable[[ClassDefContext], None] | None:
        """Relax model meta attributes such as ``_inherit``; add delegated fields."""
        return self._classes.after_class_body

    # -- type checking: env[...] and relational fields -------------------------------------

    def _find_method_hook(self, fullname: str) -> Callable[[MethodContext], Type] | None:
        """Type ``env["model.name"]`` and unannotated overrides of ``-> Self`` methods.

        ``create``, ``write`` and ``copy`` also get their ``vals`` checked (one hook per name, so
        the checks run in the same function).
        """
        profile = self._models.profile
        if fullname == profile.env_getitem:
            return self._env.getitem
        if fullname == f"{profile.environment}.ref":
            return self._env.ref
        name = fullname.rpartition(".")[2]
        if name in VALS_METHODS:
            return partial(self._vals.check, name)
        if name in DOMAIN_METHODS:
            return partial(self._domains.check, name)
        if name == "read":
            return partial(self._recordsets.read_result, name)
        if name in RECORD_PATH_METHODS:
            return partial(self._recordsets.record_path, name)
        if profile.untyped_fields and name == "__iter__":
            return self._recordsets.iterate  # Odoo 17: unannotated
        if is_self_method(fullname):
            return partial(keep_receiver, profile.base_model)
        return None

    def _find_function_hook(self, fullname: str) -> Callable[[FunctionContext], Type] | None:
        """Bind relational field constructors to their comodel; see through decorators."""
        profile = self._models.profile
        fields = self._fields
        if fullname in profile.relational_classes:
            return partial(fields.definition, fields.relational)
        if profile.is_field_class(fullname):
            return partial(fields.definition, None)
        if fullname in profile.identity_decorators:
            return identity_decorator
        factory = (
            partial(identity_factory, profile.identity_template)
            if fullname in profile.identity_factories
            else None
        )
        if fullname in profile.field_decorators:
            return partial(fields.decorator_fields, profile.field_decorators[fullname], factory)
        return factory

    def _find_method_signature_hook(
        self, fullname: str
    ) -> Callable[[MethodSigContext], FunctionLike] | None:
        """Type reading a relational field; check assigned values; let ``create`` take a dict."""
        # mypy names the hook after the descriptor's own class (``odoo.fields.Many2one``,
        # a custom subclass...), so match every ``__get__`` and check the MRO in the hook.
        if fullname.endswith(".__get__"):
            return self._values.get
        if fullname.endswith(".__set__"):
            return self._vals.field_set
        if not is_create(fullname):
            return None
        profile = self._models.profile
        return partial(create_signature, profile.base_model, untyped=profile.untyped_fields)

    def _find_attribute_hook(self, fullname: str) -> Callable[[AttributeContext], Type] | None:
        """Resolve record-typed attributes of model classes from the reading addon."""
        profile = self._models.profile
        registry = self._models.registry
        if fullname in profile.env_records:
            return partial(self._env.record, profile.env_records[fullname])
        if fullname == f"{profile.request_class}.env":
            return partial(request_env, profile.environment)
        if profile.untyped_fields and fullname == f"{profile.base_model}.env":
            return partial(request_env, profile.environment)  # Odoo 17: a bare property
        owner, _, name = fullname.rpartition(".")
        if name in LOG_ACCESS_USERS and registry.contribution(owner):
            # Declared as ``Many2one[Any]``: the res.users records the reading addon sees.
            return partial(self._env.record, "res.users")
        return self._values.record_attribute if registry.contribution(owner) else None


@dataclass(slots=True)
class _HookCache:
    """The hooks found per full name, for one registry and one set of settings."""

    mro: dict[str, Callable[[ClassDefContext], None] | None] = field(default_factory=dict)
    method: dict[str, Callable[[MethodContext], Type] | None] = field(default_factory=dict)
    function: dict[str, Callable[[FunctionContext], Type] | None] = field(default_factory=dict)
    signature: dict[str, Callable[[MethodSigContext], FunctionLike] | None] = field(
        default_factory=dict
    )
    attribute: dict[str, Callable[[AttributeContext], Type] | None] = field(default_factory=dict)


def plugin(version: str) -> type[OdooPlugin]:
    """Return the plugin class; called by mypy.

    Args:
        version: mypy version string.

    Returns:
        The plugin class.
    """
    return OdooPlugin
