"""mypy plugin entry point.

Enable it in ``pyproject.toml``::

    [tool.mypy]
    plugins = ["odoo_typing.plugin"]

What it does:

* ``_inherit``: every model class gets the earlier classes of the same model
  (and of its ``_inherit`` mixins, and of ``base``) as extra bases, so fields and
  methods defined elsewhere are visible on ``self`` and ``super()`` works. An
  ``_inherit`` that Odoo would reject at load time (unknown model, model outside
  the ``depends``, defined later, cycle) is an ``odoo-model`` error.
* ``env["model.name"]`` evaluates to that model's class; unknown names are errors.
* ``fields.Many2one("x")`` / ``One2many`` / ``Many2many`` are bound to the
  comodel class, so reading the field gives a typed recordset.

Model names are resolved from the point of view of the addon that contains the
file. Files outside any addon (scripts, and Odoo's own ``odoo/*.py`` modules,
which mypy always analyzes) have no such view: the plugin leaves them alone, so
``env[...]`` keeps the declared ``BaseModel`` type and names are not checked
there. Resolving them against every installed addon would scan the whole
registry on each cold run, and could still pick an arbitrary extension class.
"""

from __future__ import annotations

from collections.abc import Callable
from functools import partial

from mypy.errorcodes import ErrorCode
from mypy.mro import MroError, calculate_mro
from mypy.nodes import (
    AssignmentStmt,
    ClassDef,
    Expression,
    MypyFile,
    NameExpr,
    StrExpr,
    TypeInfo,
    Var,
)
from mypy.options import Options
from mypy.plugin import (
    ClassDefContext,
    FunctionContext,
    MethodContext,
    MethodSigContext,
    Plugin,
)
from mypy.types import AnyType, FunctionLike, Instance, Type, TypeOfAny, get_proper_type

# isort: split
from mypy.maptype import map_instance_to_supertype  # after mypy.types: import cycle

from odoo_typing.config import load_settings
from odoo_typing.registry import MissingParent, Registry
from odoo_typing.scanner import Contribution

__all__ = ["ODOO_MODEL", "OdooPlugin", "plugin"]

ODOO_MODEL = ErrorCode("odoo-model", "Check that Odoo model names exist", "Odoo")

ENV_GETITEM = "odoo.api.Environment.__getitem__"
RELATIONAL_CLASSES = frozenset(
    {"odoo.fields.Many2one", "odoo.fields.One2many", "odoo.fields.Many2many"}
)
RELATIONAL_BASE = "odoo.fields._Relational"
BASE_MODEL = "odoo.models.BaseModel"
DEPENDENCY_PRIORITY = 10  # mypy.build.PRI_MED


class OdooPlugin(Plugin):
    """Teach mypy how the Odoo registry assembles models."""

    def __init__(self, options: Options) -> None:
        """Load settings and prepare the (lazy) model registry."""
        super().__init__(options)
        self.registry = Registry(load_settings(options.config_file))

    # -- dependencies ----------------------------------------------------------------------

    def get_additional_deps(self, file: MypyFile) -> list[tuple[int, str, int]]:
        """Make mypy load the modules that define the models a file relies on.

        Without these edges mypy may type-check a file before the model classes it
        refers to (``_inherit``, ``env[...]``, comodels) are analyzed. Files outside
        any addon get none, since nothing is resolved in them.
        """
        addon_name = self.registry.addon_name_for_path(file.path)
        if addon_name is None:
            return []
        modules: set[str] = set()
        scan = self.registry.module_scan(file.fullname)
        for contribution in scan.contributions if scan else ():
            modules.update(base.module for base in self.registry.bases(contribution))
        for model in self.registry.referenced_models(file.path):
            modules.update(tip.module for tip in self.registry.model_tips(model, addon_name))
        modules.discard(file.fullname)
        return [(DEPENDENCY_PRIORITY, module, -1) for module in sorted(modules)]

    # -- semantic analysis: _inherit -------------------------------------------------------

    def get_customize_class_mro_hook(
        self, fullname: str
    ) -> Callable[[ClassDefContext], None] | None:
        """Add the bases implied by ``_inherit`` to Odoo model classes."""
        contribution = self.registry.contribution(fullname)
        if contribution is None:
            return None
        return partial(self._merge_bases, contribution)

    def _merge_bases(self, contribution: Contribution, ctx: ClassDefContext) -> None:
        self._report_inherit_problems(contribution, ctx)
        info = ctx.cls.info
        original = list(info.bases)
        written = {base.type.fullname: base for base in original}
        merged: dict[str, Instance] = {}
        for base in self.registry.bases(contribution):
            node = ctx.api.lookup_fully_qualified_or_none(base.fullname)
            if node is not None and isinstance(node.node, TypeInfo):
                fullname = node.node.fullname
                merged[fullname] = written.get(fullname) or Instance(node.node, [])
        if merged.keys() <= written.keys():
            return
        # A Python base that is also an _inherit base keeps its registry position
        # (class X(Mixin): _inherit = ["mixin", "other"]); putting it after all the
        # _inherit bases would contradict its own MRO.
        rest = [base for name, base in written.items() if name not in merged]
        original_mro = info.mro
        info.bases = [*merged.values(), *rest]
        info.mro = []  # calculate_mro() returns a cached MRO when one is set
        try:
            calculate_mro(info)
        except MroError:
            info.bases, info.mro = original, original_mro

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
        if cycle and cycle[0] == contribution:  # once per cycle, at its first class
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

    def _model_type(self, model: str, addon_name: str) -> Instance | AnyType | None:
        """Resolve a model name as seen from an addon.

        Returns:
            The model class; ``Any`` if the model exists but its class is not
            loaded in this mypy run; ``None`` if no visible addon defines it.
        """
        tips = self.registry.model_tips(model, addon_name)
        for tip in tips:
            node = self.lookup_fully_qualified(tip.fullname)
            if node is not None and isinstance(node.node, TypeInfo):
                return Instance(node.node, [])
        return AnyType(TypeOfAny.special_form) if tips else None

    def _env_getitem(self, ctx: MethodContext) -> Type:
        if not ctx.args or len(ctx.args[0]) != 1 or not isinstance(ctx.args[0][0], StrExpr):
            return ctx.default_return_type
        addon_name = self.registry.addon_name_for_path(ctx.api.path)
        if addon_name is None:
            return ctx.default_return_type  # outside any addon: see the module docstring
        model = ctx.args[0][0].value
        model_type = self._model_type(model, addon_name)
        if model_type is None:
            ctx.api.fail(f'Unknown Odoo model "{model}"', ctx.context, code=ODOO_MODEL)
            return ctx.default_return_type
        return model_type if isinstance(model_type, Instance) else ctx.default_return_type

    def _relational_field(self, ctx: FunctionContext) -> Type:
        default = get_proper_type(ctx.default_return_type)
        if not isinstance(default, Instance):
            return ctx.default_return_type
        comodel = _comodel_expression(ctx)
        addon_name = self.registry.addon_name_for_path(ctx.api.path)
        if comodel is None or addon_name is None:
            return ctx.default_return_type
        model_type = self._model_type(comodel.value, addon_name)
        if model_type is None:
            ctx.api.fail(f'Unknown Odoo model "{comodel.value}"', comodel, code=ODOO_MODEL)
            model_type = AnyType(TypeOfAny.from_error)
        return default.copy_modified(args=[model_type])

    def _relational_get(self, ctx: MethodSigContext) -> FunctionLike:
        descriptor = get_proper_type(ctx.type)
        signature = ctx.default_signature
        if not isinstance(descriptor, Instance):
            return signature
        for base in descriptor.type.mro:
            if base.fullname == RELATIONAL_BASE:
                break
        else:
            return signature
        comodel = get_proper_type(map_instance_to_supertype(descriptor, base).args[0])
        if not isinstance(comodel, Instance):
            return signature
        return signature.copy_modified(ret_type=comodel)


def _normalize_meta_attributes(ctx: ClassDefContext) -> None:
    """Give ``_inherit``, ``_name``, ``_order``... the type declared on ``BaseModel``.

    Without this, ``_inherit = "res.partner"`` clashes with a base class that
    happens to use a list, which is legal in Odoo. Attributes ``BaseModel``
    declares without an annotation become ``Any``.
    """
    info = ctx.cls.info
    if info.fullname == BASE_MODEL or not info.has_base(BASE_MODEL):
        return
    base_model = next(item for item in info.mro if item.fullname == BASE_MODEL)
    for statement in ctx.cls.defs.body:
        if not isinstance(statement, AssignmentStmt) or statement.type is not None:
            continue
        if len(statement.lvalues) != 1 or not isinstance(statement.lvalues[0], NameExpr):
            continue
        target = statement.lvalues[0]
        if not target.name.startswith("_") or target.name.startswith("__"):
            continue
        declared = base_model.names.get(target.name)
        if declared is None or not isinstance(declared.node, Var):
            continue
        base_var = declared.node
        explicit = base_var.type is not None and not base_var.is_inferred
        declared_type = base_var.type if explicit else AnyType(TypeOfAny.special_form)
        if target.name == "_name":
            # Only BaseModel itself has ``_name = None``; in a model class the name is
            # a str, and ``str | None`` would reject ``_inherit = _name``. Every class
            # gets the same narrowed type, so the classes stay compatible.
            declared_type = ctx.api.named_type("builtins.str")
        # Pretend the user wrote ``_inherit: <declared type> = ...``; otherwise the
        # checker re-infers the type from the right-hand side.
        statement.type = declared_type
        target.is_inferred_def = False
        if isinstance(target.node, Var):
            target.node.type = declared_type
            target.node.is_inferred = False


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
