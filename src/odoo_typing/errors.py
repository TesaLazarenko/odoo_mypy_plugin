"""Name the plugin in an exception that escapes it, instead of letting it pass for mypy's.

mypy calls ``get_additional_deps`` and ``report_config_data`` outside the context in which
it reports the crash of a hook: an exception there ends mypy with an INTERNAL ERROR, a
link to mypy's issue tracker and, without ``--show-traceback``, no message at all. The
plugin's entry points that read addons from disk (these two, and the registry refresh)
print one line that names the plugin and the exception, then raise it again, so mypy's
exit status and traceback stay the same. The hooks mypy asks for per name or expression
are not wrapped: mypy reports their crashes with a file and a line, and they run some
200,000 times a run.
"""

from __future__ import annotations

import sys

from mypy.errors import CompileError

__all__ = ["report_internal_error"]

PREFIX = "odoo-typing: internal error while "


def report_internal_error(exc: Exception, doing: str) -> None:
    """Print one line about an exception the plugin did not expect, once per exception.

    The line is also added to the exception's notes: an entry point that calls another
    (``get_additional_deps``, the registry refresh) reports it only once.

    Args:
        exc: The exception, which the caller raises again.
        doing: What the plugin was doing, for "internal error while <doing>".
    """
    if isinstance(exc, CompileError):
        return  # mypy's own way to stop with errors: mypy prints them
    if any(note.startswith(PREFIX) for note in getattr(exc, "__notes__", ())):
        return
    line = f"{PREFIX}{doing}: {type(exc).__name__}: {exc} (please report it to odoo-typing)"
    exc.add_note(line)
    print(line, file=sys.stderr)
