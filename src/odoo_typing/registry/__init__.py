"""Static model registry: which classes make up each Odoo model, per addon view.

Odoo builds every model at runtime as a chain of all classes that define or
``_inherit`` it, in module load order. An addon only "sees" the classes from
itself and its dependency closure, so every question here is asked from the
point of view of an addon. Code outside any addon has no such view and is never
resolved against the registry (see :mod:`odoo_typing.plugin`).

Each model's MRO is computed the way Odoo builds its registry class: the
``LastOrderedSet`` of ``BaseModel._build_model`` (the model's classes, then the
registry classes of its ``_inherit`` parents, ``base`` last), linearized with C3. A
class sees the earlier classes of its own model, and every class of its parents in
its addon's view, also later ones: Odoo extends a parent's registry class in place,
so a field that ``sms`` adds to ``mail.thread`` is on ``project.task`` for every addon
that depends on both. ``_inherit`` cycles, which Odoo cannot build, are found as
strongly connected components of the model graph first; the edges inside them are
ignored, so no answer depends on which class mypy asks about first.

Everything is computed lazily: the manifests are read on the first question, and
an addon's files are scanned only when a view that contains the addon is queried.

The registry is built in layers, each a class that extends the one before it: what is
read from disk (:mod:`.scans`), the classes of a model an addon sees (:mod:`.visible`),
their MRO (:mod:`.linearization`), the model as the addon sees it and its ``_inherits``
delegation (:mod:`.views`), its fields (:mod:`.fields`) and the modules mypy must load
for it (:mod:`.edges`).
"""

from __future__ import annotations

from odoo_typing.registry.edges import DependencyEdges
from odoo_typing.registry.linearization import MissingParent
from odoo_typing.registry.views import ModelView

__all__ = ["MissingParent", "ModelView", "Registry"]


class Registry(DependencyEdges):
    """Lazily scanned index of Odoo model classes."""
