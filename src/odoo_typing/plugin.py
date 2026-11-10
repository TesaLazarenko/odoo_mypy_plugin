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
    AssignmentStmt,
    CallExpr,
    ClassDef,
    Expression,
    MypyFile,
    NameExpr,
    RefExpr,
    StrExpr,
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
    NoneType,
    ProperType,
    Type,
    TypeOfAny,
    get_proper_type,
)

# isort: split
# After mypy.types: these modules import each other at import time.
from mypy.checker import TypeChecker
from mypy.maptype import map_instance_to_supertype

from odoo_typing.classes import (
    Lookup,
    create_view_class,
    install_mro,
    instance_of,
    merge_mro,
    record_python_mro,
    view_class_origin,
)
from odoo_typing.config import ConfigError, Settings, load_settings
from odoo_typing.fingerprint import Fingerprints
from odoo_typing.records import RecordStore, load_index
from odoo_typing.registry import MissingParent, Registry
from odoo_typing.scanner import Contribution

__all__ = ["ODOO_MODEL", "OdooPlugin", "plugin"]

ODOO_MODEL = ErrorCode("odoo-model", "Check that Odoo model names exist", "Odoo")

ENV_GETITEM = "odoo.api.Environment.__getitem__"
RELATIONAL_CLASSES = frozenset(
    {"odoo.fields.Many2one", "odoo.fields.One2many", "odoo.fields.Many2many"}
)
RELATIONAL_BASE = "odoo.fields._Relational"
FIELDS_MODULE = "odoo.fields."
BASE_MODEL = "odoo.models.BaseModel"
DEPENDENCY_PRIORITY = 10  # mypy.build.PRI_MED


class OdooPlugin(Plugin):
    """Teach mypy how the Odoo registry assembles models."""

    def __init__(self, options: Options) -> None:
        """Load settings and prepare the (lazy) model registry.

        A configuration problem ends mypy with the message and exit status 2, like
        mypy's own configuration errors (not as a crash of the plugin).
        """
        super().__init__(options)
        self._settings = _settings_or_exit(options)
        self._store = RecordStore.for_cache_dir(options.cache_dir)
        self._inputs_checked = False
        self._new_registry()

    def _new_registry(self) -> None:
        index = partial(load_index, self._settings, self._store)
        self.registry = Registry(self._settings, index)
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
        if self.registry.inputs_changed():
            self._new_registry()

    # -- dependencies ----------------------------------------------------------------------

    def get_additional_deps(self, file: MypyFile) -> list[tuple[int, str, int]]:
        """Make mypy load the modules that define the models a file relies on.

        Without these edges mypy may type-check a file before the model classes it
        refers to (``_inherit``, ``env[...]``, comodels) are analyzed, or not load them
        at all. Files outside any addon get none, since nothing is resolved in them.
        """
        self._refresh_registry()
        modules = set(self.registry.dependency_modules(file.fullname, file.path))
        modules.discard(file.fullname)
        return [(DEPENDENCY_PRIORITY, module, -1) for module in sorted(modules)]

    # -- semantic analysis: _inherit -------------------------------------------------------

    def get_customize_class_mro_hook(
        self, fullname: str
    ) -> Callable[[ClassDefContext], None] | None:
        """Give Odoo model classes the MRO of their registry class."""
        contribution = self.registry.contribution(fullname)
        if contribution is None:
            return None
        return partial(self._merge_bases, contribution)

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
        chain = _class_infos(self.registry.linearization(contribution)[1:], ctx.api.modules)
        if chain.pending and not ctx.api.final_iteration:
            ctx.api.defer()
            return
        merge = merge_mro(info, python_mro, chain.infos, lookup)
        bases = _class_infos(self.registry.bases(contribution), ctx.api.modules).infos
        own = [instance_of(base) for base in bases]
        written = [base for base in info.bases if base.type not in bases]
        install_mro(info, merge.order, [*own, *written], partial=chain.missing)
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
            ctx.api.fail(message, anchor, code=ODOO_MODEL)
        cycle = self.registry.inherit_cycle(contribution)
        own = [item for item in cycle if item.addon == contribution.addon]
        if own and own[0] == contribution:  # once per cycle and addon, at its first class
            names = ", ".join(f'"{item.class_name}"' for item in cycle)
            message = f"_inherit cycle: classes {names} inherit from each other"
            ctx.api.fail(message, ctx.cls, code=ODOO_MODEL)

    def get_base_class_hook(self, fullname: str) -> Callable[[ClassDefContext], None] | None:
        """Relax the types of model meta attributes such as ``_inherit``."""
        return _normalize_meta_attributes

    # -- type checking: env[...] and relational fields -------------------------------------

    def get_method_hook(self, fullname: str) -> Callable[[MethodContext], Type] | None:
        """Type ``env["model.name"]``."""
        return self._env_getitem if fullname == ENV_GETITEM else None

    def get_function_hook(self, fullname: str) -> Callable[[FunctionContext], Type] | None:
        """Bind relational field constructors to their comodel."""
        return self._relational_field if fullname in RELATIONAL_CLASSES else None

    def get_method_signature_hook(
        self, fullname: str
    ) -> Callable[[MethodSigContext], FunctionLike] | None:
        """Make reading a relational field return its comodel recordset."""
        # mypy names the hook after the descriptor's own class (``odoo.fields.Many2one``,
        # a custom subclass...), so match every ``__get__`` and check the MRO in the hook.
        return self._relational_get if fullname.endswith(".__get__") else None

    def get_attribute_hook(self, fullname: str) -> Callable[[AttributeContext], Type] | None:
        """Resolve record-typed attributes of model classes from the reading addon."""
        owner = fullname.rpartition(".")[0]
        return self._record_attribute if self.registry.contribution(owner) else None

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
        bases = _class_infos(view.bases, checker.modules).infos
        lookup = partial(_lookup, checker.modules)
        merged = create_view_class(
            checker.tree, model, addon_name, chain.infos, bases, lookup, chain.missing
        )
        return instance_of(merged)

    def _env_getitem(self, ctx: MethodContext) -> Type:
        if not ctx.args or len(ctx.args[0]) != 1 or not isinstance(ctx.args[0][0], StrExpr):
            return ctx.default_return_type
        addon_name = self.registry.addon_name_for_path(ctx.api.path)
        if addon_name is None:
            return ctx.default_return_type  # outside any addon: see the module docstring
        model = ctx.args[0][0].value
        if self.registry.view_class(model, addon_name) is None:
            ctx.api.fail(f'Unknown Odoo model "{model}"', ctx.context, code=ODOO_MODEL)
            return ctx.default_return_type
        module = _checker(ctx.api).tree.fullname
        if not self.registry.loaded_with(model, addon_name, module, ctx.api.path):
            # ``env2 = self.env; env2["x"]``: the scan saw no model name, so no edge
            # makes mypy load the model's classes with this module.
            return AnyType(TypeOfAny.special_form)
        return self._view_type(model, addon_name, ctx.api) or ctx.default_return_type

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
        if comodel is None or addon_name is None:
            return ctx.default_return_type
        definition = self.registry.definition(comodel.value, addon_name)
        if definition is None:
            ctx.api.fail(f'Unknown Odoo model "{comodel.value}"', comodel, code=ODOO_MODEL)
            return default.copy_modified(args=[AnyType(TypeOfAny.from_error)])
        found = _class_infos([definition], _checker(ctx.api).modules).infos
        model_type = instance_of(found[0]) if found else AnyType(TypeOfAny.special_form)
        return default.copy_modified(args=[model_type])

    def _relational_get(self, ctx: MethodSigContext) -> FunctionLike:
        descriptor = get_proper_type(ctx.type)
        signature = ctx.default_signature
        if not isinstance(descriptor, Instance) or not isinstance(signature, CallableType):
            return signature
        mro = descriptor.type.mro
        base = next((item for item in mro if item.fullname == RELATIONAL_BASE), None)
        if base is None:
            return signature
        records = _record_type(ctx)
        if isinstance(records, NoneType):
            return signature.copy_modified(ret_type=descriptor)  # Model.field: the field
        comodel = get_proper_type(map_instance_to_supertype(descriptor, base).args[0])
        if not isinstance(comodel, Instance):
            return signature
        owner = records.type if isinstance(records, Instance) else None
        resolved = self._comodel_at_use_site(comodel, owner, ctx.api)
        return signature.copy_modified(ret_type=resolved or comodel)

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


def _class_infos(contributions: Iterable[Contribution], modules: dict[str, MypyFile]) -> _Found:
    """Find the mypy classes of contributions (top-level classes of their modules)."""
    infos: list[TypeInfo] = []
    pending = missing = False
    for item in contributions:
        tree = modules.get(item.module)
        node = tree.names.get(item.class_name) if tree is not None else None
        if node is not None and isinstance(node.node, TypeInfo):
            infos.append(node.node)
        else:
            missing = True
            pending = pending or tree is not None
    return _Found(infos=infos, pending=pending, missing=missing)


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


def _normalize_meta_attributes(ctx: ClassDefContext) -> None:
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
    if info.fullname == BASE_MODEL or not info.has_base(BASE_MODEL):
        return
    for statement in ctx.cls.defs.body:
        target = _private_attribute(statement)
        if target is None or not isinstance(statement, AssignmentStmt):
            continue
        declared = _meta_attribute_type(info, target.name, statement.rvalue)
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


def _meta_attribute_type(info: TypeInfo, name: str, value: Expression) -> Type | None:
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
        if base.fullname == BASE_MODEL:
            return AnyType(TypeOfAny.special_form)
        break
    return None if _is_field_definition(value) else AnyType(TypeOfAny.special_form)


def _is_field_definition(value: Expression) -> bool:
    """``fields.Char(...)`` and the like (f-strings are calls too, in mypy's tree)."""
    callee = value.callee if isinstance(value, CallExpr) else None
    return isinstance(callee, RefExpr) and callee.fullname.startswith(FIELDS_MODULE)


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
        names = ", ".join(f'"{name}"' for name in problem.outside_addons)
        where = (
            f"addon {names}, which is"
            if len(problem.outside_addons) == 1
            else (f"addons {names}, which are")
        )
        return (
            f'Odoo model "{model}" in _inherit is defined in {where} '
            f'not in the dependencies of "{addon}"'
        )
    message = f'Unknown Odoo model "{model}" in _inherit'
    if problem.missing_depends:
        message += f"; dependencies not found on addons_path: {', '.join(problem.missing_depends)}"
    return message


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
