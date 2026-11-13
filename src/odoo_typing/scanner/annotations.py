"""The ``odoo.values.*`` and ``odoo.model.*`` annotations a file uses (a text search).

The annotations themselves are read by mypy (:mod:`odoo_typing.annotations`); the scan
only tells which files use them, so that only those are rewritten when mypy analyzes
them, and which models they name, for the dependency edges and the config data. A text
search over the bytes the scan reads anyway finds them in annotations, type comments
and ``Annotated[...]`` metadata alike; a match in a comment only adds an unused edge.
"""

from __future__ import annotations

import re
from typing import Final

__all__ = ["MODEL", "VALUES", "AnnotationAlias", "annotation_aliases"]

VALUES: Final = "values"  # ``odoo.values.<model>``: a dict of field values (vals)
MODEL: Final = "model"  # ``odoo.model.<model>``: the model's records
# (kind, alias): ``("values", "res_partner")`` for ``odoo.values.res_partner``.
AnnotationAlias = tuple[str, str]

_PATTERN: Final = re.compile(rb"odoo\.(values|model)\.([A-Za-z0-9_.]*[A-Za-z0-9_])")


def annotation_aliases(source: bytes) -> frozenset[AnnotationAlias]:
    """The ``(kind, alias)`` pairs of the ``odoo.values.*`` / ``odoo.model.*`` names in a file."""
    if b"odoo.values." not in source and b"odoo.model." not in source:
        return frozenset()
    return frozenset((kind.decode(), alias.decode()) for kind, alias in _PATTERN.findall(source))
