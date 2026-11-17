"""``odoo-typing-check``: mypy on addons named as you see them, errors where the files are.

mypy must be given an addon by its module name (``-p odoo.addons.my_addon``), reads it
through the overlay root and prints the paths under that root
(``.odoo-root/odoo/addons/my_addon/models/sale.py``): no path an editor has open, a
pre-commit hook passes or a CI annotation can attach to a diff. This wraps it::

    odoo-typing-check                               # every addon of addons_path
    odoo-typing-check my_addon other_addon          # by name
    odoo-typing-check addons/my_addon               # by directory
    odoo-typing-check addons/my_addon/models/sale.py  # the errors of these files only
    odoo-typing-check --daemon my_addon -- --strict   # dmypy; mypy's own options after --

It regenerates the overlay root when the addons on the addons paths changed
(``odoo-typing-root``), runs mypy on the addons the targets belong to, and prints each
path as the real file, relative to the current directory where it is under it. A path
outside the addons is skipped (a pre-commit hook passes every changed file). For
targets that are files or directories inside an addon the whole addon is checked (mypy
needs it) and only the errors of those files are printed; the exit status is then 1 if
there are any, and mypy's otherwise.
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final

from mypy import api as mypy_api

from odoo_typing.addons import Addon, AddonIndex
from odoo_typing.config import ConfigError, Settings, load_settings
from odoo_typing.overlay import MARKER, OverlayError, build_overlay, overlay_differences

__all__ = ["Targets", "main", "real_paths", "resolve_targets"]

# ``path:line: `` or ``path:line:column: `` at the start of a line of mypy's output. The
# path may start with a drive (``C:\\addons\\x.py`` on Windows).
LOCATION: Final = re.compile(r"^(?P<path>(?:[A-Za-z]:)?[^:\n]+):(?P<line>\d+)(?P<rest>(?::\d+)*: )")
ERROR: Final = ": error: "
# One message per line, so that the lines of other files can be left out.
FILTER_FLAGS: Final = ("--no-pretty", "--hide-error-context", "--no-error-summary")

MypyRun = Callable[[list[str]], tuple[str, str, int]]


@dataclass(frozen=True, slots=True)
class Targets:
    """What the targets of a command line name.

    Attributes:
        addons: The addons to check, in the order given.
        files: The resolved files and directories inside addons whose errors alone are
            printed; empty if every target is a whole addon.
    """

    addons: tuple[Addon, ...]
    files: frozenset[Path] = field(default_factory=frozenset)


class TargetError(Exception):
    """A target that names no addon of the addons paths."""


def resolve_targets(targets: Sequence[str], settings: Settings, index: AddonIndex) -> Targets:
    """The addons that names and paths belong to.

    Args:
        targets: Addon names, addon directories, or files and directories inside
            addons; none for every addon of ``addons_path``.
        settings: The plugin's settings.
        index: The addons of the settings.

    Returns:
        The addons, and the paths inside them to keep the errors of. A path outside
        the addons is skipped with a note, so the addons may be none.

    Raises:
        TargetError: For a target that is neither an addon nor a path.
    """
    by_path = {addon.path.resolve(): addon for addon in index.addons.values()}
    if not targets:
        roots = {root.resolve() for root in settings.addons_paths}
        own = [addon for path, addon in by_path.items() if path.parent in roots]
        if not own:
            raise TargetError("no addon in addons_path: name the addons to check")
        return Targets(tuple(sorted(own, key=lambda addon: addon.name)))
    addons: dict[str, Addon] = {}
    files: set[Path] = set()
    whole: set[str] = set()
    for target in targets:
        path = Path(target)
        if not path.exists():
            named = index.addons.get(target)
            if named is None:
                raise TargetError(f"{target} is neither an addon nor a path inside one")
            addons.setdefault(named.name, named)
            whole.add(named.name)
            continue
        real = path.resolve()
        found = next(
            (by_path[item] for item in (real, *real.parents) if item in by_path),
            None,
        )
        if found is None:
            # A hook passes every changed file: those outside the addons are not ours.
            print(f"odoo-typing-check: {target} is in no addon: skipped", file=sys.stderr)
            continue
        addons.setdefault(found.name, found)
        if real == found.path.resolve():
            whole.add(found.name)
        else:
            files.add(real)
    if not files:
        return Targets(tuple(addons.values()))
    # A whole addon given next to files of others: its errors are printed too.
    files |= {addons[name].path.resolve() for name in whole}
    return Targets(tuple(addons.values()), frozenset(files))


def real_paths(lines: Sequence[str], start: Path) -> list[tuple[str, Path | None]]:
    """Each line of mypy's output with its path as the real file, and that file.

    Args:
        lines: mypy's output.
        start: The directory the printed paths are relative to where they are under it.

    Returns:
        The lines, each with the resolved file it is about (``None`` for a line that
        names none, such as the summary).
    """
    resolved: dict[str, tuple[str, Path | None]] = {}
    found: list[tuple[str, Path | None]] = []
    for line in lines:
        match = LOCATION.match(line)
        if match is None:
            found.append((line, None))
            continue
        written = match["path"]
        if written not in resolved:
            resolved[written] = _real(written, start)
        shown, real = resolved[written]
        found.append((shown + line[match.end("path") :], real))
    return found


def _real(written: str, start: Path) -> tuple[str, Path | None]:
    path = Path(written)
    if not path.exists():
        return written, None
    real = Path(os.path.realpath(path))
    try:
        return str(real.relative_to(start)), real
    except ValueError:
        return str(real), real


def _dmypy_run(arguments: list[str]) -> tuple[str, str, int]:
    """``dmypy run`` with mypy's arguments: the daemon keeps what it analyzed."""
    command = [sys.executable, "-m", "mypy.dmypy", "run", "--", *arguments]
    done = subprocess.run(command, capture_output=True, text=True, check=False)
    return done.stdout, done.stderr, done.returncode


def _refresh_overlay(settings: Settings, index: AddonIndex) -> None:
    """Regenerate the overlay root if the addons of the addons paths changed."""
    output = settings.overlay_root
    if output is None:
        return
    if (output / MARKER).is_file() and not overlay_differences(index, output):
        return
    count = build_overlay(settings, output)
    print(f"odoo-typing-check: wrote {output} ({count} addons)", file=sys.stderr)


def main(argv: Sequence[str] | None = None, run: MypyRun | None = None) -> int:
    """Command-line entry point of ``odoo-typing-check``.

    Args:
        argv: Arguments, without the program name (default: ``sys.argv[1:]``).
        run: Runs mypy with arguments (default: ``mypy.api.run``, or ``dmypy run`` with
            ``--daemon``).

    Returns:
        mypy's exit status; for targets inside addons, 1 if their files have errors and
        0 if not; 2 on a configuration error, an unknown target or a mypy failure.
    """
    parser = argparse.ArgumentParser(
        prog="odoo-typing-check",
        description="Run mypy on addons by name or path; print errors at the real paths.",
        epilog="mypy's own options follow --",
    )
    parser.add_argument("targets", nargs="*", help="addon names, addon directories, files")
    parser.add_argument("--config", default="pyproject.toml", help="default: %(default)s")
    parser.add_argument("--daemon", action="store_true", help="run dmypy instead of mypy")
    arguments = list(sys.argv[1:] if argv is None else argv)
    split = arguments.index("--") if "--" in arguments else len(arguments)
    args = parser.parse_args(arguments[:split])
    try:
        settings = load_settings(args.config, require_overlay=False)
        index = AddonIndex(settings.odoo_path, settings.addons_paths, settings.custom_prefix)
        _refresh_overlay(settings, index)
        targets = resolve_targets(args.targets, settings, index)
    except (ConfigError, OverlayError, TargetError, OSError) as exc:
        print(f"odoo-typing-check: error: {exc}", file=sys.stderr)
        return 2
    if not targets.addons:
        print("odoo-typing-check: no addon to check")
        return 0
    command = ["--config-file", args.config, *arguments[split + 1 :]]
    if targets.files:
        command += FILTER_FLAGS
    for addon in targets.addons:
        command += ["-p", addon.module_prefix]
    if run is None:
        run = _dmypy_run if args.daemon else mypy_api.run
    stdout, stderr, status = run(command)
    sys.stderr.write(stderr)
    lines = real_paths(stdout.splitlines(), Path.cwd().resolve())
    if status not in (0, 1) or not targets.files:
        for line, _ in lines:
            print(line)
        return status
    return _print_files(lines, targets.files)


def _print_files(lines: list[tuple[str, Path | None]], files: frozenset[Path]) -> int:
    """Print the lines of some files (or of files under some directories) and a summary."""
    errors = elsewhere = 0
    for line, real in lines:
        inside = real is not None and (real in files or any(item in files for item in real.parents))
        if inside:
            print(line)
        if ERROR in line:
            errors += inside
            elsewhere += not inside
    print(f"odoo-typing-check: {errors} errors in the given files ({elsewhere} elsewhere)")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
