"""``odoo-typing-baseline``: adopt the plugin gradually, reporting only new errors.

mypy has no baseline mode, so this wraps it::

    odoo-typing-baseline record -- -p odoo.addons.my_addon   # save today's errors
    odoo-typing-baseline check -- -p odoo.addons.my_addon    # report only new ones

An error is keyed ``path:code:message`` without its line number, which shifts with every
edit above it; the baseline counts each key, so a second identical error in a file is
new. ``check`` prints the new errors in mypy's format and exits 1 if there are any (0
otherwise); fixed errors are only counted, so ``record`` again to drop them.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path
from typing import Final

from mypy import api as mypy_api

__all__ = ["DEFAULT_BASELINE", "error_keys", "main", "new_errors"]

DEFAULT_BASELINE: Final = Path("odoo-typing-baseline.json")
ERROR: Final = re.compile(r"^(?P<path>[^:\n]+):(?P<line>\d+)(?::\d+)?: error: (?P<rest>.*)$")
CODE: Final = re.compile(r"  \[(?P<code>[a-z0-9-]+)\]$")

MypyRun = Callable[[list[str]], tuple[str, str, int]]


def error_keys(lines: Iterable[str]) -> list[tuple[str, str]]:
    """The errors of mypy's output: each with its key (``path:code:message``) and line.

    Notes and the summary line are dropped; an error without a code gets ``-``.
    """
    found: list[tuple[str, str]] = []
    for line in lines:
        match = ERROR.match(line)
        if match is None:
            continue
        rest = match["rest"]
        code_match = CODE.search(rest)
        code = code_match["code"] if code_match else "-"
        message = rest[: code_match.start()] if code_match else rest
        found.append((f"{match['path']}:{code}:{message}", line))
    return found


def new_errors(lines: Iterable[str], baseline: Counter[str]) -> list[str]:
    """The output lines of the errors beyond what the baseline counts for their key."""
    seen: Counter[str] = Counter()
    new: list[str] = []
    for key, line in error_keys(lines):
        seen[key] += 1
        if seen[key] > baseline[key]:
            new.append(line)
    return new


def main(argv: Sequence[str] | None = None, run: MypyRun = mypy_api.run) -> int:
    """Command-line entry point of ``odoo-typing-baseline``.

    Args:
        argv: Arguments, without the program name (default: ``sys.argv[1:]``).
        run: Runs mypy with arguments (``mypy.api.run``).

    Returns:
        ``record``: 0, or mypy's status if it failed to run (2). ``check``: 1 if there
        are new errors, 0 if not, 2 on a missing baseline or a mypy failure.
    """
    parser = argparse.ArgumentParser(
        prog="odoo-typing-baseline",
        description="Record mypy's errors once, then report only new ones.",
    )
    parser.add_argument("command", choices=["record", "check"])
    parser.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    arguments = list(sys.argv[1:] if argv is None else argv)
    split = arguments.index("--") if "--" in arguments else len(arguments)
    args = parser.parse_args(arguments[:split])  # mypy's own arguments follow "--"
    stdout, stderr, status = run(arguments[split + 1 :])
    if status not in (0, 1):
        sys.stdout.write(stdout)
        sys.stderr.write(stderr)
        return status
    lines = stdout.splitlines()
    if args.command == "record":
        counts = Counter(key for key, _ in error_keys(lines))
        args.baseline.write_text(json.dumps(dict(sorted(counts.items())), indent=1) + "\n")
        print(f"odoo-typing-baseline: recorded {sum(counts.values())} errors in {args.baseline}")
        return 0
    try:
        baseline = Counter(json.loads(args.baseline.read_text()))
    except (OSError, ValueError, TypeError) as exc:
        print(f"odoo-typing-baseline: error: cannot read {args.baseline}: {exc}", file=sys.stderr)
        return 2
    new = new_errors(lines, baseline)
    for line in new:
        print(line)
    known = len(error_keys(lines)) - len(new)
    print(f"odoo-typing-baseline: {len(new)} new errors ({known} in the baseline)")
    return 1 if new else 0


if __name__ == "__main__":
    sys.exit(main())
