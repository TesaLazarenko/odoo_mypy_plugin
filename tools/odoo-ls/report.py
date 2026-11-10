"""Turn the odoo-ls `--parse` JSON output into a readable report.

Usage: python3 tools/odoo-ls/report.py OUTPUT_JSON [TRACKED_DIR ...]

Output JSON shape (odoo-ls 1.6.x):
    {"events": [
        {"type": "diagnostic", "uri": "file:///...py", "version": null,
         "diagnostics": [{"range": {"start": {"line": 7, "character": 4}, ...},
                          "severity": 1, "code": "OLS03016", "message": "..."}]},
        {"type": "log", "severity": 3, "message": "..."}]}
Lines in "range" are 0-based; LSP severity: 1=error 2=warning 3=info 4=hint.

Files may contain markers like `# seeded: <description>` on lines with a
deliberately planted error; for those the report also says which planted
errors odoo-ls caught and which it missed.

Exit code: 1 if any error-severity diagnostic was found, else 0
(odoo-ls itself always exits 0, so CI should rely on this script).
"""

from __future__ import annotations

import json
import re
import sys
from collections import defaultdict
from pathlib import Path
from urllib.parse import unquote, urlparse

SEVERITY = {1: "error", 2: "warning", 3: "info", 4: "hint"}
SEEDED = re.compile(r"#\s*seeded:\s*(.+)$")


def uri_to_path(uri: str) -> Path:
    return Path(unquote(urlparse(uri).path))


def load_diagnostics(output: Path, tracked: list[Path]) -> dict[Path, list[dict]]:
    events = json.loads(output.read_text())["events"]
    result: dict[Path, list[dict]] = defaultdict(list)
    for event in events:
        if event.get("type") != "diagnostic":
            continue
        path = uri_to_path(event["uri"])
        if tracked and not any(path.is_relative_to(t) for t in tracked):
            continue
        result[path].extend(event["diagnostics"])
    return result


def seeded_lines(path: Path) -> dict[int, str]:
    """1-based line number -> description of the planted error."""
    if path.suffix != ".py" or not path.is_file():
        return {}
    marks = {}
    for number, line in enumerate(path.read_text().splitlines(), start=1):
        if match := SEEDED.search(line):
            marks[number] = match.group(1).strip()
    return marks


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__, file=sys.stderr)
        return 2
    output = Path(argv[1])
    tracked = [Path(p).resolve() for p in argv[2:]]
    by_file = load_diagnostics(output, tracked)

    errors = 0
    print("== odoo-ls diagnostics ==")
    for path in sorted(by_file):
        for diag in sorted(by_file[path], key=lambda d: d["range"]["start"]["line"]):
            start = diag["range"]["start"]
            severity = SEVERITY.get(diag.get("severity", 1), "?")
            errors += severity == "error"
            print(
                f"{path}:{start['line'] + 1}:{start['character'] + 1}: "
                f"{severity} {diag.get('code', '')} {diag['message']}"
            )
    if not any(by_file.values()):
        print("(none)")

    # Planted-error coverage, for every tracked .py file that has markers.
    files = {p for t in tracked for p in t.rglob("*.py")} if tracked else set(by_file)
    caught_total = planted_total = 0
    for path in sorted(files):
        marks = seeded_lines(path)
        if not marks:
            continue
        hits = defaultdict(list)
        for diag in by_file.get(path, []):
            hits[diag["range"]["start"]["line"] + 1].append(diag.get("code", "?"))
        print(f"\n== planted errors in {path} ==")
        for line, description in marks.items():
            codes = hits.get(line)
            mark = "CAUGHT " + ",".join(codes) if codes else "missed"
            print(f"  line {line:>3}  {mark:<18} {description}")
        caught = sum(1 for line in marks if line in hits)
        caught_total += caught
        planted_total += len(marks)
    if planted_total:
        print(f"\nodoo-ls caught {caught_total} of {planted_total} planted errors")

    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
