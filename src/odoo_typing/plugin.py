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
* ``search([...])`` and the other domain methods, ``domain=`` of relational fields, the
  returns, variables and parameters annotated ``odoo.domain.<model>``: the structure,
  paths, operators and values of literal terms (``odoo-domain``, ``odoo-domain-search``)
  and the model of typed domains (``odoo-domain-model``), see :mod:`odoo_typing.domains`.

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

import json
import os
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from functools import partial
from typing import TypeVar

from mypy.checker import TypeChecker
from mypy.checker_state import checker_state
from mypy.lookup import lookup_fully_qualified
from mypy.nodes import (
    Decorator,
    FuncDef,
    Import,
    ImportAll,
    ImportFrom,
    MypyFile,
    TypeInfo,
    Var,
)
from mypy.options import Options
from mypy.plugin import (
    AnalyzeTypeContext,
    AttributeContext,
    ClassDefContext,
    FunctionContext,
    FunctionSigContext,
    MethodContext,
    MethodSigContext,
    Plugin,
    ReportConfigContext,
)
from mypy.types import (
    AnyType,
    CallableType,
    FunctionLike,
    Instance,
    Type,
    TypeOfAny,
    get_proper_type,
)
from mypy.util import correct_relative_import

from odoo_typing import records
from odoo_typing.addons import Addon
from odoo_typing.annotations import (
    ANNOTATED_NAMES,
    TRIGGER_FULLNAME,
    Wrapping,
    rewrite_annotations,
)
from odoo_typing.compat import profile_for
from odoo_typing.config import ConfigError, Settings, load_settings
from odoo_typing.domains import DOMAIN_METHODS, PARAMETER_DOMAIN_METHODS
from odoo_typing.errors import RAISE_VARIABLE, fail_soft, report_internal_error
from odoo_typing.fingerprint import Fingerprints
from odoo_typing.hooks.annotations import (
    DICT_GET,
    AnnotationHooks,
    values_get,
    values_get_signature,
)
from odoo_typing.hooks.asked_fields import HASATTR, AskedFieldHooks
from odoo_typing.hooks.built_classes import inherit_built_class
from odoo_typing.hooks.class_attributes import define_through_cls, readable_on_class
from odoo_typing.hooks.comparisons import COMPARISON_HOOKS, ComparisonHooks
from odoo_typing.hooks.domains import (
    DomainHooks,
    domain_parameters,
    has_domain_type,
    wrapper_signature,
)
from odoo_typing.hooks.env import (
    XML_ID_METHODS,
    EnvHooks,
    add_request_attributes,
    env_cursor,
    present_env,
    request_env,
    type_request,
)
from odoo_typing.hooks.field_definitions import FieldDefinitionHooks
from odoo_typing.hooks.field_values import FILTER, FieldValueHooks, falsy_filter_signature
from odoo_typing.hooks.mixed_values import (
    MIXED_CALLS,
    MIXED_ITERATIONS,
    MIXED_READS,
    MIXED_WRITES,
    MixedValueHooks,
)
from odoo_typing.hooks.model_classes import ModelClassHooks
from odoo_typing.hooks.models import ODOO_MODEL, ODOO_MODEL_DEPENDS, Models
from odoo_typing.hooks.recordsets import RecordsetHooks
from odoo_typing.hooks.vals import ValsHooks
from odoo_typing.methods import (
    SELF_METHODS,
    browse_signature,
    command_id_signature,
    create_signature,
    identity_decorator,
    identity_factory,
    is_browse,
    is_command_id_method,
    is_create,
    is_self_method,
    keep_receiver,
)
from odoo_typing.odoo_version import profile_mismatch_warning, version_mismatch_warning
from odoo_typing.package_names import Bound, bind_in, bound_on_package, import_into
from odoo_typing.rebound import ReboundVariables
from odoo_typing.records import RecordStore, load_index
from odoo_typing.references import RECORD_PATH_METHODS
from odoo_typing.registry import Registry
from odoo_typing.scan_store import ScanStore
from odoo_typing.scanner import LOG_ACCESS_USERS
from odoo_typing.stamps import Stamps, stamp
from odoo_typing.startup import (
    follow_imports_warning,
    mypy_version_warning,
    package_bases_warning,
    profile_or_exit,
    python_version_warning_or_exit,
    settings_or_exit,
    stale_overlay_warning,
)
from odoo_typing.term_lists import is_term_list_method, term_list_signature

__all__ = ["ODOO_MODEL", "ODOO_MODEL_DEPENDS", "OdooPlugin", "plugin"]

# mypy reuses its cache while the plugins snapshot (this file's hash and this module's
# ``__version__``, mypy.build.take_module_snapshot) is unchanged. The digest of the whole
# package makes a change of any plugin file check everything again, also the modules
# outside addons (Odoo's own), which report no config data.
ContextT = TypeVar("ContextT")
ResultT = TypeVar("ResultT")

__version__ = records.source_digest()

DEPENDENCY_PRIORITY = 10  # mypy.build.PRI_MED
ENV = "env"
GETATTR = "__getattr__"
CONTAINS = "__contains__"
ITERATE = "__iter__"
# ``records._fields[name]``: a dict (Odoo 17-19) or a ``MappingProxyType`` (Odoo 20).
MAPPING_ITEMS = frozenset({"builtins.dict.__getitem__", "types.MappingProxyType.__getitem__"})
# What iterating, indexing and the set operators of records give is the addon's view too:
# ``for line in self``, ``self[0]``, ``self - other``.
OWN_VIEW_DUNDERS = frozenset(
    {ITERATE, "__getitem__", "__or__", "__and__", "__sub__", "__add__", "__reversed__"}
)
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
        if warning := profile_mismatch_warning(profile, settings.odoo_path):
            print(warning, file=sys.stderr)
        if warning := package_bases_warning(profile, settings, options):
            print(warning, file=sys.stderr)
        if warning := follow_imports_warning(options):
            print(warning, file=sys.stderr)
        self._store = RecordStore.for_cache_dir(options.cache_dir)
        self._kept_index: dict[str, tuple[list[Addon], Stamps]] = {}
        self._kept_scans: dict[str, object] = {}
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
        self._comparisons = ComparisonHooks(models)
        self._fields = FieldDefinitionHooks(models, self._domains, self._env)
        self._values = FieldValueHooks(models)
        self._asked = AskedFieldHooks(models)
        self._mixed = MixedValueHooks(models)
        self._rebound = ReboundVariables()
        annotations = AnnotationHooks(models)
        self._type_hooks: dict[str, Callable[[AnalyzeTypeContext], Type]] = {
            name: partial(annotations.annotated, name) for name in ANNOTATED_NAMES
        }
        self._type_hooks[TRIGGER_FULLNAME] = annotations.rewritten
        self._type_hooks = {
            name: fail_soft(hook, _any_type, f"the annotation {name}")
            for name, hook in self._type_hooks.items()
        }
        self._after_class_body = fail_soft(
            self._classes.after_class_body, _no_result, "the class hook of a model"
        )
        self._kept_record = fail_soft(
            self._kept_records, _default_attribute, "the hook of a record kept on a class"
        )
        self._every_class = fail_soft(_every_class, _no_result, "the hook of every class")
        self._rewritten: dict[str, MypyFile] = {}  # module -> the tree rewritten last
        # The ``odoo`` package where it is a namespace (Odoo 19+), and its tree once the
        # names Odoo binds on it are there (see _bind_package_names).
        self._package = settings.odoo_path.name if profile.namespace_odoo else None
        self._package_tree: MypyFile | None = None
        self._package_bound: Bound | None = None

    def _new_registry(self, settings: Settings) -> Registry:
        """A registry for the settings, and the fingerprints that follow it.

        The addon index and the scans this process read are kept across registries
        (``_kept_index``, ``_kept_scans``): a daemon update after an edit of one addon
        file reads and decodes only what changed (each entry checks its stamps).
        """
        index = partial(load_index, settings, self._store, self._kept_index)
        shared = RecordStore.shared(settings.scan_cache_dir)
        scans = ScanStore(self._store, shared, self._kept_scans)
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
        try:
            bound = self._package_names(ctx.id)
            if bound:
                # The names added to the ``odoo`` package (Odoo 19+): its cache entry
                # is of no use to a run that would add other names, or none.
                return json.dumps(sorted(bound.items()))
            return self._fingerprints.module_data(ctx.id, ctx.path)
        except Exception as exc:
            report_internal_error(exc, f"computing the cache data of {ctx.id}")
            raise

    def set_modules(self, modules: dict[str, MypyFile]) -> None:
        """Called by mypy when a set of modules is loaded (each daemon update too).

        The hooks chosen from declared signatures are chosen again: a declaration in a
        file the registry does not read (an addon's ``tests``, a helper package) may
        have changed without starting a new registry.
        """
        super().set_modules(modules)
        self._inputs_checked = False
        self._package_bound = None  # a daemon update: ``odoo/init.py`` may have changed
        self._hooks.forget_declared()

    def _refresh_registry(self) -> None:
        """Start over with a new registry if a file it read changed (daemon updates).

        Checked once per batch of modules mypy loads: the first parsed file of a
        daemon update triggers it, a normal run pays it once.
        """
        if self._inputs_checked:
            return
        self._inputs_checked = True
        models = self._models
        try:
            if self._config_changed() or models.registry.inputs_changed():
                models.registry = self._new_registry(models.settings)
                self._hooks = _HookCache()  # hooks found with the old registry or settings
            if not self._overlay_checked:
                self._overlay_checked = True
                if warning := stale_overlay_warning(models.settings, models.registry):
                    print(warning, file=sys.stderr)
        except Exception as exc:
            report_internal_error(exc, "checking the addons for changes")
            raise

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
        try:
            self._refresh_registry()
            registry = self._models.registry
            if registry.misnamed(file.fullname, file.path):
                return []  # never make mypy load the addon again under its configured name
            modules = set(registry.dependency_modules(file.fullname, file.path))
            modules |= registry.inherited_view_modules(file.path, _imported_modules(file))
        except Exception as exc:
            report_internal_error(exc, f"finding the models {file.fullname} relies on")
            raise
        modules.discard(file.fullname)
        modules.update(self._package_imports(file))
        return [(DEPENDENCY_PRIORITY, module, -1) for module in sorted(modules)]

    def _package_imports(self, file: MypyFile) -> list[str]:
        """Give the ``odoo`` namespace package the names Odoo binds on it (Odoo 19+).

        ``from odoo import Command, _`` names what a module of the package assigns to it
        (:mod:`odoo_typing.package_names`): the same imports are added to the package's
        module, which mypy parses from an empty source. With an ``__init__.py`` (Odoo 17,
        18) the package says what it has itself.

        Returns:
            The modules of the added imports.
        """
        try:
            return import_into(file, self._package_names(file.fullname))
        except Exception as exc:
            report_internal_error(exc, f"reading the names bound on the package {file.fullname}")
            raise

    def _bind_package_names(self) -> None:
        """Bind the names of the ``odoo`` namespace package in its current tree (Odoo 19+).

        mypy parses the package again without :meth:`get_additional_deps` when a module
        of its import cycle is out of date; asked from every hook lookup, so the names
        are there before mypy stores the package or analyzes a module that imports from
        it. One dict lookup once the tree is done.
        """
        modules = self._modules
        package = self._package
        if package is None or not modules:
            return
        tree = modules.get(package)
        if tree is None or self._package_tree is tree:
            return
        try:
            if self._package_bound is None:  # read from disk once, not at every lookup
                self._package_bound = self._package_names(package)
            if bind_in(tree, self._package_bound, modules):
                self._package_tree = tree
        except Exception as exc:  # pylint: disable=broad-exception-caught
            if os.environ.get(RAISE_VARIABLE):
                raise
            report_internal_error(exc, f"binding the names of the package {package}")
            self._package_tree = tree

    def _prepare_rebound(self) -> None:
        """Declare the rebound variables of the addon module being checked (odoo_typing.rebound).

        Asked from every hook lookup of type checking, the first moment the plugin takes
        part in checking a module (for a model, its first field): one dict lookup once
        the tree is done. Modules outside the addons are left as mypy infers them.
        """
        checker = checker_state.type_checker
        if not isinstance(checker, TypeChecker):
            return
        tree = checker.tree
        if self._rebound.prepared(tree):
            return
        try:
            outside = self._models.registry.addons.addon_for_module(tree.fullname) is None
            if outside or not self._models.settings.rebound_variables:
                self._rebound.skip(tree)
            else:
                self._rebound.prepare(tree, checker.named_type("builtins.bool"))
        except Exception as exc:  # pylint: disable=broad-exception-caught
            if os.environ.get(RAISE_VARIABLE):
                raise
            report_internal_error(exc, f"declaring the rebound variables of {tree.fullname}")
            self._rebound.skip(tree)

    def _package_names(self, module: str) -> Bound:
        """The names Odoo binds on a module, if it is the ``odoo`` namespace package."""
        odoo = self._models.settings.odoo_path
        if module != odoo.name or (odoo / "__init__.py").exists():
            return {}
        return bound_on_package(odoo, odoo.name)

    # -- annotations: odoo.values.* / odoo.model.* -----------------------------------------

    def _rewrite_module_of(self, class_fullname: str) -> None:
        """Rewrite the bare ``odoo.<kind>.*`` strings of a class's module, once per tree.

        See :mod:`odoo_typing.annotations`. Called when semantic analysis reaches a
        regular top-level class (:meth:`get_customize_class_mro_hook`), before its body:
        on every path where mypy analyzes a module from source (a parsed file, a cached
        one whose dependency changed, the modules of an import cycle, a daemon update,
        ``--num-workers``), which no hook that sees the parsed file covers. mypy calls
        the hook for no class-syntax ``TypedDict`` / ``NamedTuple`` and no class whose
        bases it rejects; classes defined in functions are skipped here. A module
        without such a class is never rewritten (``Annotated`` works there).

        The same pass wraps the expressions whose domains are checked: those of
        ``odoo.domain.*`` annotations and the returns of fields' search methods.
        """
        if "@" in class_fullname:
            return  # a class defined in a function (``Name@12``): not a top-level class
        module = class_fullname.rpartition(".")[0]
        tree = self._modules.get(module) if self._modules else None
        if tree is None or self._rewritten.get(module) is tree:
            return
        self._rewritten[module] = tree
        registry = self._models.registry
        aliases = registry.annotation_aliases(tree.path)
        search = registry.search_methods(module) if registry.addon_name_for_path(tree.path) else {}
        if not aliases and not search:
            return
        builtins = self._modules.get("builtins") if self._modules else None
        wrapper = builtins.names.get("__build_class__") if builtins is not None else None
        wrapping = None
        if wrapper is not None and wrapper.node is not None:
            wrapping = Wrapping(wrapper.node, registry.model_classes(module), search)
        rewrite_annotations(tree, wrapping)

    def get_type_analyze_hook(self, fullname: str) -> Callable[[AnalyzeTypeContext], Type] | None:
        """Type ``odoo.values.*`` / ``odoo.model.*`` annotations (odoo_typing.annotations)."""
        if self._package is not None:
            self._bind_package_names()
        return self._type_hooks.get(fullname)

    # -- hook lookup -----------------------------------------------------------------------
    # mypy asks for a hook at every call, attribute and class (about 200,000 times on the
    # account addon); the answer depends only on the name, the profile and the registry,
    # so it is kept per name until either changes (:class:`_HookCache`), and on the
    # declared signature for some, kept per batch of loaded modules.

    def get_customize_class_mro_hook(
        self, fullname: str
    ) -> Callable[[ClassDefContext], None] | None:
        """Give Odoo model classes the MRO of their registry class.

        Every class also gets the attributes its class methods define
        (:mod:`odoo_typing.hooks.class_attributes`) and, when it builds another class's
        objects, that class as its base (:mod:`odoo_typing.hooks.built_classes`).

        Also rewrites the annotations of the class's module (:meth:`_rewrite_module_of`): mypy
        analyzes a module's classes before their bodies, and defers what it cannot look
        up yet, so the module's functions see the rewritten names on the next pass.
        """
        if self._package is not None:
            self._bind_package_names()
        self._rewrite_module_of(fullname)
        found = self._hooks.mro
        if fullname not in found:
            model = _soft(
                self._find_customize_class_mro_hook(fullname), _no_result, f"the MRO of {fullname}"
            )
            every = self._every_class
            found[fullname] = every if model is None else partial(_both, every, model)
        return found[fullname]

    def get_class_attribute_hook(self, fullname: str) -> Callable[[AttributeContext], Type] | None:
        """Let a class read the attributes its class methods define (no hook of its own).

        mypy asks for this hook right before it looks the attribute up on the class, which
        is when an attribute defined through ``cls`` stops being instance-only
        (:mod:`odoo_typing.hooks.class_attributes`).
        """
        self._prepare_rebound()
        class_name, _, name = fullname.rpartition(".")
        if not self._modules:
            return None
        try:
            symbol = lookup_fully_qualified(class_name, self._modules, raise_on_missing=False)
            if symbol is None or not isinstance(symbol.node, TypeInfo):
                return None
            readable_on_class(symbol.node, name)
            attribute = symbol.node.get(name)
            variable = attribute.node if attribute is not None else None
            if isinstance(variable, Var) and self._values.keeps_record(variable):
                return self._kept_record
        except Exception as exc:  # pylint: disable=broad-exception-caught
            if os.environ.get(RAISE_VARIABLE):
                raise
            report_internal_error(exc, f"reading the class attribute {fullname}")
        return None

    def get_method_hook(self, fullname: str) -> Callable[[MethodContext], Type] | None:
        """Type ``env[...]`` and recordset methods; check vals, domains and field paths."""
        if self._package is not None:
            self._bind_package_names()
        self._prepare_rebound()
        found = self._hooks.method
        if fullname not in found:
            self._type_request()
            method = self._method_fullname(fullname)
            found[fullname] = _soft(
                self._with_own_view(method, self._find_method_hook(method)),
                _default_return,
                f"the hook of {fullname}()",
            )
        return found[fullname]

    def _with_own_view(
        self, fullname: str, hook: Callable[[MethodContext], Type] | None
    ) -> Callable[[MethodContext], Type] | None:
        """A ``-> Self`` method or iteration of a model class: records as the addon sees them.

        See :meth:`RecordsetHooks.own_view`; the class of the receiver is in the hook's
        name, so other classes keep their hook.
        """
        owner, _, name = fullname.rpartition(".")
        if name not in SELF_METHODS and name not in OWN_VIEW_DUNDERS:
            return hook
        if not owner or self._models.registry.contribution(owner) is None:
            return hook
        return partial(self._recordsets.own_view, hook)

    def get_function_hook(self, fullname: str) -> Callable[[FunctionContext], Type] | None:
        """Bind relational field constructors to their comodel; see through decorators."""
        if self._package is not None:
            self._bind_package_names()
        self._prepare_rebound()
        found = self._hooks.function
        if fullname not in found:
            self._type_request()
            found[fullname] = _soft(
                self._find_function_hook(fullname), _default_return, f"the hook of {fullname}()"
            )
        return found[fullname]

    def get_function_signature_hook(
        self, fullname: str
    ) -> Callable[[FunctionSigContext], FunctionLike] | None:
        """Make the wrapper of checked domain expressions an identity (odoo_typing.annotations).

        ``filter(None, ...)`` takes values that may be ``False``.
        """
        self._prepare_rebound()
        if fullname == FILTER:
            return falsy_filter_signature
        return wrapper_signature if fullname == TRIGGER_FULLNAME else None

    def get_method_signature_hook(
        self, fullname: str
    ) -> Callable[[MethodSigContext], FunctionLike] | None:
        """Type reading a relational field; check assigned values; let ``create`` take a dict."""
        self._prepare_rebound()
        found = self._hooks.signature
        if fullname not in found:
            self._type_request()
            found[fullname] = _soft(
                self._find_method_signature_hook(self._method_fullname(fullname)),
                _default_signature,
                f"the signature hook of {fullname}()",
            )
        return found[fullname]

    def _method_fullname(self, fullname: str) -> str:
        """A method hook's name as ``<receiver class>.<name>``, also for a ``Self`` receiver.

        mypy names a method's hooks after the receiver's class only when the receiver is an
        instance; for a type variable (``self`` in an override the plugin types
        ``(self: Self) -> Self``, or ``self: T``) it asks with the callable's own name,
        ``"search of BaseModel"``, or a bare ``"write"`` for an unannotated def. A method
        of ``BaseModel`` gets its full name; for another class only the method's name is
        known, which the hooks dispatch on (each checks that the receiver is a model).
        """
        if "." in fullname:  # a full name (also of a view class: <subclass of "x" ...>.create)
            return fullname
        method, of, owner = fullname.partition(" of ")
        if not of:
            return fullname
        base_model = self._models.profile.base_model
        return f"{base_model}.{method}" if owner == base_model.rpartition(".")[2] else method

    def get_attribute_hook(self, fullname: str) -> Callable[[AttributeContext], Type] | None:
        """Resolve record-typed attributes of model classes from the reading addon."""
        if self._package is not None:
            self._bind_package_names()
        self._prepare_rebound()
        found = self._hooks.attribute
        if fullname not in found:
            self._type_request()
            hook = self._find_attribute_hook(fullname)
            if hook is None:
                kept = self._keeps_record(fullname)
                if kept is None:
                    return None  # asked again once mypy has inferred the attribute's type
                hook = self._kept_records if kept else None
            if hook is None and self._hook_gives_own_base(fullname):
                hook = _any_attribute
            found[fullname] = _soft(hook, _default_attribute, f"the hook of {fullname}")
        return found[fullname]

    def _kept_records(self, ctx: AttributeContext) -> Type:
        """Records kept on a class that is no model, with the fields their function asked for."""
        return self._asked.with_asked(self._values.kept_record(ctx), ctx)

    def _dict_get(self, ctx: MethodContext) -> Type:
        """``get`` of ``odoo.values.*``, and of a dict of mixed values."""
        found = values_get(ctx)
        return self._mixed.read(ctx) if found is ctx.default_return_type else found

    def _env_records(self, ctx: MethodContext) -> Type:
        """``env["model"]``, with the fields the function asked the model for."""
        return self._asked.with_asked(self._env.getitem(ctx), ctx)

    def _keeps_record(self, fullname: str) -> bool | None:
        """Whether an attribute of a class that is no model holds records of a model.

        ``None`` while its type is not known: mypy infers ``self.partner = ...`` when it
        checks the method that assigns it, which may be after the first read.
        """
        symbol = (
            lookup_fully_qualified(fullname, self._modules, raise_on_missing=False)
            if self._modules
            else None
        )
        variable = symbol.node if symbol is not None else None
        return self._values.keeps_record(variable) if isinstance(variable, Var) else False

    def _type_request(self) -> None:
        """Type ``odoo.http.request`` once mypy has checked its assignment (see ``type_request``).

        Asked at the first lookup of each name of a type-checking hook, so it also happens
        while ``odoo.http`` itself is checked: its cache entry then keeps the type, for a
        warm run in which no class hook runs before a module that uses ``request``.
        """
        classes = self._classes
        if not classes.request_typed and self._modules:
            classes.request_typed = type_request(self._models.profile, self._modules)

    # -- semantic analysis: _inherit -------------------------------------------------------

    def _find_customize_class_mro_hook(
        self, fullname: str
    ) -> Callable[[ClassDefContext], None] | None:
        """Give Odoo model classes the MRO of their registry class."""
        if fullname in self._models.profile.comodel_generic:
            return self._classes.comodel_generic
        if fullname == self._models.profile.request_class:
            return add_request_attributes
        contribution = self._models.registry.contribution(fullname)
        if contribution is None:
            return None
        return partial(self._classes.merge_bases, contribution)

    def get_base_class_hook(self, fullname: str) -> Callable[[ClassDefContext], None] | None:
        """Relax model meta attributes such as ``_inherit``; add delegated fields."""
        return self._after_class_body

    # -- type checking: env[...] and relational fields -------------------------------------

    def _find_method_hook(self, fullname: str) -> Callable[[MethodContext], Type] | None:
        """Type ``env["model.name"]`` and unannotated overrides of ``-> Self`` methods.

        ``create``, ``write`` and ``copy`` also get their ``vals`` checked (one hook per name, so
        the checks run in the same function).
        """
        profile = self._models.profile
        if fullname == DICT_GET:
            return self._dict_get
        if fullname in MAPPING_ITEMS:
            return partial(_first_of, (self._recordsets.declared_field, self._mixed.read))
        if fullname in MIXED_READS:
            return self._mixed.read
        if fullname in MIXED_ITERATIONS:
            return self._mixed.iterate
        if fullname == profile.env_getitem:
            return self._env_records
        if fullname == f"{profile.environment}.ref":
            return self._env.ref
        name = fullname.rpartition(".")[2]
        if name in VALS_METHODS:
            return partial(self._vals.check, name)
        if name in DOMAIN_METHODS:
            return partial(self._domains.check, name)
        if name in PARAMETER_DOMAIN_METHODS:
            return partial(self._domains.check_parameter, name, PARAMETER_DOMAIN_METHODS[name])
        if name == "read":
            return partial(self._recordsets.read_result, name)
        if fullname in COMPARISON_HOOKS:
            return self._comparisons.selection_keys
        if name == CONTAINS:
            return self._asked.contains  # ``"f" in records``
        if name == "fetch":
            return partial(
                _checked, partial(self._recordsets.check_field_names, name, "field_names")
            )
        if name in XML_ID_METHODS:
            return partial(self._env.xml_id_argument, name)
        if name in RECORD_PATH_METHODS:
            return partial(self._recordsets.record_path, name)
        if profile.untyped_fields and name == "__iter__":
            return self._recordsets.iterate  # Odoo 17: unannotated
        then = partial(keep_receiver, profile.base_model) if is_self_method(fullname) else None
        parameters = domain_parameters(self._declared_signature(fullname, method=True))
        if parameters:
            return partial(self._domains.annotated_parameters, parameters, name, then)
        return then

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
        if fullname == TRIGGER_FULLNAME:
            return self._domains.wrapped
        if fullname in MIXED_CALLS:
            return self._mixed.called
        if fullname == HASATTR:
            return self._asked.has_attribute
        if factory is None:
            parameters = domain_parameters(self._declared_signature(fullname, method=False))
            if parameters:
                name = fullname.rpartition(".")[2]
                return partial(self._domains.annotated_parameters, parameters, name, None)
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
        if fullname == DICT_GET:
            return partial(_signature_then, values_get_signature, self._mixed.read_signature)
        if fullname in MIXED_WRITES:
            return self._mixed.write
        if fullname in MIXED_READS:
            return self._mixed.read_signature
        profile = self._models.profile
        if is_create(fullname):
            return partial(create_signature, profile.base_model, untyped=profile.untyped_fields)
        if is_command_id_method(fullname):
            return partial(command_id_signature, profile.new_id)
        if is_browse(fullname):
            return partial(browse_signature, profile.base_model)
        if is_term_list_method(fullname):
            return term_list_signature
        domain_class = profile.domain_class  # Odoo 19+: DomainType parameters
        widen: Callable[[MethodSigContext], FunctionLike] | None = None
        if domain_class is not None and has_domain_type(
            self._declared_signature(fullname, method=True), domain_class
        ):
            widen = partial(self._domains.widen_signature, None)
        if fullname.rpartition(".")[2] == "filtered":
            return partial(self._recordsets.filtered_signature, widen)
        return widen

    def _declared_signature(self, fullname: str, method: bool) -> Type | None:
        """The declared type of the method or function a hook is asked for, if mypy has it.

        mypy names a method's hooks after the receiver's class (``addon.SaleOrder.search``),
        so the method is looked up in that class's MRO.
        """
        modules = self._modules
        if not modules:
            return None
        found: object
        if method:
            owner, _, name = fullname.rpartition(".")
            node = lookup_fully_qualified(owner, modules)
            info = node.node if node is not None else None
            found = info.get_method(name) if isinstance(info, TypeInfo) else None
        else:
            node = lookup_fully_qualified(fullname, modules)
            found = node.node if node is not None else None
        if isinstance(found, Decorator):
            found = found.func
        return found.type if isinstance(found, FuncDef) else None

    def _hook_gives_own_base(self, fullname: str) -> bool:
        """Whether a name is read through a ``__getattr__`` declared to return a base class.

        Odoo 20's ``TableSQL`` and ``FieldSQL`` read ``table.order_id.partner_id`` through
        ``__getattr__``, declared ``-> SQL``, their common base, which has no such hook:
        the declaration names the family, not the class, and the next attribute of the
        chain was an error. What such a hook gives has attributes that cannot be told
        from the declaration (``Any``).
        """
        owner, _, name = fullname.rpartition(".")
        symbol = (
            lookup_fully_qualified(owner, self._modules, raise_on_missing=False)
            if self._modules
            else None
        )
        info = symbol.node if symbol is not None else None
        if not isinstance(info, TypeInfo) or info.get(name) is not None:
            return False
        method = info.get_method(GETATTR)
        function = method.func if isinstance(method, Decorator) else method
        declared = function.type if isinstance(function, FuncDef) else None
        given = get_proper_type(declared.ret_type) if isinstance(declared, CallableType) else None
        return isinstance(given, Instance) and given.type in info.mro[1:-1]

    def _declared_records(self, fullname: str) -> bool:
        """Whether an attribute or property is declared as ``BaseModel``: records of no model."""
        symbol = (
            lookup_fully_qualified(fullname, self._modules, raise_on_missing=False)
            if self._modules
            else None
        )
        node = symbol.node if symbol is not None else None
        variable = node.var if isinstance(node, Decorator) else node
        declared = get_proper_type(variable.type) if isinstance(variable, Var) else None
        if isinstance(declared, CallableType):  # a property: what its getter returns
            declared = get_proper_type(declared.ret_type)
        return (
            isinstance(declared, Instance)
            and declared.type.fullname == self._models.profile.base_model
        )

    def _declared_by_odoo(self, class_fullname: str) -> bool:
        """Whether a class is one of Odoo's own, outside the addons.

        What Odoo declares too loosely is read as Odoo uses it; what an addon declares
        (``env: Environment | None`` of its own class) is its author's word.
        """
        package = self._models.settings.odoo_path.name
        if not class_fullname.startswith(f"{package}."):
            return False
        return self._models.registry.addons.addon_for_module(class_fullname) is None

    def _find_attribute_hook(self, fullname: str) -> Callable[[AttributeContext], Type] | None:
        """Resolve record-typed attributes of model classes from the reading addon."""
        profile = self._models.profile
        registry = self._models.registry
        if fullname in profile.env_records:
            return partial(self._env.record, profile.env_records[fullname])
        if fullname == f"{profile.request_class}.env":
            return partial(request_env, profile.environment)
        if fullname == f"{profile.environment}.cr":
            return env_cursor
        if profile.untyped_fields and fullname == f"{profile.base_model}.env":
            return partial(request_env, profile.environment)  # Odoo 17: a bare property
        required = self._models.settings.required_values
        if required and fullname == f"{profile.base_model}.display_name":
            return self._values.display_name  # Odoo 17, 18 annotate it on BaseModel
        owner, _, name = fullname.rpartition(".")
        if owner == profile.environment and self._declared_records(fullname):
            return partial(self._env.named_record, name)  # ``env.website`` (Odoo 20)
        if name == ENV and owner != profile.base_model and self._declared_by_odoo(owner):
            return partial(present_env, profile.environment)  # a controller's (Odoo 20)
        if name in LOG_ACCESS_USERS and registry.contribution(owner):
            # Declared as ``Many2one[Any]``: the res.users records the reading addon sees.
            return partial(self._env.record, "res.users")
        return self._values.record_attribute if registry.contribution(owner) else None


def _soft(
    hook: Callable[[ContextT], ResultT] | None,
    fallback: Callable[[ContextT], ResultT],
    what: str,
) -> Callable[[ContextT], ResultT] | None:
    """:func:`~odoo_typing.errors.fail_soft` of a hook, if there is one."""
    return fail_soft(hook, fallback, what) if hook is not None else None


def _default_return(ctx: MethodContext | FunctionContext) -> Type:
    return ctx.default_return_type


def _first_of(hooks: tuple[Callable[[MethodContext], Type], ...], ctx: MethodContext) -> Type:
    """The first hook of a method that gives another type than mypy's."""
    for hook in hooks:
        found = hook(ctx)
        if found is not ctx.default_return_type:
            return found
    return ctx.default_return_type


def _signature_then(
    first: Callable[[MethodSigContext], FunctionLike],
    then: Callable[[MethodSigContext], FunctionLike],
    ctx: MethodSigContext,
) -> FunctionLike:
    """Two signature hooks of one method, the second given what the first made."""
    signature = first(ctx)
    if not isinstance(signature, CallableType):
        return signature
    return then(MethodSigContext(ctx.type, ctx.args, signature, ctx.context, ctx.api))


def _default_signature(ctx: MethodSigContext) -> FunctionLike:
    return ctx.default_signature


def _default_attribute(ctx: AttributeContext) -> Type:
    return ctx.default_attr_type


def _any_attribute(ctx: AttributeContext) -> Type:
    """An attribute whose class cannot be told from its declaration (read: ``Any``)."""
    return ctx.default_attr_type if ctx.is_lvalue else AnyType(TypeOfAny.special_form)


def _any_type(_ctx: AnalyzeTypeContext) -> Type:
    return AnyType(TypeOfAny.from_error)


def _no_result(_ctx: ClassDefContext) -> None:
    return None


def _imported_modules(file: MypyFile) -> list[str]:
    """What a file's import statements name, as dotted names (``a.b`` and ``a.b.c``)."""
    found: list[str] = []
    for statement in file.imports:
        if isinstance(statement, Import):
            found += [name for name, _ in statement.ids]
        elif isinstance(statement, (ImportFrom, ImportAll)):
            source, _ = correct_relative_import(
                file.fullname, statement.relative, statement.id, file.is_package_init_file()
            )
            found.append(source)
            if isinstance(statement, ImportFrom):
                found += [f"{source}.{name}" for name, _ in statement.names]
    return found


def _every_class(ctx: ClassDefContext) -> None:
    """What the plugin does for a class of any kind, before mypy analyzes its body."""
    inherit_built_class(ctx)
    define_through_cls(ctx)


def _both(
    first: Callable[[ClassDefContext], None],
    second: Callable[[ClassDefContext], None],
    ctx: ClassDefContext,
) -> None:
    """Run two class hooks, one after the other."""
    first(ctx)
    second(ctx)


def _checked(check: Callable[[MethodContext], None], ctx: MethodContext) -> Type:
    """Run a check of a method call that keeps its declared return type."""
    check(ctx)
    return ctx.default_return_type


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

    def forget_declared(self) -> None:
        """Forget the hooks that depend on mypy's symbol tables, not only on the name.

        The method, function and signature hooks look at the declared signature
        (``odoo.domain.*`` and ``DomainType`` parameters); the class and attribute hooks
        depend on the registry and the profile alone.
        """
        self.method.clear()
        self.function.clear()
        self.signature.clear()


def plugin(version: str) -> type[OdooPlugin]:
    """Return the plugin class; called by mypy.

    A mypy version the plugin is not tested with gets a warning, never an exception:
    mypy would report it as an INTERNAL ERROR of its own and hide the message.

    Args:
        version: mypy version string.

    Returns:
        The plugin class.
    """
    if warning := mypy_version_warning(version):
        print(warning, file=sys.stderr)
    return OdooPlugin
