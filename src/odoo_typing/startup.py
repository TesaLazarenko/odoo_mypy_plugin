"""What the plugin checks before it starts: settings, Odoo and Python versions, overlay.

A problem that makes checking pointless ends mypy with the message and exit status 2,
like mypy's own configuration errors (not as a crash of the plugin); the other
functions return a warning for the plugin to print.
"""

from __future__ import annotations

import re
import sys
from typing import Final

from mypy.options import Options

from odoo_typing.compat import SUPPORTED_VERSIONS, OdooProfile, profile_for
from odoo_typing.config import WHERE, ConfigError, Settings, load_settings
from odoo_typing.overlay import overlay_differences
from odoo_typing.registry import Registry

__all__ = [
    "TESTED_MYPY",
    "follow_imports_warning",
    "mypy_version_warning",
    "package_bases_warning",
    "profile_or_exit",
    "python_version_warning_or_exit",
    "settings_or_exit",
    "stale_overlay_warning",
]

# The mypy versions the plugin is tested with, from the first to the one after the last
# (``[low, high)``): keep it equal to the ``mypy`` requirement of pyproject.toml.
TESTED_MYPY: Final = ((2, 1), (2, 5))
_MAJOR_MINOR = re.compile(r"(\d+)\.(\d+)")


def mypy_version_warning(version: str) -> str | None:
    """Warning text if mypy is not a version the plugin is tested with.

    The plugin reads mypy internals beyond the plugin API (tests/test_mypy_internals.py),
    which another version may rename: missed or wrong errors, or an INTERNAL ERROR that
    points at mypy. It is a warning, not an exit: a new mypy release often works, and the
    requirement keeps a normal install in range.

    Args:
        version: mypy's version string (``2.4.1``, ``2.5.0+dev.1a2b3c``).

    Returns:
        The warning text, or ``None`` for a tested version or an unreadable string.
    """
    match = _MAJOR_MINOR.match(version)
    if match is None:
        return None
    low, high = TESTED_MYPY
    if low <= (int(match[1]), int(match[2])) < high:
        return None
    first, after = (".".join(map(str, bound)) for bound in TESTED_MYPY)
    return (
        f"odoo-typing: warning: mypy {version} is not a version odoo-typing is tested with "
        f"(>={first},<{after}); the plugin relies on mypy internals, so expect missed or "
        "wrong errors or an INTERNAL ERROR: install a tested mypy"
    )


def settings_or_exit(options: Options) -> Settings:
    """Read the settings; on a configuration error print it and exit with status 2.

    mypy reports any exception raised while it creates a plugin as an internal error
    of its own and hides the message; ``SystemExit`` passes through.
    """
    try:
        return load_settings(options.config_file, options.mypy_path)
    except ConfigError as exc:
        print(f"odoo-typing: error: {exc}", file=sys.stderr)
        raise SystemExit(2) from None


def profile_or_exit(settings: Settings) -> OdooProfile:
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


def python_version_warning_or_exit(
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


def follow_imports_warning(options: Options) -> str | None:
    """Warning text if mypy skips Odoo's modules (``follow_imports = "skip"``).

    The plugin then sees no ``BaseModel``, no field class and no ``Environment``: almost
    every check stays silent, and mypy reports success. ``"silent"`` analyzes them
    without showing their errors.
    """
    if options.clone_for_module("odoo.models").follow_imports != "skip":
        return None
    return (
        'odoo-typing: warning: follow_imports = "skip" for odoo: the plugin cannot see '
        'Odoo\'s classes, so it checks almost nothing; use "silent" instead'
    )


def package_bases_warning(profile: OdooProfile, settings: Settings, options: Options) -> str | None:
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


def stale_overlay_warning(settings: Settings, registry: Registry) -> str | None:
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
