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
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from functools import partial

from mypy.errorcodes import ErrorCode
from mypy.lookup import lookup_fully_qualified
from mypy.nodes import (
    ARG_POS,
    MDEF,
    AssignmentStmt,
    CallExpr,
    ClassDef,
    Context,
    Expression,
    ListExpr,
    MemberExpr,
    MypyFile,
    NameExpr,
    PlaceholderNode,
    RefExpr,
    StrExpr,
    SymbolTableNode,
    TempNode,
    TupleExpr,
    TypeInfo,
    Var,
)
from mypy.options import Options
from mypy.plugin import (
    AttributeContext,
    CheckerPluginInterface,
    ClassDefContext,
    FunctionContext,
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
    LiteralType,
    NoneType,
    ProperType,
    TupleType,
    Type,
    TypedDictType,
    TypeOfAny,
    UnionType,
    get_proper_type,
)

# isort: split
# After mypy.types: these modules import each other at import time.
from mypy.checker import TypeChecker
from mypy.maptype import map_instance_to_supertype
from mypy.semanal import SemanticAnalyzer
from mypy.typeops import make_simplified_union

from odoo_typing.classes import (
    Lookup,
    add_delegated_fields,
    add_log_access_fields,
    create_view_class,
    install_mro,
    instance_of,
    make_comodel_generic,
    merge_mro,
    record_python_mro,
    view_class_name,
    view_class_origin,
)
from odoo_typing.compat import SUPPORTED_VERSIONS, OdooProfile, profile_for
from odoo_typing.config import WHERE, ConfigError, Settings, load_settings
from odoo_typing.domains import (
    DOMAIN_METHODS,
    ODOO_DOMAIN,
    DomainChecker,
    Term,
    string_domain_paths,
)
from odoo_typing.fingerprint import Fingerprints
from odoo_typing.guards import module_guard
from odoo_typing.methods import (
    annotate_self_overrides,
    create_signature,
    identity_decorator,
    identity_factory,
    is_create,
    is_self_method,
    keep_receiver,
)
from odoo_typing.odoo_version import version_mismatch_warning
from odoo_typing.overlay import overlay_differences
from odoo_typing.records import RecordStore, index_key, load_index
from odoo_typing.references import (
    KNOWN_CLASSES,
    METHOD_ARGUMENTS,
    ODOO_FIELD_REF,
    RECORD_PATH_METHODS,
    decorator_problem,
    did_you_mean,
    field_kind,
    missing_method_message,
    record_paths,
    resolve_path,
)
from odoo_typing.registry import MissingParent, Registry
from odoo_typing.scan_store import ScanStore
from odoo_typing.scanner import LOG_ACCESS_USERS, POSITIONAL_PARAMETERS, Contribution, FieldInfo
from odoo_typing.stamps import stamp
from odoo_typing.vals import ValsChecker, ValsTarget, context_defaults, stored, vals_dicts
from odoo_typing.xmlids import ODOO_XMLID, ODOO_XMLID_DEPENDS, automatic_model

__all__ = ["ODOO_MODEL", "ODOO_MODEL_DEPENDS", "OdooPlugin", "plugin"]

ODOO_MODEL = ErrorCode("odoo-model", "Check that Odoo model names exist", "Odoo")
ODOO_MODEL_DEPENDS = ErrorCode(
    "odoo-model-depends",
    "Check that Odoo models are defined in the addon's dependencies",
    "Odoo",
    sub_code_of=ODOO_MODEL,
)

DEPENDENCY_PRIORITY = 10  # mypy.build.PRI_MED
# Field classes whose reading ``_value_get`` types (``Image`` is a ``Binary``).
VALUE_KINDS = frozenset({"Binary", "Reference", "Selection"})
# Methods whose first argument is ``vals`` (``create``: also a list of them;
# ``copy``: its ``default``).
VALS_METHODS = frozenset({"create", "write", "copy"})


class OdooPlugin(Plugin):
    """Teach mypy how the Odoo registry assembles models."""

    def __init__(self, options: Options) -> None:
        """Load settings and prepare the (lazy) model registry.

        A configuration problem ends mypy with the message and exit status 2, like
        mypy's own configuration errors (not as a crash of the plugin).
        """
        super().__init__(options)
        self._settings = _settings_or_exit(options)
        self._profile = _profile_or_exit(self._settings)
        interpreter = (sys.version_info[0], sys.version_info[1])
        if warning := _python_version_warning_or_exit(self._profile, options, interpreter):
            print(warning, file=sys.stderr)
        if warning := version_mismatch_warning(self._profile.version, self._settings.odoo_path):
            print(warning, file=sys.stderr)
        if warning := _package_bases_warning(self._profile, self._settings, options):
            print(warning, file=sys.stderr)
        self._store = RecordStore.for_cache_dir(options.cache_dir)
        self._config_stamp = stamp(options.config_file) if options.config_file else None
        self._inputs_checked = False
        self._overlay_checked = False
        self._request_typed = False
        self._new_registry()

    def _new_registry(self) -> None:
        index = partial(load_index, self._settings, self._store)
        scans = ScanStore(self._store, index_key(self._settings))
        self.registry = Registry(self._settings, index, scans)
        self._fingerprints = Fingerprints(self.registry, self._store, self._settings)

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
        if self._config_changed() or self.registry.inputs_changed():
            self._new_registry()
        if not self._overlay_checked:
            self._overlay_checked = True
            if warning := _stale_overlay_warning(self._settings, self.registry):
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
        self._settings, self._profile = settings, profile
        self._request_typed = False
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
        if self.registry.misnamed(file.fullname):
            return []  # never make mypy load the addon again under its configured name
        modules = set(self.registry.dependency_modules(file.fullname, file.path))
        modules.discard(file.fullname)
        return [(DEPENDENCY_PRIORITY, module, -1) for module in sorted(modules)]

    # -- semantic analysis: _inherit -------------------------------------------------------

    def get_customize_class_mro_hook(
        self, fullname: str
    ) -> Callable[[ClassDefContext], None] | None:
        """Give Odoo model classes the MRO of their registry class."""
        if fullname in self._profile.comodel_generic:
            return self._comodel_generic
        contribution = self.registry.contribution(fullname)
        if contribution is None:
            return None
        return partial(self._merge_bases, contribution)

    def _comodel_generic(self, ctx: ClassDefContext) -> None:
        # Bound ``object``: ``BaseModel`` may not be analyzed yet (an import cycle).
        object_type = ctx.api.named_type("builtins.object")
        make_comodel_generic(ctx.cls, object_type, self._profile.is_field_class)

    def _merge_bases(self, contribution: Contribution, ctx: ClassDefContext) -> None:
        """Replace a model class's MRO with the one Odoo's registry gives it.

        The MRO is built from the registry, never copied from a base class whose own
        merge may not have run yet. A class of the chain that is not analyzed yet (a
        later module of the same import cycle) makes mypy analyze this class again;
        on the final iteration, or for modules outside this run, it is left out.
        """
        self._report_inherit_problems(contribution, ctx)
        info = ctx.cls.info
        if info.bad_mro:
            return  # mypy already reported the Python bases
        lookup = _semanal_lookup(ctx)
        python_mro = record_python_mro(info, lookup)
        incomplete = _incomplete(ctx)
        chain = _class_infos(
            self.registry.linearization(contribution)[1:], ctx.api.modules, incomplete
        )
        if chain.pending and not ctx.api.final_iteration:
            ctx.api.defer()
            return
        merge = merge_mro(info, python_mro, chain.infos, lookup)
        bases = _class_infos(self.registry.bases(contribution), ctx.api.modules, incomplete).infos
        # A registry base written in the source keeps its type arguments (``Mix[int]``).
        written = {base.type: base for base in info.bases}
        own = [written.pop(base) if base in written else instance_of(base) for base in bases]
        install_mro(info, merge.order, [*own, *written.values()], partial=chain.missing)
        if merge.conflict or self.registry.mro_conflict(contribution):
            ctx.api.msg.note(_conflict_message(ctx.cls.name), ctx.cls, code=ODOO_MODEL)

    def _report_inherit_problems(self, contribution: Contribution, ctx: ClassDefContext) -> None:
        """Report what Odoo's ``_build_model`` would reject when it loads the class.

        mypy may run this hook more than once for a class (deferred analysis); it
        drops repeated identical errors, so each problem is shown once.
        """
        anchor = _inherit_value(ctx.cls) or ctx.cls
        for problem in self.registry.missing_parents(contribution):
            message = _missing_parent_message(problem, contribution.addon)
            code = ODOO_MODEL_DEPENDS if problem.outside_addons else ODOO_MODEL
            ctx.api.fail(message, anchor, code=code)
        cycle = self.registry.inherit_cycle(contribution)
        own = [item for item in cycle if item.addon == contribution.addon]
        if own and own[0] == contribution:  # once per cycle and addon, at its first class
            names = ", ".join(f'"{item.class_name}"' for item in cycle)
            message = f"_inherit cycle: classes {names} inherit from each other"
            ctx.api.fail(message, ctx.cls, code=ODOO_MODEL)

    def get_base_class_hook(self, fullname: str) -> Callable[[ClassDefContext], None] | None:
        """Relax model meta attributes such as ``_inherit``; add delegated fields."""
        return self._after_class_body

    def _after_class_body(self, ctx: ClassDefContext) -> None:
        """Run once the class body is analyzed (and again for each written base)."""
        _normalize_meta_attributes(ctx, self._profile)
        if not self._request_typed:
            self._request_typed = _type_request(self._profile, ctx.api.modules)
        if not ctx.cls.info.has_base(self._profile.base_model):
            return  # the hook runs for every class, also outside Odoo
        annotate_self_overrides(ctx)
        contribution = self.registry.contribution(ctx.cls.info.fullname)
        if contribution is not None:
            self._delegate_fields(contribution, ctx)
            if self.registry.log_access(contribution):
                add_log_access_fields(ctx.cls.info, _log_access_types(ctx, self._profile))

    def _delegate_fields(self, contribution: Contribution, ctx: ClassDefContext) -> None:
        """Add the fields of the models a class delegates to (``_inherits``).

        They are added after the class body, so the class's own fields win, from the
        delegated registry classes as the class's addon sees them. A delegated class
        that is not analyzed yet makes mypy analyze this class again.
        """
        chain = self.registry.linearization(contribution)
        sources: list[tuple[TypeInfo, tuple[str, ...]]] = []
        delegates = self.registry.delegate_closure(chain, contribution.model, contribution.addon)
        for delegate in delegates:
            classes = self.registry.delegate_chain(delegate, contribution.addon)
            found = _field_sources(classes, ctx.api.modules, _incomplete(ctx))
            if found is None and not ctx.api.final_iteration:
                ctx.api.defer()
                return
            sources += found or []
        add_delegated_fields(ctx.cls.info, sources)

    # -- type checking: env[...] and relational fields -------------------------------------

    def get_method_hook(self, fullname: str) -> Callable[[MethodContext], Type] | None:
        """Type ``env["model.name"]`` and unannotated overrides of ``-> Self`` methods.

        ``create``, ``write`` and ``copy`` also get their ``vals`` checked (one hook per name, so
        the checks run in the same function).
        """
        if fullname == self._profile.env_getitem:
            return self._env_getitem
        if fullname == f"{self._profile.environment}.ref":
            return self._env_ref
        name = fullname.rpartition(".")[2]
        if name in VALS_METHODS:
            return partial(self._check_vals, name)
        if name in DOMAIN_METHODS:
            return partial(self._check_domain, name)
        if name == "read":
            return partial(self._read_result, name)
        if name in RECORD_PATH_METHODS:
            return partial(self._record_path, name)
        if self._profile.untyped_fields and name == "__iter__":
            return self._iterate_records  # Odoo 17: unannotated
        if is_self_method(fullname):
            return partial(keep_receiver, self._profile.base_model)
        return None

    def _check_vals(self, method: str, ctx: MethodContext) -> Type:
        """Check the literal ``vals`` of ``create`` / ``write`` / ``copy(default=)``."""
        base_model = self._profile.base_model
        receiver = get_proper_type(ctx.type)
        argument = ctx.args[0][0] if ctx.args and ctx.args[0] else None
        addon_name = self.registry.addon_name_for_path(ctx.api.path)
        model = self._model_of(receiver.type) if isinstance(receiver, Instance) else None
        if argument is not None and addon_name is not None and model is not None:
            target = self._vals_target(model, addon_name)
            checker = self._vals_checker(ctx.api, addon_name)
            for vals in vals_dicts(argument, many=method == "create"):
                checker.check(target, vals)
        call = ctx.context
        if (
            method == "create"
            and addon_name is not None
            and model is not None
            and isinstance(call, CallExpr)
            and isinstance(call.callee, MemberExpr)
        ):
            defaults = context_defaults(call.callee.expr)  # x.with_context(default_y=...).create
            if defaults:
                target = self._vals_target(model, addon_name)
                self._vals_checker(ctx.api, addon_name).check_defaults(target, defaults)
        if is_self_method(method):
            return keep_receiver(base_model, ctx)
        return ctx.default_return_type

    def _iterate_records(self, ctx: MethodContext) -> Type:
        """``for record in records`` (Odoo 17): each record has the receiver's type."""
        receiver = get_proper_type(ctx.type)
        if not isinstance(receiver, Instance) or not receiver.type.has_base(
            self._profile.base_model
        ):
            return ctx.default_return_type
        return _checker(ctx.api).named_generic_type("typing.Iterator", [receiver])

    def _check_domain(self, method: str, ctx: MethodContext) -> Type:
        """Check the literal ``domain`` of ``search`` / ``search_count`` / ``_read_group``...

        Only on a concrete model: a mixin's domains name fields of the models using it.
        """
        receiver = get_proper_type(ctx.type)
        addon_name = self.registry.addon_name_for_path(ctx.api.path)
        model = self._model_of(receiver.type) if isinstance(receiver, Instance) else None
        index = ctx.callee_arg_names.index("domain") if "domain" in ctx.callee_arg_names else 0
        domain = ctx.args[index][0] if len(ctx.args) > index and ctx.args[index] else None
        if (
            domain is not None
            and model is not None
            and addon_name is not None
            and self.registry.concrete(model, addon_name)
        ):
            self._check_domain_expression(domain, model, addon_name, ctx.api)
        if method == "search_read":
            return self._read_result(method, ctx)
        if is_self_method(method):
            return keep_receiver(self._profile.base_model, ctx)
        return ctx.default_return_type

    def _read_result(self, method: str, ctx: MethodContext) -> Type:
        """``read(["a", "b"])`` / ``search_read(fields=[...])``: a list of TypedDicts.

        Each dict has ``id`` and the literal field names, typed as ``convert_to_read``
        returns them (:func:`_read_value`); a name that is not a field is reported
        (Odoo raises ``ValueError``) on a concrete model.
        """
        receiver = get_proper_type(ctx.type)
        names = _literal_strings_argument(ctx, "fields", 0 if method == "read" else 1)
        ids_only = _ids_only(_argument(ctx, "load", 1 if method == "read" else -1))
        if not isinstance(receiver, Instance) or names is None or ids_only is None:
            return ctx.default_return_type
        model = self._model_of(receiver.type)
        addon_name = self.registry.addon_name_for_path(ctx.api.path)
        if model is None or addon_name is None:
            return ctx.default_return_type
        fields = self.registry.fields(model, addon_name)
        checker = _checker(ctx.api)
        items: dict[str, Type] = {"id": checker.named_type("builtins.int")}
        for node in names:
            info = fields.get(node.value)
            if info is None:
                if self.registry.concrete(model, addon_name):
                    message = f'Odoo model "{model}" has no field "{node.value}" in {method}()'
                    message += did_you_mean(node.value, fields)
                    ctx.api.fail(message, node, code=ODOO_FIELD_REF)
                return ctx.default_return_type
            value = _read_value(info, ids_only, checker)
            items[node.value] = (
                value
                or self._path_type(receiver, node.value, ctx)
                or AnyType(TypeOfAny.special_form)
            )
        fallback = checker.named_type("typing._TypedDict")
        row = TypedDictType(items, set(items), set(), fallback)
        return checker.named_generic_type("builtins.list", [row])

    def _record_path(self, method: str, ctx: MethodContext) -> Type:
        """``mapped("a.b")``, ``filtered("a.b")``, ``sorted("a")``, ``grouped("a")``.

        The fields of the string must exist (``odoo-field-ref``, on a concrete model);
        ``mapped`` then returns the records of the last field's comodel, or a list of its
        values, and ``grouped`` a dict from its values to records of the receiver.
        """
        result = (
            keep_receiver(self._profile.base_model, ctx)
            if is_self_method(method)
            else ctx.default_return_type
        )
        receiver = get_proper_type(ctx.type)
        argument = ctx.args[0][0] if ctx.args and ctx.args[0] else None
        if not isinstance(receiver, Instance) or not isinstance(argument, StrExpr):
            return result
        if self._model_of(receiver.type) is None:
            return result  # a ``sorted`` / ``mapped`` method of another class
        if not argument.value or not self._check_record_paths(method, receiver, argument, ctx):
            return result
        if method not in {"mapped", "grouped"}:
            return result
        value = self._path_type(receiver, argument.value, ctx)
        if value is None:
            return result
        checker = _checker(ctx.api)
        if method == "grouped":
            return checker.named_generic_type("builtins.dict", [value, receiver])
        records = get_proper_type(value)
        if isinstance(records, Instance) and records.type.has_base(self._profile.base_model):
            return records
        return checker.named_generic_type("builtins.list", [value])

    def _check_record_paths(
        self, method: str, receiver: Instance, argument: StrExpr, ctx: MethodContext
    ) -> bool:
        """Report the fields of a path argument that do not exist; whether all do."""
        paths, problem = record_paths(method, argument.value, self._profile.sorted_order)
        if problem is not None:
            ctx.api.fail(problem, argument, code=ODOO_FIELD_REF)
            return False
        model = self._model_of(receiver.type)
        addon_name = self.registry.addon_name_for_path(ctx.api.path)
        if model is None or addon_name is None or not self.registry.concrete(model, addon_name):
            return True
        fields_of = partial(self.registry.fields, addon_name=addon_name)
        valid = True
        for path in paths:
            found = resolve_path(path, model, fields_of, f'{method}("{argument.value}")')
            if found.problem is not None:
                ctx.api.fail(found.problem, argument, code=ODOO_FIELD_REF)
                valid = False
        return valid

    def _path_type(self, receiver: Instance, path: str, ctx: MethodContext) -> Type | None:
        """The type of ``record.a.b`` for the path ``a.b``; ``None`` if it has errors."""
        expression: Expression = TempNode(receiver, context=ctx.context)
        for name in path.split("."):
            expression = MemberExpr(expression, name)
            expression.set_line(ctx.context)
        checker = _checker(ctx.api)
        with checker.msg.filter_errors() as errors:
            value = checker.expr_checker.accept(expression)
        return None if errors.has_new_errors() else value

    def _domain_checker(self, addon_name: str, api: CheckerPluginInterface) -> DomainChecker:
        return DomainChecker(
            partial(api.fail, code=ODOO_DOMAIN),
            partial(self.registry.fields, addon_name=addon_name),
            self._profile.domain_operators,
        )

    def _check_domain_expression(
        self, domain: Expression, model: str, addon_name: str, api: CheckerPluginInterface
    ) -> None:
        """Check a literal domain list, or a ``Domain([...])`` / ``Domain(path, op, value)``."""
        checker = self._domain_checker(addon_name, api)
        if isinstance(domain, ListExpr):
            checker.check(model, domain.items)
            return
        domain_class = self._profile.domain_class
        if (
            domain_class is None
            or not isinstance(domain, CallExpr)
            or not isinstance(domain.callee, RefExpr)
            or domain.callee.fullname != domain_class
            or any(kind != ARG_POS for kind in domain.arg_kinds)
        ):
            return
        if len(domain.args) == 1 and isinstance(domain.args[0], ListExpr):
            checker.check(model, domain.args[0].items)
        elif len(domain.args) == 3:
            checker.check(model, [TupleExpr(list(domain.args))])

    def _check_field_domain(
        self, call: CallExpr, field_class: str, addon_name: str, api: CheckerPluginInterface
    ) -> None:
        """Check the ``domain=`` of a relational field against its comodel."""
        arguments = dict(_field_arguments(call, field_class))
        comodel = arguments.get("comodel_name")
        domain = arguments.get("domain")
        if not isinstance(comodel, StrExpr) or domain is None:
            return
        if not self.registry.concrete(comodel.value, addon_name):
            return
        if not isinstance(domain, StrExpr):
            self._check_domain_expression(domain, comodel.value, addon_name, api)
            return
        checker = self._domain_checker(addon_name, api)
        for path, operator in string_domain_paths(domain.value):
            path_node = StrExpr(path)
            path_node.set_line(domain)
            operator_node = None
            if operator is not None:
                operator_node = StrExpr(operator)
                operator_node.set_line(domain)
            checker.check_term(comodel.value, Term(path_node, operator_node, None))

    def _vals_checker(self, api: CheckerPluginInterface, addon_name: str) -> ValsChecker:
        return ValsChecker(
            _checker(api),
            self._profile.base_model,
            self._profile.new_id,
            self._model_of,
            partial(self.registry.concrete, addon_name=addon_name),
            partial(self._vals_target, addon_name=addon_name),
        )

    def _vals_target(self, model: str, addon_name: str) -> ValsTarget:
        """The fields the vals of a model are checked against, as an addon sees them."""
        return ValsTarget(
            model,
            self.registry.fields(model, addon_name),
            check_keys=self.registry.concrete(model, addon_name),
            ignore_keys=self._settings.vals_ignore_keys,
        )

    def _field_set(self, ctx: MethodSigContext) -> FunctionLike:
        """Check the value assigned to a field (``record.partner_id = value``).

        mypy types ``record.name = value`` as a call of the descriptor's ``__set__``;
        the signature is kept (Odoo leaves the value unannotated) and the value is
        checked like a ``vals`` value of that field (:mod:`odoo_typing.vals`).
        """
        lvalue = ctx.context
        records = ctx.args[0][0] if len(ctx.args) == 2 and ctx.args[0] else None
        value = ctx.args[1][0] if records is not None and ctx.args[1] else None
        if not isinstance(lvalue, MemberExpr) or not isinstance(records, TempNode):
            return ctx.default_signature
        record = get_proper_type(records.type)
        model = self._model_of(record.type) if isinstance(record, Instance) else None
        addon_name = self.registry.addon_name_for_path(ctx.api.path)
        if value is None or model is None or addon_name is None:
            return ctx.default_signature
        info = self.registry.fields(model, addon_name).get(lvalue.name)
        if info is None:
            return ctx.default_signature
        checker = _checker(ctx.api)
        with checker.msg.filter_errors():  # reported where mypy checks the rvalue
            value_type = checker.expr_checker.accept(value)
        self._vals_checker(ctx.api, addon_name).check_value(model, info, value, value_type)
        return ctx.default_signature

    def get_function_hook(self, fullname: str) -> Callable[[FunctionContext], Type] | None:
        """Bind relational field constructors to their comodel; see through decorators."""
        profile = self._profile
        if fullname in profile.relational_classes:
            return partial(self._field_definition, self._relational_field)
        if profile.is_field_class(fullname):
            return partial(self._field_definition, None)
        if fullname in profile.identity_decorators:
            return identity_decorator
        factory = (
            partial(identity_factory, profile.identity_template)
            if fullname in profile.identity_factories
            else None
        )
        if fullname in profile.field_decorators:
            return partial(self._decorator_fields, profile.field_decorators[fullname], factory)
        return factory

    def get_method_signature_hook(
        self, fullname: str
    ) -> Callable[[MethodSigContext], FunctionLike] | None:
        """Type reading a relational field; check assigned values; let ``create`` take a dict."""
        # mypy names the hook after the descriptor's own class (``odoo.fields.Many2one``,
        # a custom subclass...), so match every ``__get__`` and check the MRO in the hook.
        if fullname.endswith(".__get__"):
            return self._relational_get
        if fullname.endswith(".__set__"):
            return self._field_set
        if not is_create(fullname):
            return None
        profile = self._profile
        return partial(create_signature, profile.base_model, untyped=profile.untyped_fields)

    def get_attribute_hook(self, fullname: str) -> Callable[[AttributeContext], Type] | None:
        """Resolve record-typed attributes of model classes from the reading addon."""
        if fullname in self._profile.env_records:
            return partial(self._env_record, self._profile.env_records[fullname])
        if fullname == f"{self._profile.request_class}.env":
            return partial(_request_env, self._profile.environment)
        if self._profile.untyped_fields and fullname == f"{self._profile.base_model}.env":
            return partial(_request_env, self._profile.environment)  # Odoo 17: a bare property
        owner, _, name = fullname.rpartition(".")
        if name in LOG_ACCESS_USERS and self.registry.contribution(owner):
            # Declared as ``Many2one[Any]``: the res.users records the reading addon sees.
            return partial(self._env_record, "res.users")
        return self._record_attribute if self.registry.contribution(owner) else None

    def _env_record(self, model: str, ctx: AttributeContext) -> Type:
        """``env.user`` and the like: a record of a model, as the reading addon sees it."""
        addon_name = self.registry.addon_name_for_path(ctx.api.path)
        if ctx.is_lvalue or addon_name is None:
            return ctx.default_attr_type
        resolved = self._env_model(model, addon_name, ctx.api)
        return resolved if isinstance(resolved, Instance) else ctx.default_attr_type

    def _record_attribute(self, ctx: AttributeContext) -> Type:
        """Re-resolve an attribute whose type is a model class from the reading addon.

        For instance base's annotated ``parent_id: Partner = fields.Many2one(...)``: the
        declaring addon's class has fewer extensions than the reading addon sees.
        Assignments keep the declared type.
        """
        declared = get_proper_type(ctx.default_attr_type)
        if ctx.is_lvalue or not isinstance(declared, Instance):
            return ctx.default_attr_type
        owner = get_proper_type(ctx.type)
        owner_info = owner.type if isinstance(owner, Instance) else None
        return self._comodel_at_use_site(declared, owner_info, ctx.api) or ctx.default_attr_type

    def _view_type(self, model: str, addon_name: str, api: CheckerPluginInterface) -> ViewType:
        """The type of ``env[model]`` as seen from an addon.

        Returns:
            The model's last class if its MRO is the whole registry class; otherwise a
            merged class (created in the module being checked); ``Any`` if none of its
            classes is loaded in this mypy run; ``None`` if no visible addon defines it.
        """
        view = self.registry.view_class(model, addon_name)
        if view is None:
            return None
        checker = _checker(api)
        if view.tip is not None:
            tip = _class_infos([view.tip], checker.modules)
            if tip.infos:
                return instance_of(tip.infos[0])
        chain = _class_infos(view.chain, checker.modules)
        own = [item for item in view.chain if item.model == model]
        if not _class_infos(own, checker.modules).infos:
            return AnyType(TypeOfAny.special_form)  # e.g. --follow-imports=skip
        imported = _imported_view_class(
            view_class_name(model, addon_name), checker, self.registry.addons.addon_for_module
        )
        if imported is not None:
            return instance_of(imported)
        bases = _class_infos(view.bases, checker.modules).infos
        lookup = partial(_lookup, checker.modules)
        merged = create_view_class(
            checker.tree, model, addon_name, chain.infos, bases, lookup, chain.missing
        )
        for delegate in self.registry.delegate_closure(view.chain, model, addon_name):
            classes = self.registry.delegate_chain(delegate, addon_name)
            add_delegated_fields(merged, _field_sources(classes, checker.modules) or [])
        return instance_of(merged)

    def _env_ref(self, ctx: MethodContext) -> Type:
        """``env.ref("module.name")``: the records of the XML id's model.

        The id must be created by the data files of the addon's closure (``odoo-xmlid``;
        ``odoo-xmlid-depends`` when only an addon outside it creates it). With
        ``raise_if_not_found=False`` the result may be ``None``, and an id that is not
        found is not an error (the code expects it to be missing).
        """
        xml_id = _argument(ctx, "xml_id", 0)
        addon_name = self.registry.addon_name_for_path(ctx.api.path)
        if not isinstance(xml_id, StrExpr) or addon_name is None:
            return ctx.default_return_type
        optional = _is_false(_argument(ctx, "raise_if_not_found", 1))
        model = self._xml_id_model(xml_id, addon_name, optional, ctx.api)
        if model is None:
            return ctx.default_return_type
        records = self._view_type(model, addon_name, ctx.api)
        if not isinstance(records, Instance):
            return ctx.default_return_type
        return UnionType([records, NoneType()]) if optional else records

    def _xml_id_model(
        self, xml_id: StrExpr, addon_name: str, optional: bool, api: CheckerPluginInterface
    ) -> str | None:
        """The model of an XML id as an addon sees it; report it if it is not created."""
        name = xml_id.value
        module, _, local = name.partition(".")
        if not local:
            api.fail(f'XML id "{name}" has no module ("module.name")', xml_id, code=ODOO_XMLID)
            return None
        known = self.registry.xml_ids(addon_name)
        if name in known:
            return known[name]
        closure = self.registry.addons.closure(addon_name)
        if module in closure and (automatic := automatic_model(local)) is not None:
            return automatic
        if optional or self.registry.guarded(api.path, module_guard(module), xml_id.line):
            return None
        outside = self.registry.xml_id_outside(name, addon_name)
        if outside is not None:
            message = (
                f'XML id "{name}" is created by addon "{outside}", which is not in the '
                f'dependencies of "{addon_name}"'
            )
            api.fail(message, xml_id, code=ODOO_XMLID_DEPENDS)
            return None
        if module not in closure:
            return None  # created by code (``local_es.x``), or by an addon not on disk
        same_module = [item for item in known if item.startswith(f"{module}.")]
        message = f'Unknown XML id "{name}"' + did_you_mean(name, same_module)
        api.fail(message, xml_id, code=ODOO_XMLID)
        return None

    def _env_getitem(self, ctx: MethodContext) -> Type:
        model = _model_name_argument(ctx)
        addon_name = self.registry.addon_name_for_path(ctx.api.path)
        if addon_name is None:
            return ctx.default_return_type  # outside any addon: see the module docstring
        if model is None:
            # A name known only at run time: ``BaseModel`` would make every field read
            # an error, so ``Any`` unless the user asked for ``dynamic_env = "base"``.
            if self._settings.dynamic_env == "any":
                return AnyType(TypeOfAny.special_form)
            return ctx.default_return_type
        if self.registry.view_class(model, addon_name) is None:
            outside = self._unknown_model(model, addon_name, ctx.context, ctx.api)
            return outside or ctx.default_return_type
        return self._env_model(model, addon_name, ctx.api) or ctx.default_return_type

    def _env_model(self, model: str, addon_name: str, api: CheckerPluginInterface) -> ViewType:
        """A model's records as an addon sees them; ``Any`` if not loaded with the module."""
        module = _checker(api).tree.fullname
        if not self.registry.loaded_with(model, addon_name, module, api.path):
            # ``env2 = self.env; env2["x"]``: the scan saw no model name, so no edge
            # makes mypy load the model's classes with this module.
            return AnyType(TypeOfAny.special_form)
        return self._view_type(model, addon_name, api)

    def _unknown_model(
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
            message = f'Odoo model "{model}" is defined in {_outside(outside, addon_name)}'
            api.fail(message, context, code=ODOO_MODEL_DEPENDS)
            return AnyType(TypeOfAny.special_form)
        api.fail(f'Unknown Odoo model "{model}"', context, code=ODOO_MODEL)
        return None

    def _field_definition(
        self, then: Callable[[FunctionContext], Type] | None, ctx: FunctionContext
    ) -> Type:
        """Check the names a field definition refers to, then type it (``then``)."""
        owner = self._field_owner(ctx)
        default = get_proper_type(ctx.default_return_type)
        if (
            owner is not None
            and isinstance(ctx.context, CallExpr)
            and isinstance(default, Instance)
        ):
            self._check_field_references(ctx.context, default.type, *owner, ctx.api)
            if default.type.fullname in self._profile.relational_classes:
                self._check_field_domain(ctx.context, default.type.name, owner[1], ctx.api)
        return then(ctx) if then is not None else ctx.default_return_type

    def _field_owner(self, ctx: FunctionContext) -> tuple[str, str, TypeInfo] | None:
        """The model, addon and class whose body a field definition is in, if a model's."""
        checker = _checker(ctx.api)
        info = checker.scope.active_class()
        addon_name = self.registry.addon_name_for_path(ctx.api.path)
        model = self._model_of(info) if info is not None else None
        if info is None or model is None or addon_name is None:
            return None
        return model, addon_name, info

    def _check_field_references(
        self,
        call: CallExpr,
        field_class: TypeInfo,
        model: str,
        addon_name: str,
        owner: TypeInfo,
        api: CheckerPluginInterface,
    ) -> None:
        """Report fields of ``related=`` and methods of ``compute=``... that do not exist.

        Not in abstract models: their fields refer to fields and methods of the models
        that use them.
        """
        if not field_class.has_base(self._profile.field_base) or not self.registry.concrete(
            model, addon_name
        ):
            return
        for argument, value in _field_arguments(call, field_class.name):
            if not isinstance(value, StrExpr):
                continue
            if argument == "related":
                self._check_related(value, field_class.name, model, addon_name, api)
            elif argument in METHOD_ARGUMENTS and not (
                argument == "selection" and field_class.name not in {"Selection", "Reference"}
            ):
                methods = self.registry.methods(model, addon_name)
                if value.value not in methods and owner.get(value.value) is None:
                    message = missing_method_message(model, argument, value.value, methods)
                    api.fail(message, value, code=ODOO_FIELD_REF)

    def _check_related(
        self,
        path: StrExpr,
        field_class: str,
        model: str,
        addon_name: str,
        api: CheckerPluginInterface,
    ) -> None:
        fields_of = partial(self.registry.fields, addon_name=addon_name)
        result = resolve_path(path.value, model, fields_of, f'related="{path.value}"')
        if result.problem is not None:
            api.fail(result.problem, path, code=ODOO_FIELD_REF)
            return
        target = result.field
        if (
            target is not None
            and field_class in KNOWN_CLASSES
            and target.field_type in KNOWN_CLASSES
            and field_kind(field_class) != field_kind(target.field_type)
        ):
            api.fail(
                f'Related field is a {field_class}, but "{path.value}" is a '
                f'{target.field_type} field of Odoo model "{result.model}"',
                path,
                code=ODOO_FIELD_REF,
            )

    def _decorator_fields(
        self,
        decorator: str,
        then: Callable[[FunctionContext], Type] | None,
        ctx: FunctionContext,
    ) -> Type:
        """Check the fields that ``@api.depends(...)``, ``constrains``, ``onchange`` name.

        Only in the body of a concrete model's class, as for field definitions.
        """
        owner = self._field_owner(ctx)
        if owner is not None and self.registry.concrete(owner[0], owner[1]):
            model, addon_name, _ = owner
            fields_of = partial(self.registry.fields, addon_name=addon_name)
            for argument in [item for arguments in ctx.args for item in arguments]:
                if not isinstance(argument, StrExpr):
                    continue  # a lambda, ``*names``
                problem = decorator_problem(decorator, argument.value, model, fields_of)
                if problem is not None:
                    ctx.api.fail(problem, argument, code=ODOO_FIELD_REF)
        return then(ctx) if then is not None else ctx.default_return_type

    def _related_comodel(self, ctx: FunctionContext) -> str | None:
        """The comodel of the last field of a ``related=`` path in a field definition."""
        owner = self._field_owner(ctx)
        call = ctx.context
        if owner is None or not isinstance(call, CallExpr):
            return None
        model, addon_name, _ = owner
        path = dict(_field_arguments(call, "")).get("related")
        if not isinstance(path, StrExpr):
            return None
        fields_of = partial(self.registry.fields, addon_name=addon_name)
        target = resolve_path(path.value, model, fields_of, "").field
        if target is None or "comodel" in target.dynamic:
            return None
        return target.comodel

    def _relational_field(self, ctx: FunctionContext) -> Type:
        """Bind ``fields.Many2one("x")`` to the class that defines model ``x``.

        The declared type is the same for every declaration of a field with that
        comodel, whichever addon declares it; reading the field resolves the comodel
        again from the reading addon (:meth:`_relational_get`).
        """
        default = get_proper_type(ctx.default_return_type)
        if not isinstance(default, Instance):
            return ctx.default_return_type
        comodel = _comodel_expression(ctx)
        addon_name = self.registry.addon_name_for_path(ctx.api.path)
        name = comodel.value if comodel is not None else self._related_comodel(ctx)
        if name is None or addon_name is None:
            # A redefinition (``fields.Many2one(ondelete=...)``) keeps the comodel of the
            # earlier declaration; mypy cannot infer ``M`` from such a call.
            unknown = AnyType(TypeOfAny.special_form)
            return default.copy_modified(args=[unknown]) if default.args else default
        definition = self.registry.definition(name, addon_name)
        if definition is None:
            if comodel is not None:
                self._unknown_model(name, addon_name, comodel, ctx.api)
            unknown = AnyType(TypeOfAny.from_error)
            return default.copy_modified(args=[unknown]) if default.type.type_vars else default
        if not default.type.type_vars:
            return default  # Odoo 17: not generic; reading the field finds the comodel
        found = _class_infos([definition], _checker(ctx.api).modules).infos
        model_type = instance_of(found[0]) if found else AnyType(TypeOfAny.special_form)
        return default.copy_modified(args=[model_type])

    def _relational_get(self, ctx: MethodSigContext) -> FunctionLike:
        descriptor = get_proper_type(ctx.type)
        signature = ctx.default_signature
        if not isinstance(descriptor, Instance) or not isinstance(signature, CallableType):
            return signature
        mro = descriptor.type.mro
        relational = self._profile.relational_base
        base = next((item for item in mro if item.fullname == relational), None)
        if base is None:
            return self._value_get(descriptor, signature, ctx)
        records = _record_type(ctx)
        if isinstance(records, NoneType):
            # Model.field: the field (Odoo 19 declares ``records: BaseModel``, not None).
            arg_types = [records, *signature.arg_types[1:]] if signature.arg_types else []
            return signature.copy_modified(arg_types=arg_types, ret_type=descriptor)
        args = map_instance_to_supertype(descriptor, base).args
        comodel = get_proper_type(args[0]) if args else None  # Odoo 19: not generic
        if not isinstance(comodel, Instance) and self._profile.untyped_fields:
            if not isinstance(records, Instance):
                return signature
            return self._indexed_comodel_get(records, signature, ctx)  # Odoo 17
        if not isinstance(comodel, Instance):
            return signature
        owner = records.type if isinstance(records, Instance) else None
        resolved = self._comodel_at_use_site(comodel, owner, ctx.api)
        return signature.copy_modified(ret_type=resolved or comodel)

    def _value_get(
        self, descriptor: Instance, signature: CallableType, ctx: MethodSigContext
    ) -> FunctionLike:
        """Type reading the fields that Odoo leaves untyped or types too loosely.

        ``Binary`` / ``Image`` (``Field`` without a value type, so ``Any``) read as
        ``bytes | Literal[False]``; ``Reference`` reads as a record of one of the models
        of a literal ``selection`` (``BaseModel`` otherwise), not as Selection's ``str``;
        with ``literal_selection``, a ``Selection`` with literal keys reads as
        ``Literal[<keys>] | Literal[False]``.
        """
        kind = next(
            (
                item.name
                for item in descriptor.type.mro
                if item.name in VALUE_KINDS and self._profile.is_field_class(item.fullname)
            ),
            None,
        )
        records = _record_type(ctx)
        if not isinstance(records, Instance):
            return signature
        checker = _checker(ctx.api)
        if kind is None or (kind == "Selection" and not self._settings.literal_selection):
            if self._profile.untyped_fields:  # Odoo 17
                return _class_value_get(descriptor, signature, self._profile, checker)
            return signature
        false = LiteralType(False, checker.named_type("builtins.bool"))
        info = self._read_field(records, ctx)
        if kind == "Binary":
            if not isinstance(get_proper_type(signature.ret_type), AnyType):
                return signature  # Odoo 20 types it (BinaryValue)
            if info is None or not stored(info):
                return signature  # a computed Binary holds any value (widget data)
            value: Type = make_simplified_union([checker.named_type("builtins.bytes"), false])
            return signature.copy_modified(ret_type=value)
        if kind == "Reference":
            models = self._reference_types(info, records, ctx.api)
            return signature.copy_modified(ret_type=make_simplified_union([*models, false]))
        if info is None or not info.selection or "selection" in info.dynamic:
            if self._profile.untyped_fields:
                return _class_value_get(descriptor, signature, self._profile, checker)
            return signature
        keys = [LiteralType(key, checker.named_type("builtins.str")) for key in info.selection]
        return signature.copy_modified(ret_type=make_simplified_union([*keys, false]))

    def _indexed_comodel_get(
        self, records: Instance, signature: CallableType, ctx: MethodSigContext
    ) -> FunctionLike:
        """Odoo 17: a relational field reads as its comodel's records, from the field index."""
        info = self._read_field(records, ctx)
        addon_name = self.registry.addon_name_for_path(ctx.api.path)
        model = self._model_of(records.type)
        if info is None or not addon_name or model is None:
            return signature
        comodel = info.comodel if "comodel" not in info.dynamic else None
        if comodel is None and info.related:  # fields.Many2one(related="a.b")
            fields_of = partial(self.registry.fields, addon_name=addon_name)
            target = resolve_path(info.related, model, fields_of, "").field
            comodel = target.comodel if target is not None else None
        if comodel is None:
            return signature
        view = self._view_type(comodel, addon_name, ctx.api)
        return signature.copy_modified(ret_type=view) if isinstance(view, Instance) else signature

    def _read_field(self, records: Instance, ctx: MethodSigContext) -> FieldInfo | None:
        """The field read by ``record.name`` (``ctx.context``), as the reading addon sees it."""
        member = ctx.context
        model = self._model_of(records.type)
        addon_name = self.registry.addon_name_for_path(ctx.api.path)
        if not isinstance(member, MemberExpr) or model is None or addon_name is None:
            return None
        return self.registry.fields(model, addon_name).get(member.name)

    def _reference_types(
        self, info: FieldInfo | None, records: Instance, api: CheckerPluginInterface
    ) -> list[Type]:
        """The record types a ``Reference`` field holds: its selection's models."""
        found_base = next(
            (item for item in records.type.mro if item.fullname == self._profile.base_model), None
        )
        base: Type = (
            instance_of(found_base) if found_base is not None else AnyType(TypeOfAny.special_form)
        )
        addon_name = self.registry.addon_name_for_path(api.path)
        if info is None or not info.selection or "selection" in info.dynamic or not addon_name:
            return [base]
        found: list[Type] = []
        for model in info.selection:
            view = self._view_type(model, addon_name, api)
            if not isinstance(view, Instance):
                return [base]
            found.append(view)
        return found

    def _comodel_at_use_site(
        self, comodel: Instance, owner: TypeInfo | None, api: CheckerPluginInterface
    ) -> Instance | None:
        """Resolve a field's comodel from the addon that reads the field.

        The reading addon's view is used if mypy loads all the comodel's classes in it
        with the reading module (:meth:`Registry.loaded_with`); otherwise the view of
        the record's own class, if they are loaded with that class's module.
        """
        model = self._model_of(comodel.type)
        if model is None:
            return None
        module = _checker(api).tree.fullname
        views: list[tuple[str | None, str, str | None]] = [
            (self.registry.addon_name_for_path(api.path), module, api.path)
        ]
        if owner is not None:
            views.append((self._addon_of_class(owner), owner.module_name, None))
        for addon_name, host, path in views:
            if addon_name is not None and self.registry.loaded_with(model, addon_name, host, path):
                resolved = self._view_type(model, addon_name, api)
                return resolved if isinstance(resolved, Instance) else None
        return None

    def _addon_of_class(self, info: TypeInfo) -> str | None:
        """The addon whose view a model class has (its own, or a merged class's)."""
        origin = view_class_origin(info)
        if origin is not None:
            return origin[1]
        contribution = self.registry.contribution(info.fullname)
        return contribution.addon if contribution else None

    def _model_of(self, info: TypeInfo) -> str | None:
        origin = view_class_origin(info)
        if origin is not None:
            return origin[0]
        contribution = self.registry.contribution(info.fullname)
        return contribution.model if contribution else None


ViewType = Instance | AnyType | None


def _settings_or_exit(options: Options) -> Settings:
    """Read the settings; on a configuration error print it and exit with status 2.

    mypy reports any exception raised while it creates a plugin as an internal error
    of its own and hides the message; ``SystemExit`` passes through.
    """
    try:
        return load_settings(options.config_file, options.mypy_path)
    except ConfigError as exc:
        print(f"odoo-typing: error: {exc}", file=sys.stderr)
        raise SystemExit(2) from None


def _stale_overlay_warning(settings: Settings, registry: Registry) -> str | None:
    """A warning when the overlay root no longer links the addons on the addons paths."""
    if settings.overlay_root is None:
        return None
    differences = overlay_differences(registry.addons, settings.overlay_root)
    if not differences:
        return None
    shown = "; ".join(differences[:3]) + ("; ..." if len(differences) > 3 else "")
    return (
        f"odoo-typing: warning: the overlay root {settings.overlay_root} is out of date "
        f"({shown}); run odoo-typing-root"
    )


def _package_bases_warning(
    profile: OdooProfile, settings: Settings, options: Options
) -> str | None:
    """Warning text if overlay files would not get their ``odoo.addons.<name>`` names.

    In Odoo 19+ ``odoo/addons`` has no ``__init__.py``; without
    ``explicit_package_bases`` mypy names a file of the overlay root after its
    directory alone (``my_addon.models``), so every module is found twice.
    """
    if not profile.namespace_odoo or settings.overlay_root is None:
        return None
    if options.explicit_package_bases:
        return None
    return (
        f"odoo-typing: warning: Odoo {profile.version} has no odoo/addons/__init__.py: set "
        "explicit_package_bases = true in [tool.mypy], or mypy does not name the files "
        'of overlay_root "odoo.addons.<name>"'
    )


def _python_version_warning_or_exit(
    profile: OdooProfile, options: Options, interpreter: tuple[int, int]
) -> str | None:
    """Exit if mypy runs on a Python too old for the Odoo sources; warn if it targets one.

    Odoo 20 declares ``class Field[T]`` (PEP 695). An older interpreter cannot parse it
    at all (a blocking syntax error in Odoo). An older ``python_version`` is fine for the
    silenced Odoo modules (mypy parses them for the interpreter), but each checked file
    with the syntax, Odoo's own addons too, gets a ``[syntax]`` error.

    Args:
        profile: Profile of the configured Odoo version.
        options: mypy options (``python_version`` is the target).
        interpreter: Version of the Python that runs mypy.

    Returns:
        The warning text, or ``None`` when the target is new enough.
    """
    needed = ".".join(map(str, profile.min_python))
    if interpreter < profile.min_python:
        running = ".".join(map(str, interpreter))
        print(
            f"odoo-typing: error: Odoo {profile.version} needs Python {needed} or newer "
            f"to parse; mypy runs on Python {running}",
            file=sys.stderr,
        )
        raise SystemExit(2)
    if options.python_version >= profile.min_python:
        return None
    target = ".".join(map(str, options.python_version))
    return (
        f"odoo-typing: warning: Odoo {profile.version} uses Python {needed} syntax: set "
        f'python_version = "{needed}" in [tool.mypy] (mypy targets {target})'
    )


def _profile_or_exit(settings: Settings) -> OdooProfile:
    """The profile of the configured Odoo version; exit with status 2 if there is none."""
    version = settings.odoo_version
    if version is None:
        message = f"set odoo_version (one of {', '.join(map(str, SUPPORTED_VERSIONS))}) in {WHERE}"
    elif (profile := profile_for(version)) is not None:
        return profile
    else:
        message = f"Odoo {version} is not supported yet (see ROADMAP.md, Odoo versions)"
    print(f"odoo-typing: error: {message}", file=sys.stderr)
    raise SystemExit(2)


Incomplete = Callable[[str], bool]


@dataclass(frozen=True, slots=True)
class _Found:
    """Classes looked up in mypy's modules.

    Attributes:
        infos: The classes found, in the order asked.
        pending: Some class's module is part of this run but the class is not
            analyzed yet.
        missing: Some class was not found (pending, or its module is not loaded).
    """

    infos: list[TypeInfo]
    pending: bool
    missing: bool


def _class_infos(
    contributions: Iterable[Contribution],
    modules: dict[str, MypyFile],
    incomplete: Incomplete = lambda module: False,
) -> _Found:
    """Find the mypy classes of contributions (top-level classes of their modules).

    A class absent from a module that is completely analyzed does not exist for mypy
    (an unreachable ``if sys.version_info < ...`` branch): it is neither pending nor
    missing. ``incomplete`` tells which modules are still being analyzed (none, once
    type checking runs). A class whose definition mypy has not finished (a placeholder
    in its module's names: a deferred class of another module of the same import
    cycle, as Odoo 17's ``base.models.res_partner`` for ``base.populate``) is pending
    even when its module no longer counts as incomplete.
    """
    infos: list[TypeInfo] = []
    pending = missing = False
    for item in contributions:
        tree = modules.get(item.module)
        node = tree.names.get(item.class_name) if tree is not None else None
        if node is not None and isinstance(node.node, TypeInfo):
            infos.append(node.node)
        elif tree is None:
            missing = True
        elif incomplete(item.module) or (
            node is not None and isinstance(node.node, PlaceholderNode)
        ):
            pending = missing = True
    return _Found(infos=infos, pending=pending, missing=missing)


def _incomplete(ctx: ClassDefContext) -> Incomplete:
    """Which modules semantic analysis has not finished yet (all, if unknown)."""
    if isinstance(ctx.api, SemanticAnalyzer):
        return ctx.api.is_incomplete_namespace
    return lambda module: True


def _imported_view_class(
    name: str, checker: TypeChecker, in_addon: Callable[[str], object]
) -> TypeInfo | None:
    """A merged view class of the module being checked or of an addon module it imports.

    One class per model and addon would be best, but a class stored in a module that is
    not loaded with the user could not be read back from the cache. A module that the
    user imports from is always loaded with it, and values (``RECS = env["x"]``) can
    only reach the user through such imports; so the class of such a module is reused.
    """
    seen = {checker.tree.fullname}
    stack = [checker.tree]
    while stack:
        tree = stack.pop()
        node = tree.names.get(name)
        if node is not None and isinstance(node.node, TypeInfo):
            return node.node
        for module in _referenced_modules(tree, checker.modules) - seen:
            seen.add(module)
            if in_addon(module):
                stack.append(checker.modules[module])
    return None


def _referenced_modules(tree: MypyFile, modules: dict[str, MypyFile]) -> set[str]:
    """Modules whose symbols a module's symbol table refers to (its imports, also cached)."""
    found: set[str] = set()
    for entry in tree.names.values():
        node = entry.node
        if isinstance(node, MypyFile):
            found.add(node.fullname)
        elif node is not None:
            found.add(node.fullname.rpartition(".")[0])
    found.discard(tree.fullname)
    return {module for module in found if module in modules}


def _lookup(modules: dict[str, MypyFile], fullname: str) -> TypeInfo | None:
    node = lookup_fully_qualified(fullname, modules)
    return node.node if node is not None and isinstance(node.node, TypeInfo) else None


def _semanal_lookup(ctx: ClassDefContext) -> Lookup:
    return partial(_lookup, ctx.api.modules)


def _checker(api: CheckerPluginInterface) -> TypeChecker:
    """Return mypy's checker behind the plugin interface.

    The interface does not expose the module being checked, which merged view
    classes must be stored in (see :mod:`odoo_typing.classes`); mypy's checker does.
    """
    if not isinstance(api, TypeChecker):
        raise TypeError(f"unexpected checker plugin interface: {type(api).__name__}")
    return api


def _record_type(ctx: MethodSigContext) -> ProperType | None:
    """Type of the records a descriptor is read on (``NoneType`` for ``Model.field``)."""
    if not ctx.args or not ctx.args[0]:
        return None
    return get_proper_type(ctx.api.get_expression_type(ctx.args[0][0]))


def _conflict_message(class_name: str) -> str:
    return (
        f'Odoo model class "{class_name}" has no consistent method resolution order '
        "(Odoo cannot load it either); its bases are kept, some out of order"
    )


def _log_access_types(ctx: ClassDefContext, profile: OdooProfile) -> dict[str, Instance]:
    """Descriptor types of the log-access fields (``odoo.fields`` is analyzed first)."""
    many2one = ctx.api.lookup_fully_qualified_or_none(profile.many2one)
    datetime = ctx.api.lookup_fully_qualified_or_none(profile.datetime)
    if not (
        many2one
        and isinstance(many2one.node, TypeInfo)
        and datetime
        and isinstance(datetime.node, TypeInfo)
    ):
        return {}
    user = Instance(many2one.node, [AnyType(TypeOfAny.special_form)])
    date = Instance(datetime.node, [])
    return {"create_uid": user, "create_date": date, "write_uid": user, "write_date": date}


def _normalize_meta_attributes(ctx: ClassDefContext, profile: OdooProfile) -> None:
    """Relax the private class attributes of model classes (``_inherit``, ``_order``...).

    Odoo classes override such configuration attributes freely: ``_inherit = "x"``
    where a base used a list, ``_sequence_index = "journal_id"`` where
    ``sequence.mixin`` has ``False``, or a property where a mixin has a plain string.
    Once ``_inherit`` parents are real bases, mypy would report each of these. So a
    private attribute assigned without annotation gets the type a model base class
    declares for it explicitly (``BaseModel._inherit: str | list[str] | ...``), and
    ``Any`` otherwise. ``_name`` is a ``str`` in every model class. Private fields
    (``_x = fields.Char()``) keep their type unless ``BaseModel`` declares the name.
    """
    info = ctx.cls.info
    if info.fullname == profile.base_model or not info.has_base(profile.base_model):
        return
    if profile.untyped_fields:
        _relax_untyped_base_attributes(info, profile)
        _declare_untyped_attributes(ctx, profile)
    for statement in ctx.cls.defs.body:
        target = _private_attribute(statement)
        if target is None or not isinstance(statement, AssignmentStmt):
            continue
        declared = _meta_attribute_type(info, target.name, statement.rvalue, profile)
        if target.name == "_name":
            # Only BaseModel itself has ``_name = None``; in a model class the name is
            # a str, and ``str | None`` would reject ``_inherit = _name``. Every class
            # gets the same narrowed type, so the classes stay compatible.
            declared = ctx.api.named_type("builtins.str")
        if declared is None:
            continue
        # Pretend the user wrote ``_inherit: <declared type> = ...``; otherwise the
        # checker re-infers the type from the right-hand side.
        statement.type = declared
        target.is_inferred_def = False
        if isinstance(target.node, Var):
            target.node.type = declared
            target.node.is_inferred = False


def _relax_untyped_base_attributes(info: TypeInfo, profile: OdooProfile) -> None:
    """Odoo 17: make the unannotated private attributes of Odoo's model classes ``Any``.

    ``BaseModel._name = None`` is inferred as ``None`` there (18+ declare
    ``_name: str | None``), so every ``_name = "x"`` would be an incompatible override.
    """
    module = profile.base_model.rpartition(".")[0]
    for base in info.mro[1:]:
        if base.module_name != module:
            continue
        for name, node in base.names.items():
            var = node.node
            private = name.startswith("_") and not name.startswith("__")
            if private and isinstance(var, Var) and var.is_inferred:
                var.type = AnyType(TypeOfAny.special_form)


def _declare_untyped_attributes(ctx: ClassDefContext, profile: OdooProfile) -> None:
    """Odoo 17: declare what Odoo 18 annotates and 17 only sets at run time.

    ``BaseModel.id`` / ``display_name`` / ``_fields`` (added by the metaclass) and the
    attributes ``Environment.__new__`` assigns (``cr``, ``uid``, ``context``...), with
    Odoo 18's types where they are simple, else ``Any``.
    """
    api = ctx.api
    false = LiteralType(False, api.named_type("builtins.bool"))
    text = api.named_type("builtins.str")
    anything = AnyType(TypeOfAny.special_form)
    new_id = api.lookup_fully_qualified_or_none(profile.new_id)
    if new_id is None or not isinstance(new_id.node, TypeInfo):
        return
    record_id = UnionType([api.named_type("builtins.int"), instance_of(new_id.node), false])
    declarations: dict[str, dict[str, Type]] = {
        profile.base_model: {
            "id": record_id,
            "display_name": UnionType([text, false]),
            "_fields": api.named_type("builtins.dict", [text, anything]),
        },
        profile.environment: {
            "cr": anything,
            "uid": api.named_type("builtins.int"),
            "context": api.named_type("builtins.dict", [text, anything]),
            "su": api.named_type("builtins.bool"),
            "registry": anything,
            "cache": anything,
            "transaction": anything,
        },
    }
    for owner, attributes in declarations.items():
        node = api.lookup_fully_qualified_or_none(owner)
        if node is None or not isinstance(node.node, TypeInfo):
            continue
        for name, value in attributes.items():
            if name in node.node.names:
                continue
            var = Var(name, value)
            var.info = node.node
            var._fullname = f"{owner}.{name}"
            node.node.names[name] = SymbolTableNode(MDEF, var, plugin_generated=True)


def _private_attribute(statement: object) -> NameExpr | None:
    """The target of ``_x = value`` (single name, no annotation, not a dunder)."""
    # ``unanalyzed_type``, not ``type``: semantic analysis already sets ``type`` for
    # literals (``_x = False`` is ``bool``) where no base class defines the name.
    if not isinstance(statement, AssignmentStmt) or statement.unanalyzed_type is not None:
        return None
    if len(statement.lvalues) != 1 or not isinstance(statement.lvalues[0], NameExpr):
        return None
    target = statement.lvalues[0]
    private = target.name.startswith("_") and not target.name.startswith("__")
    return target if private else None


def _meta_attribute_type(
    info: TypeInfo, name: str, value: Expression, profile: OdooProfile
) -> Type | None:
    """The type a private class attribute gets (see _normalize_meta_attributes).

    Returns:
        ``None`` to leave the attribute alone: a base defines it as a method or
        property, or the value is a field that no ``BaseModel`` declaration covers.
    """
    for base in info.mro[1:]:
        node = base.names.get(name)
        if node is None:
            continue
        if not isinstance(node.node, Var):
            return None
        if node.node.type is not None and not node.node.is_inferred:
            return node.node.type  # annotated (or normalized) on a base class
        if base.fullname == profile.base_model:
            return AnyType(TypeOfAny.special_form)
        break
    return None if _is_field_definition(value, profile) else AnyType(TypeOfAny.special_form)


def _is_field_definition(value: Expression, profile: OdooProfile) -> bool:
    """``fields.Char(...)`` and the like (f-strings are calls too, in mypy's tree)."""
    callee = value.callee if isinstance(value, CallExpr) else None
    return isinstance(callee, RefExpr) and profile.is_field_class(callee.fullname)


def _inherit_value(cls: ClassDef) -> Expression | None:
    """The right-hand side of ``_inherit = ...`` in a class body, if there is one."""
    for statement in cls.defs.body:
        if isinstance(statement, AssignmentStmt) and any(
            isinstance(target, NameExpr) and target.name == "_inherit"
            for target in statement.lvalues
        ):
            return statement.rvalue
    return None


def _missing_parent_message(problem: MissingParent, addon: str) -> str:
    model = problem.model
    if problem.defined_later:
        return (
            f'Odoo model "{model}" in _inherit is defined after this class; '
            "Odoo registers the parent first"
        )
    if problem.outside_addons:
        where = _outside(problem.outside_addons, addon)
        return f'Odoo model "{model}" in _inherit is defined in {where}'
    message = f'Unknown Odoo model "{model}" in _inherit'
    if problem.missing_depends:
        message += f"; dependencies not found on addons_path: {', '.join(problem.missing_depends)}"
    return message


def _outside(addons: tuple[str, ...], addon: str) -> str:
    """``addon "sale", which is not in the dependencies of "my_addon"``."""
    names = ", ".join(f'"{name}"' for name in addons)
    where = f"addon {names}, which is" if len(addons) == 1 else f"addons {names}, which are"
    return f'{where} not in the dependencies of "{addon}"'


def _model_name_argument(ctx: MethodContext) -> str | None:
    """The model name in ``env[...]``: a string literal, or a ``Final`` / ``Literal`` str."""
    if not ctx.args or len(ctx.args[0]) != 1:
        return None
    if isinstance(ctx.args[0][0], StrExpr):
        return ctx.args[0][0].value
    arg_type = get_proper_type(ctx.arg_types[0][0])
    if isinstance(arg_type, Instance) and arg_type.last_known_value is not None:
        arg_type = arg_type.last_known_value
    if isinstance(arg_type, LiteralType) and isinstance(arg_type.value, str):
        return arg_type.value
    return None


def _field_sources(
    classes: Iterable[Contribution],
    modules: dict[str, MypyFile],
    incomplete: Incomplete = lambda module: False,
) -> list[tuple[TypeInfo, tuple[str, ...]]] | None:
    """The mypy classes of contributions, each with the fields it declares.

    Returns:
        ``None`` if some class's module is part of this run but the class is not
        analyzed yet; classes of modules outside the run are left out.
    """
    sources: list[tuple[TypeInfo, tuple[str, ...]]] = []
    for item in classes:
        found = _class_infos([item], modules, incomplete)
        if found.pending:
            return None
        sources += [(info, item.field_names) for info in found.infos]
    return sources


def _field_arguments(call: CallExpr, field_class: str) -> list[tuple[str, Expression]]:
    """The arguments of a field definition by parameter name (positional ones too)."""
    positional = POSITIONAL_PARAMETERS.get(field_class, ("string",))
    found: list[tuple[str, Expression]] = []
    position = 0
    for name, value, kind in zip(call.arg_names, call.args, call.arg_kinds, strict=True):
        if name is not None:
            found.append((name, value))
        elif kind == ARG_POS:
            if position < len(positional):
                found.append((positional[position], value))
            position += 1
    return found


# Odoo 17: what reading a field gives, by field class (as Odoo 18 declares them).
CLASS_VALUES: dict[str, tuple[str | bool, ...]] = {
    "Boolean": ("builtins.bool",),
    "Integer": ("builtins.int",),
    "Float": ("builtins.float",),
    "Monetary": ("builtins.float",),
    "_String": ("builtins.str", False),
    "Selection": ("builtins.str", False),
    "Date": ("datetime.date", False),
    "Datetime": ("datetime.datetime", False),
    "Id": ("builtins.int", "new_id", False),
}


def _class_value_get(
    descriptor: Instance, signature: CallableType, profile: OdooProfile, checker: TypeChecker
) -> FunctionLike:
    """The value type of a field class Odoo does not type (17), by the first known class."""
    if not isinstance(get_proper_type(signature.ret_type), AnyType):
        return signature
    found = next(
        (CLASS_VALUES[item.name] for item in descriptor.type.mro if item.name in CLASS_VALUES), None
    )
    if found is None:
        return signature
    items: list[Type] = []
    for part in found:
        if not isinstance(part, str):  # False
            items.append(LiteralType(False, checker.named_type("builtins.bool")))
            continue
        node = lookup_fully_qualified(profile.new_id if part == "new_id" else part, checker.modules)
        if node is None or not isinstance(node.node, TypeInfo):
            return signature
        items.append(instance_of(node.node))
    value = items[0] if len(items) == 1 else UnionType(items)
    return signature.copy_modified(ret_type=value)


def _type_request(profile: OdooProfile, modules: dict[str, MypyFile]) -> bool:
    """Type ``odoo.http.request`` as a ``Request`` where Odoo leaves it ``Any``.

    Odoo 18-19 build it with werkzeug's ``LocalStack()()``, which mypy cannot follow
    (werkzeug is not on the search path), so ``request.env[...]`` in a controller went
    unchecked. It runs from the class hook of a later module, once ``odoo.http`` is
    type-checked (its variable has a type then).

    Returns:
        Whether nothing is left to do (typed now, or typed by Odoo).
    """
    if profile.request_var is None:
        return True
    if "odoo.http" not in modules:
        return False
    variable = lookup_fully_qualified(profile.request_var, modules)
    request = lookup_fully_qualified(profile.request_class, modules)
    if (
        variable is None
        or request is None
        or not isinstance(variable.node, Var)
        or not isinstance(request.node, TypeInfo)
        or variable.node.type is None
    ):
        return False  # odoo.http is not analyzed (or type-checked) yet: a later class
    if isinstance(get_proper_type(variable.node.type), AnyType):
        variable.node.type = instance_of(request.node)
    return True


def _request_env(environment: str, ctx: AttributeContext) -> Type:
    """``request.env`` / ``self.env`` (Odoo 17): an ``Environment``.

    Odoo sets ``request.env`` to ``None`` before dispatch, and Odoo 17 declares
    ``BaseModel.env`` as an unannotated property.
    """
    if not isinstance(ctx.api, TypeChecker):
        return ctx.default_attr_type
    node = lookup_fully_qualified(environment, ctx.api.modules)
    if node is None or not isinstance(node.node, TypeInfo):
        return ctx.default_attr_type
    return instance_of(node.node)


def _argument(ctx: MethodContext, name: str, position: int) -> Expression | None:
    """The expression passed for a parameter, by name, else by position (``-1``: none)."""
    if name in ctx.callee_arg_names:
        index = ctx.callee_arg_names.index(name)
    elif position >= 0:
        index = position
    else:
        return None
    return ctx.args[index][0] if len(ctx.args) > index and ctx.args[index] else None


def _is_false(node: Expression | None) -> bool:
    return isinstance(node, NameExpr) and node.name == "False"


def _ids_only(load: Expression | None) -> bool | None:
    """Whether ``read(load=...)`` gives ids only; ``None`` if ``load`` is not literal.

    Odoo reads display names only for ``load="_classic_read"`` (the default).
    """
    if load is None:
        return False
    if isinstance(load, StrExpr):
        return load.value != "_classic_read"
    if isinstance(load, NameExpr) and load.name in {"None", "False"}:
        return True
    return None


def _literal_strings_argument(ctx: MethodContext, name: str, position: int) -> list[StrExpr] | None:
    """A literal list (or tuple) of strings passed for a parameter, if one is."""
    value = _argument(ctx, name, position)
    if not isinstance(value, (ListExpr, TupleExpr)) or not value.items:
        return None
    items = [item for item in value.items if isinstance(item, StrExpr)]
    return items if len(items) == len(value.items) else None


def _read_value(info: FieldInfo, ids_only: bool, checker: TypeChecker) -> Type | None:
    """The type ``read()`` gives a field's value, if it differs from reading the field.

    ``Many2one`` is ``(id, display_name)`` (an id with ``load=None``), x2many fields a list
    of ids, ``Reference`` a ``"model,id"`` string; ``Json`` / ``Properties`` / custom
    classes are ``Any``; ``None`` means the type of ``record.field``.
    """
    false = LiteralType(False, checker.named_type("builtins.bool"))
    integer = checker.named_type("builtins.int")
    if info.field_type == "Many2one":
        if ids_only:
            return UnionType([integer, false])  # simplifying would absorb False in int
        pair = TupleType(
            [integer, checker.named_type("builtins.str")], checker.named_type("builtins.tuple")
        )
        return make_simplified_union([pair, false])
    if info.field_type in {"One2many", "Many2many"}:
        return checker.named_generic_type("builtins.list", [integer])
    if info.field_type == "Reference":
        return make_simplified_union([checker.named_type("builtins.str"), false])
    if info.field_type in {"Json", "Properties", "PropertiesDefinition"}:
        return AnyType(TypeOfAny.special_form)
    if info.field_type not in KNOWN_CLASSES:
        return AnyType(TypeOfAny.special_form)
    return None


def _comodel_expression(ctx: FunctionContext) -> StrExpr | None:
    if "comodel_name" not in ctx.callee_arg_names:
        return None
    actuals = ctx.args[ctx.callee_arg_names.index("comodel_name")]
    if len(actuals) == 1 and isinstance(actuals[0], StrExpr):
        return actuals[0]
    return None


def plugin(version: str) -> type[OdooPlugin]:
    """Return the plugin class; called by mypy.

    Args:
        version: mypy version string.

    Returns:
        The plugin class.
    """
    return OdooPlugin
