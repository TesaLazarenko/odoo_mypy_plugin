"""Detect the version of the configured Odoo, without importing it.

The ``odoo_version`` setting is the single source of truth for which version the
checks target (see :mod:`odoo_typing.compat`). The Odoo found on disk is only compared
with it, so that a config that points at another Odoo is noticed: its ``release.py``,
and whether it defines the classes the version's profile names.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from odoo_typing.addons import is_file
from odoo_typing.compat import SUPPORTED_VERSIONS, OdooProfile, profile_for

__all__ = ["profile_mismatch_warning", "read_odoo_version", "version_mismatch_warning"]
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
    """Warning text if the Odoo at ``odoo_path`` is not the final release ``configured``.0.

    An intermediate version (SaaS, ``master``: ``saas~18.3``) has no profile of its own,
    whatever its major; nor does a major the plugin does not support. An undetectable
    version gives no warning: test trees and patched checkouts may have no usable
    ``release.py``.
    """
    version = read_odoo_version(odoo_path)
    if version is None or version == (configured, 0):
        return None
    major, minor = version
    found = f"the Odoo at {odoo_path} is Odoo {major}.{minor}"
    supported = f"{SUPPORTED_VERSIONS[0]}.0 to {SUPPORTED_VERSIONS[-1]}.0"
    if minor:
        return (
            f"odoo-typing: warning: {found}, an intermediate (SaaS or development) version, "
            f"and odoo_version = {configured}: odoo-typing supports the final releases "
            f"{supported}, so some checks may be missing or wrong"
        )
    if major not in SUPPORTED_VERSIONS:
        return (
            f"odoo-typing: warning: odoo_version = {configured}, but {found}, which "
            f"odoo-typing does not support (only {supported}); point odoo_path at an "
            f"Odoo {configured}"
        )
    return (
        f"odoo-typing: warning: odoo_version = {configured}, but {found}; the checks "
        f"follow odoo_version, so set it to {major} or point odoo_path at an Odoo {configured}"
    )


def profile_mismatch_warning(profile: OdooProfile, odoo_path: Path) -> str | None:
    """Warning text if the Odoo at ``odoo_path`` does not define the classes of the profile.

    The hooks are keyed on the modules where Odoo defines its classes (see
    :mod:`odoo_typing.compat`); in an Odoo that defines them elsewhere they silently
    never fire. That is checked on the source of a few of them (``BaseModel``,
    ``Environment``...), a few milliseconds. No warning for the final release of another
    major (:func:`version_mismatch_warning` tells it), nor for a tree without the sources
    of Odoo's ORM (test trees).
    """
    version = read_odoo_version(odoo_path)
    if version is not None and version[0] != profile.version and not version[1]:
        return None
    sources: dict[Path, bytes] = {}
    missing = [name for name in _anchors(profile) if not _defines(odoo_path, name, sources)]
    if not missing or not _has_orm_sources(odoo_path):
        return None
    shown = ", ".join(missing[:3]) + (", ..." if len(missing) > 3 else "")
    return (
        f"odoo-typing: warning: the Odoo at {odoo_path} does not define {shown}, which "
        f"the Odoo {profile.version} profile expects: odoo_version or odoo_path is wrong, "
        "or this is an intermediate (SaaS or development) version; checks would be "
        "silently incomplete"
    )


def _anchors(profile: OdooProfile) -> list[str]:
    """Full names of classes in the modules the profile's hooks are keyed on."""
    names = [
        profile.base_model,
        profile.new_id,
        profile.environment,
        profile.many2one,
        profile.request_class,
    ]
    if profile.domain_class is not None:
        names.append(profile.domain_class)
    return names


def _defines(odoo_path: Path, fullname: str, sources: dict[Path, bytes]) -> bool:
    """Whether the module of ``odoo.<module>.<Class>`` defines the class at top level."""
    path = _module_file(odoo_path, fullname)
    if path is None:
        return False
    if path not in sources:
        try:
            sources[path] = path.read_bytes()
        except OSError:
            sources[path] = b""
    pattern = rb"^class " + re.escape(fullname.rpartition(".")[2].encode()) + rb"\b"
    return re.search(pattern, sources[path], re.MULTILINE) is not None


def _module_file(odoo_path: Path, fullname: str) -> Path | None:
    """The file of the module of ``odoo.<module>.<Class>`` (``x.py`` or ``x/__init__.py``)."""
    *package, module = fullname.split(".")[1:-1]
    base = odoo_path.joinpath(*package)
    for candidate in (base / f"{module}.py", base / module / "__init__.py"):
        if is_file(candidate):  # never raises: a directory that cannot be searched has none
            return candidate
    return None


def _has_orm_sources(odoo_path: Path) -> bool:
    """Whether ``odoo_path`` has the module of ``BaseModel`` of some supported version."""
    profiles = [profile_for(version) for version in SUPPORTED_VERSIONS]
    return any(
        _module_file(odoo_path, profile.base_model) is not None
        for profile in profiles
        if profile is not None
    )
