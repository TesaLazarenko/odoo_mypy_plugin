"""Name the plugin in an exception that escapes it, instead of letting it pass for mypy's.

mypy calls ``get_additional_deps`` and ``report_config_data`` outside the context in which
it reports the crash of a hook: an exception there ends mypy with an INTERNAL ERROR, a
link to mypy's issue tracker and, without ``--show-traceback``, no message at all. The
plugin's entry points that read addons from disk (these two, and the registry refresh)
print one line that names the plugin and the exception, then raise it again, so mypy's
exit status and traceback stay the same.

The hooks mypy asks for per name or expression fail softly (:func:`fail_soft`): an
exception there would end the whole run with an INTERNAL ERROR that names mypy, also when
it happens in a file of Odoo whose errors are not even shown, and hide the user's own
errors. The hook's result is then what mypy would have used without the plugin, and one
line per kind of failure names the plugin, the file and the line. With
``ODOO_TYPING_RAISE=1`` (the test suite) the exception goes through as before.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Callable
from typing import Final, TypeVar

from mypy.errors import CompileError

__all__ = ["RAISE_VARIABLE", "fail_soft", "report_internal_error"]

PREFIX = "odoo-typing: internal error while "
RAISE_VARIABLE: Final = "ODOO_TYPING_RAISE"

ContextT = TypeVar("ContextT")
ResultT = TypeVar("ResultT")

# (the function behind a hook, exception class) reported in this process: one function
# serves many names (``create`` / ``write`` / ``copy`` of every class).
_reported: set[tuple[str, str]] = set()


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


def fail_soft(
    hook: Callable[[ContextT], ResultT],
    fallback: Callable[[ContextT], ResultT],
    what: str,
) -> Callable[[ContextT], ResultT]:
    """A hook that gives ``fallback(ctx)`` (mypy's own result) when it raises.

    Args:
        hook: The hook.
        fallback: What mypy would use without the hook (``ctx.default_return_type``).
        what: How the message names the hook (``the method hook of create``).
    """
    if os.environ.get(RAISE_VARIABLE):
        return hook
    function: object = hook
    while hasattr(function, "func"):  # functools.partial
        function = function.func
    name = getattr(function, "__qualname__", what)

    def soft(ctx: ContextT) -> ResultT:
        try:
            return hook(ctx)
        except CompileError:
            raise  # mypy's own way to stop with errors
        except Exception as exc:  # pylint: disable=broad-exception-caught
            _report_soft_failure(exc, name, what, ctx)
            return fallback(ctx)

    return soft


def _report_soft_failure(exc: Exception, name: str, what: str, ctx: object) -> None:
    key = (name, type(exc).__name__)
    if key in _reported:
        return
    _reported.add(key)
    print(
        f"{PREFIX}running {what} at {_where(ctx)}: {type(exc).__name__}: {exc} "
        "(its result is mypy's own there; please report it to odoo-typing)",
        file=sys.stderr,
    )


def _where(ctx: object) -> str:
    """``path:line`` of the node a hook was called for, as far as the context says."""
    api = getattr(ctx, "api", None)
    path = getattr(api, "path", None)
    if path is None:
        module = getattr(api, "cur_mod_node", None)
        path = getattr(module, "path", None)
    node = getattr(ctx, "context", None) or getattr(ctx, "cls", None)
    line = getattr(node, "line", None)
    return f"{path or '?'}:{line if line is not None else '?'}"
