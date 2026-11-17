"""Hooks of semantic analysis: the MRO of model classes, ``_inherit`` problems, delegation."""

from __future__ import annotations

from functools import partial

from mypy.nodes import (
    GDEF,
    AssignmentStmt,
    ClassDef,
    Expression,
    ListExpr,
    NameExpr,
    StrExpr,
    SymbolTableNode,
    TupleExpr,
    TypeInfo,
    Var,
)
from mypy.plugin import ClassDefContext
from mypy.types import AnyType, Instance, LiteralType, TypeOfAny

from odoo_typing.classes import (
    Lookup,
    add_delegated_fields,
    add_log_access_fields,
    install_mro,
    instance_of,
    make_comodel_generic,
    merge_mro,
    record_python_mro,
)
from odoo_typing.compat import OdooProfile
from odoo_typing.hooks.class_attributes import bind_method_aliases
from odoo_typing.hooks.env import type_request
from odoo_typing.hooks.meta_attributes import normalize_meta_attributes
from odoo_typing.hooks.models import (
    FIELDS_DIGEST,
    ODOO_MODEL,
    ODOO_MODEL_DEPENDS,
    Hooks,
    Models,
    errors_hidden,
    fine_grained,
    not_in_dependencies,
)
from odoo_typing.hooks.nodes import class_infos, field_sources, incomplete_modules, lookup_class
from odoo_typing.hooks.passed import widen_passed
from odoo_typing.methods import annotate_self_overrides
from odoo_typing.records import digest
from odoo_typing.references import (
    META_FIELD_ATTRIBUTES,
    ODOO_FIELD_REF,
    did_you_mean,
    order_fields,
)
from odoo_typing.registry import MissingParent
from odoo_typing.scan_store import encode
from odoo_typing.scanner import Contribution, ModuleScan

__all__ = ["ModelClassHooks"]


class ModelClassHooks(Hooks):
    """Give model classes what Odoo's registry gives them: MRO, delegated and log fields.

    Attributes:
        request_typed: Whether ``odoo.http.request`` has its type (or needs none); the
            plugin resets it when the settings change.
    """

    def __init__(self, models: Models) -> None:
        super().__init__(models)
        self.request_typed = False
        # ``__odoo_fields`` per module, kept with the scan it was made from: each model
        # class of a module, and each analysis pass, asks again.
        self._fields_digests: dict[str, tuple[ModuleScan, str]] = {}

    def comodel_generic(self, ctx: ClassDefContext) -> None:
        """Give a relational field class a type variable for its comodel."""
        # Bound ``object``: ``BaseModel`` may not be analyzed yet (an import cycle).
        object_type = ctx.api.named_type("builtins.object")
        make_comodel_generic(ctx.cls, object_type, self._profile.is_field_class)

    def merge_bases(self, contribution: Contribution, ctx: ClassDefContext) -> None:
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
        incomplete = incomplete_modules(ctx)
        chain = class_infos(
            self.registry.linearization(contribution)[1:], ctx.api.modules, incomplete
        )
        if chain.pending and not ctx.api.final_iteration:
            ctx.api.defer()
            return
        merge = merge_mro(info, python_mro, chain.infos, lookup)
        bases = class_infos(self.registry.bases(contribution), ctx.api.modules, incomplete).infos
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

    def after_class_body(self, ctx: ClassDefContext) -> None:
        """Run once the class body is analyzed (and again for each written base)."""
        normalize_meta_attributes(ctx, self._profile)
        bind_method_aliases(ctx)
        if not self.request_typed:
            self.request_typed = type_request(self._profile, ctx.api.modules)
        if not ctx.cls.info.has_base(self._profile.base_model):
            return  # the hook runs for every class, also outside Odoo
        annotate_self_overrides(ctx)
        contribution = self.registry.contribution(ctx.cls.info.fullname)
        if contribution is not None:
            if errors_hidden(ctx.api):
                # What Odoo annotates more narrowly than its own code passes.
                widen_passed(ctx, self.registry.passed(contribution))
            else:
                self._check_meta_references(contribution, ctx)
            self._delegate_fields(contribution, ctx)
            if self.registry.log_access(contribution):
                add_log_access_fields(ctx.cls.info, _log_access_types(ctx, self._profile))
            self._add_fields_digest(ctx)

    def _check_meta_references(self, contribution: Contribution, ctx: ClassDefContext) -> None:
        """Report the fields that ``_order``, ``_rec_name`` and the like name but are not there.

        In the class body of a concrete model, against the fields its addon sees. Odoo
        asserts ``_rec_name`` when it sets the model up and raises for the others when it
        searches. Reported from semantic analysis (mypy has no hook on a class attribute
        when it checks types), so the daemon does not report again when another module
        drops the field: see docs/how-it-works.md.
        """
        model, addon_name = contribution.model, contribution.addon
        if not self.registry.concrete(model, addon_name):
            return
        fields = self.registry.fields(model, addon_name)
        if not fields:
            return
        for statement in ctx.cls.defs.body:
            if not isinstance(statement, AssignmentStmt) or len(statement.lvalues) != 1:
                continue
            target = statement.lvalues[0]
            if not isinstance(target, NameExpr):
                continue
            for name, node in _meta_field_names(target.name, statement.rvalue, ctx):
                if name not in fields:
                    message = f'Odoo model "{model}" has no field "{name}" in {target.name}'
                    message += did_you_mean(name, fields)
                    ctx.api.fail(message, node, code=ODOO_FIELD_REF)

    def _add_fields_digest(self, ctx: ClassDefContext) -> None:
        """Give the module of a model class a hidden attribute: a digest of its fields.

        The type of a field (``fields.Float(...)``) does not change with its arguments
        (``store=``, ``search=``, ``related=``...), so neither does the module's snapshot in
        the daemon, which then would not check again what the arguments decide (a domain
        on a field that is no longer stored). A ``Literal`` digest of the module's model
        classes (their fields and what the registry reads of them: ``_name``,
        ``_inherit``, ``_inherits``...) makes such a change a change of
        ``<module.__odoo_fields>``. A module attribute, not a class member: mypy would
        make every class that inherits a member depend on it. Only in fine-grained mode,
        like the dependencies on it (:meth:`Hooks._depend_on`): a full run would spend time
        (about 1 % on the account addon) on an attribute nothing reads.
        """
        if not fine_grained(ctx.api.options):
            return
        info = ctx.cls.info
        tree = ctx.api.modules.get(info.module_name)
        scan = self.registry.module_scan(info.module_name)
        if tree is None or scan is None:
            return
        value = self._fields_digest(scan)
        existing = tree.names.get(FIELDS_DIGEST)
        if existing is not None and isinstance(existing.node, Var):
            found = existing.node.type
            if isinstance(found, LiteralType) and found.value == value:
                return
        var = Var(FIELDS_DIGEST, LiteralType(value, ctx.api.named_type("builtins.str")))
        var._fullname = f"{tree.fullname}.{FIELDS_DIGEST}"
        tree.names[FIELDS_DIGEST] = SymbolTableNode(
            GDEF, var, module_hidden=True, plugin_generated=True
        )

    def _fields_digest(self, scan: ModuleScan) -> str:
        """The digest of a module's model classes: what ``__odoo_fields`` holds."""
        kept = self._fields_digests.get(scan.module)
        if kept is not None and kept[0] is scan:
            return kept[1]
        parts = [
            [item.class_name, item.model, item.parents, item.inherits, item.delegates]
            for item in scan.contributions
        ]
        details = [[item.log_access, item.abstract] for item in scan.contributions]
        fields = [item.fields for item in scan.contributions]
        # What the methods assign decides whether a computed field may be empty.
        assigned = [[item.assigned, item.assigned_any] for item in scan.contributions]
        value = digest(
            [encode(tuple(map(tuple, parts))), details, encode(tuple(fields)), assigned]
        )[:16]
        self._fields_digests[scan.module] = (scan, value)
        return value

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
            found = field_sources(classes, ctx.api.modules, incomplete_modules(ctx))
            if found is None and not ctx.api.final_iteration:
                ctx.api.defer()
                return
            sources += found or []
        add_delegated_fields(ctx.cls.info, sources)


def _meta_field_names(
    attribute: str, value: Expression, ctx: ClassDefContext
) -> list[tuple[str, Expression]]:
    """The fields the value of a class attribute names, each with the node to report at.

    ``_order`` is an order (a string that is not one is reported here); the attributes of
    :data:`META_FIELD_ATTRIBUTES` are a field name, or a list of names (their first steps:
    ``_rec_names_search`` takes paths).
    """
    if attribute == "_order" and isinstance(value, StrExpr):
        names = order_fields(value.value)
        if names is None:
            message = f'_order = "{value.value}" is not an order: Odoo raises when it searches'
            ctx.api.fail(message, value, code=ODOO_FIELD_REF)
        return [(name, value) for name in names or ()]
    if attribute not in META_FIELD_ATTRIBUTES:
        return []
    items = value.items if isinstance(value, ListExpr | TupleExpr) else [value]
    return [(item.value.split(".")[0], item) for item in items if isinstance(item, StrExpr)]


def _semanal_lookup(ctx: ClassDefContext) -> Lookup:
    return partial(lookup_class, ctx.api.modules)


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
        where = not_in_dependencies(problem.outside_addons, addon)
        return f'Odoo model "{model}" in _inherit is defined in {where}'
    message = f'Unknown Odoo model "{model}" in _inherit'
    if problem.missing_depends:
        message += f"; dependencies not found on addons_path: {', '.join(problem.missing_depends)}"
    return message
