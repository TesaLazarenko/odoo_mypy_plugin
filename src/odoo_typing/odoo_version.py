"""Detect the version of the configured Odoo, without importing it.

The ``odoo_version`` setting is the single source of truth for which version the
checks target (see :mod:`odoo_typing.compat`). The Odoo found on disk is only compared
with it, so that a config that points at another Odoo is noticed.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

__all__ = ["read_odoo_version", "version_mismatch_warning"]
_SAAS = re.compile(r"saas~(\d+)")


def read_odoo_version(odoo_path: Path) -> tuple[int, int] | None:
    """Return ``(major, minor)`` from ``version_info`` in ``odoo_path/release.py``.

    Handles final releases (``version_info = (18, 0, ...)``) and SaaS releases
    (``version_info = ('saas~17', 4, ...)``).

    Args:
        odoo_path: Directory of the ``odoo`` package.

    Returns:
        The version, or ``None`` if the file is missing or has another format.
    """
    try:
        tree = ast.parse((odoo_path / "release.py").read_bytes())
    except (OSError, SyntaxError, ValueError):
        return None
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and any(
                isinstance(target, ast.Name) and target.id == "version_info"
                for target in node.targets
            )
            and isinstance(node.value, ast.Tuple)
        ):
            return _major_minor(node.value.elts)
    return None


def _major_minor(elements: list[ast.expr]) -> tuple[int, int] | None:
    values = [item.value if isinstance(item, ast.Constant) else None for item in elements[:2]]
    if len(values) < 2 or not isinstance(values[1], int):
        return None
    major = values[0]
    if isinstance(major, str) and (match := _SAAS.fullmatch(major)):
        major = int(match.group(1))
    return (major, values[1]) if isinstance(major, int) else None


def version_mismatch_warning(configured: int, odoo_path: Path) -> str | None:
    """Warning text if the Odoo at ``odoo_path`` is not the configured major version.

    An undetectable version gives no warning: test trees and patched checkouts may
    have no usable ``release.py``.
    """
    version = read_odoo_version(odoo_path)
    if version is None or version[0] == configured:
        return None
    major, minor = version
    return (
        f"odoo-typing: warning: odoo_version = {configured}, but the Odoo at {odoo_path} "
        f"is Odoo {major}.{minor}; the checks follow odoo_version, so set it to {major} "
        f"or point odoo_path at an Odoo {configured}"
    )
