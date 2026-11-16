"""What a scan gives: the model classes of an addon's files and their field definitions."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from odoo_typing.guards import Guard
from odoo_typing.xmlids import DataScan

__all__ = [
    "AddonScan",
    "Contribution",
    "FieldClass",
    "FieldInfo",
    "ModuleScan",
    "Positional",
    "SourceError",
    "UnlinkedField",
]

EMPTY_DATA: Final = DataScan((), ())


@dataclass(frozen=True, slots=True)
class FieldInfo:
    """One field definition in a class body (``name = fields.Char(...)``).

    Each attribute is the argument the definition passes, ``None`` if it passes none:
    Odoo merges the definitions of a field argument by argument (see
    :mod:`odoo_typing.model_fields`). An argument whose value the scan cannot evaluate
    (an imported constant, a lambda) stays ``None`` and is named in ``dynamic``.

    Attributes:
        name: Field name.
        field_type: Name of the field class (``Char``, ``Many2one``); of an
            :class:`UnlinkedField`, the dotted name of its class.
        comodel: ``comodel_name`` of a relational field.
        inverse_name: ``inverse_name`` of a ``One2many``.
        selection: Keys of a ``selection`` given as a list of pairs.
        selection_method: Name of the method of a ``selection`` given as a method.
        selection_add: Keys a ``selection_add`` adds.
        related: ``related`` path.
        compute: Name of the ``compute`` method.
        inverse: Name of the ``inverse`` method.
        search: Name of the ``search`` method.
        readonly: ``readonly`` flag.
        required: ``required`` flag.
        store: ``store`` flag.
        delegate: ``delegate`` flag (``Many2one``).
        compute_sql: ``True`` if ``compute_sql`` is passed (Odoo 20; its value is often a
            lambda, so only that it is passed).
        attachment: ``attachment`` flag (``Binary``).
        dynamic: Arguments passed with values the scan cannot evaluate.
    """

    name: str
    field_type: str
    comodel: str | None = None
    inverse_name: str | None = None
    selection: tuple[str, ...] | None = None
    selection_method: str | None = None
    selection_add: tuple[str, ...] | None = None
    related: str | None = None
    compute: str | None = None
    inverse: str | None = None
    search: str | None = None
    readonly: bool | None = None
    required: bool | None = None
    store: bool | None = None
    delegate: bool | None = None
    compute_sql: bool | None = None
    attachment: bool | None = None
    dynamic: frozenset[str] = frozenset()


# A positional argument of a field call, read each way a field class may take it: as a
# text (a comodel, an inverse name, a selection method), as the keys of a selection list,
# as a method of the class (by name).
Positional = tuple[str | None, tuple[str, ...] | None, str | None]


@dataclass(frozen=True, slots=True)
class UnlinkedField(FieldInfo):
    """A field of a class that the scan of its addon cannot look at.

    A class of another addon, of Odoo's own modules or of a library (and, in a file read
    on its own, of another file): ``field_type`` is its dotted name
    (``odoo.addons.queue_job.fields.JobSerialized``). The registry reads what describes
    the class and makes a :class:`FieldInfo` of it, or drops the name if the class is no
    field class (:meth:`odoo_typing.registry.scans.ScannedAddons.link_classes`).

    Attributes:
        positional: The positional arguments of the call: which parameters they are
            depends on the Odoo class the class derives from.
    """

    positional: tuple[Positional, ...] = ()


@dataclass(frozen=True, slots=True)
class FieldClass:
    """A class that may be a field class: what it derives from.

    See :mod:`odoo_typing.scanner.field_classes`. Also a function that returns fields,
    and a name a module only imports (``from .fields import BigInteger`` in a package's
    ``__init__``): its one base is the class it names, so that an import from the
    package finds it.

    Attributes:
        name: Name of the class in its module.
        bases: Dotted names of the classes it derives from: Odoo's
            (``odoo.fields.Integer``) and those of other addons. The bases that classes
            of its own addon have stand for these classes.
        converts: Whether the class or one of its addon's bases defines a
            ``convert_to_*`` method: its values are not those of the Odoo class.
        arguments: Whether one of them defines ``__init__`` (or is a function): its
            positional arguments are not those of the Odoo class.
    """

    name: str
    bases: tuple[str, ...]
    converts: bool = False
    arguments: bool = False


@dataclass(frozen=True, slots=True)
class Contribution:
    """One Python class that defines or extends an Odoo model.

    Attributes:
        addon: Name of the addon that contains the class.
        module: Python module name, as mypy sees it.
        class_name: Name of the class.
        model: Odoo model name (``_name``, or the first ``_inherit``).
        parents: Models listed in ``_inherit``.
        position: Registration order: the addon's load key, then the class's
            creation index within the addon.
        comodels: Comodels of the relational fields the class body declares
            (``x = fields.Many2one("res.partner")``), in source order.
        fields: The fields the class body declares (``x = fields.Char()``), in source
            order.
        methods: Names of the methods the class body defines.
        delegates: Models the class delegates fields to: the keys of ``_inherits``,
            then the comodels of ``fields.Many2one(..., delegate=True)``.
        inherits: ``_inherits`` of the class body: (model, field name) pairs.
        log_access: ``_log_access`` as the class body sets it, ``False`` if it only
            sets ``_auto = False`` (Odoo's default follows ``_auto``), ``None`` if
            neither is set.
        abstract: Whether the class is abstract: ``_abstract`` of its body, else whether
            it derives from ``AbstractModel`` rather than ``Model`` / ``TransientModel``;
            ``None`` if neither tells.
    """

    addon: str
    module: str
    class_name: str
    model: str
    parents: tuple[str, ...]
    position: tuple[int, str, int]
    comodels: tuple[str, ...] = ()
    fields: tuple[FieldInfo, ...] = ()
    methods: tuple[str, ...] = ()
    delegates: tuple[str, ...] = ()
    inherits: tuple[tuple[str, str], ...] = ()
    log_access: bool | None = None
    abstract: bool | None = None

    @property
    def fullname(self) -> str:
        """Fully qualified class name."""
        return f"{self.module}.{self.class_name}"

    @property
    def field_names(self) -> tuple[str, ...]:
        """Names of the fields the class body declares, in source order."""
        return tuple(info.name for info in self.fields)


@dataclass(frozen=True, slots=True)
class ModuleScan:
    """What one Python file of an addon contributes.

    Attributes:
        module: Python module name.
        path: Source file.
        contributions: Model classes defined in the file, in source order.
        referenced_models: Model names used as string literals in ``env[...]``
            and as relational field comodels.
        guards: Lines where a model is checked to be installed (``odoo_typing.guards``).
        annotations: The ``odoo.values.*`` / ``odoo.model.*`` names the file uses, as
            ``(kind, alias)`` pairs (``odoo_typing.scanner.annotations``).
        classes: The classes of the file that may be field classes.
    """

    module: str
    path: Path
    contributions: tuple[Contribution, ...]
    referenced_models: frozenset[str]
    # Only the file's own checks read it, so a shifted line must not change the
    # fingerprint of the addons that depend on this one (see fingerprint._canonical).
    guards: tuple[Guard, ...] = field(default=(), metadata={"fingerprint": False})
    # Only the file's own analysis and edges use them (a change is a change of the file).
    annotations: frozenset[tuple[str, str]] = field(
        default=frozenset(), metadata={"fingerprint": False}
    )
    classes: tuple[FieldClass, ...] = ()


class SourceError(Exception):
    """A Python source file that cannot be read or parsed.

    Attributes:
        path: The file.
        reason: Short description, e.g. ``invalid syntax (line 3)``.
    """

    def __init__(self, path: Path, reason: str) -> None:
        super().__init__(f"{path}: {reason}")
        self.path = path
        self.reason = reason


@dataclass(frozen=True, slots=True)
class AddonScan:
    """Result of scanning one addon.

    Attributes:
        modules: One entry per imported file, in import order.
        errors: Imported files that could not be parsed (their classes are missing).
        data: The XML ids of the data files of the manifest (``odoo_typing.xmlids``).
    """

    modules: tuple[ModuleScan, ...]
    errors: tuple[SourceError, ...]
    data: DataScan = EMPTY_DATA
