"""Regression benchmark: run mypy with and without the plugin on real Odoo addons.

Every addon is its own mypy target, so the numbers of different addons do not mix.
For each addon the script runs mypy twice with a fresh cache directory (cold run, then
warm run on the same cache) and records wall time, peak RSS, the exit status and the
normalized errors (``path:line:code:message``, path relative to the Odoo root).
Errors present with the plugin but absent without it are the *plugin-only* errors.

The run without the plugin (``noplugin.json``) is computed once and reused, because the
plugin code does not change it; pass ``--refresh-baseline`` after upgrading mypy or Odoo.

Usage::

    uv run python tools/bench/bench.py --label baseline
    uv run python tools/bench/bench.py --label wp1 --compare baseline
    uv run python tools/bench/bench.py --compare-only wp1 --compare baseline  # no mypy run

Exit status: 1 if a mypy run crashed (exit status 2, e.g. an INTERNAL ERROR), else 0.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from collections import defaultdict
from collections.abc import Iterable, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TypedDict, cast

__all__ = [
    "AddonReport",
    "BenchContext",
    "BenchError",
    "Report",
    "RunResult",
    "diff_reports",
    "group_errors",
    "main",
    "parse_errors",
]

SCRATCH = Path(
    "/tmp/claude-1000/-home-whoami-odoo-typing/b1f32af2-e055-42a6-9b2e-eb618a6e734f/scratchpad"
)
DEFAULT_ODOO_ROOT = SCRATCH / "smoke" / "odoo_root"
DEFAULT_BENCH_DIR = SCRATCH / "bench"
DEFAULT_ADDONS = ("crm", "sale", "account", "website_sale", "project_sms", "sale_stock")
BASELINE_LABEL = "noplugin"
REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLES_PER_GROUP = 3
CRASH_TAIL_LINES = 40

ERROR_LINE = re.compile(
    r"^(?P<path>.+?):(?P<line>\d+): error: (?P<message>.*?)(?:  \[(?P<code>[a-z0-9-]+)\])?$"
)
QUOTED = re.compile(r'"[^"]*"')
NUMBER = re.compile(r"\b\d+\b")

CONFIG_TEMPLATE = """\
[tool.mypy]
plugins = [{plugins}]
mypy_path = ["{odoo_root}"]
check_untyped_defs = true
show_error_codes = true
show_traceback = true
error_summary = false

[[tool.mypy.overrides]]
module = ["odoo", "odoo.*"]
follow_imports = "silent"

[tool.odoo_typing]
odoo_path = "{odoo_root}/odoo"
addons_path = []
"""


class BenchError(Exception):
    """Raised for invalid arguments or unreadable benchmark files."""


class AddonReport(TypedDict):
    """Measurements for one addon (one mypy target)."""

    exit_status: int
    warm_exit_status: int
    cold_s: float
    warm_s: float
    rss_cold_mb: float
    rss_warm_mb: float
    total_errors: int
    warm_matches_cold: bool
    errors: list[str]
    plugin_only: list[str]
    crash: str


class Report(TypedDict):
    """One benchmark run, stored as ``<bench dir>/<label>.json``."""

    label: str
    plugin: bool
    created: str
    git_commit: str
    mypy_version: str
    odoo_root: str
    addons: dict[str, AddonReport]


@dataclass(frozen=True, slots=True)
class RunResult:
    """Outcome of one mypy process.

    Attributes:
        seconds: Wall time.
        peak_rss_mb: Peak resident set size of the mypy process.
        exit_status: mypy's exit status (0 clean, 1 errors, 2 crash or usage error).
        stdout: Captured standard output.
        stderr: Captured standard error.
    """

    seconds: float
    peak_rss_mb: float
    exit_status: int
    stdout: str
    stderr: str


@dataclass(frozen=True, slots=True)
class BenchContext:
    """Paths and settings shared by every run of one invocation."""

    python: str
    odoo_root: Path
    bench_dir: Path
    timeout: float

    @property
    def config_dir(self) -> Path:
        """Directory of the generated mypy configs."""
        return self.bench_dir / "config"

    def report_path(self, label: str) -> Path:
        """Return the JSON file of a label."""
        return self.bench_dir / f"{label}.json"


# -- running mypy ------------------------------------------------------------------------


def write_config(context: BenchContext, with_plugin: bool) -> Path:
    """Generate the mypy config file for runs with or without the plugin.

    Args:
        context: Benchmark paths.
        with_plugin: Whether the config enables ``odoo_typing.plugin``.

    Returns:
        Path of the written ``.toml`` file.
    """
    context.config_dir.mkdir(parents=True, exist_ok=True)
    name = "plugin.toml" if with_plugin else "noplugin.toml"
    path = context.config_dir / name
    plugins = '"odoo_typing.plugin"' if with_plugin else ""
    path.write_text(CONFIG_TEMPLATE.format(plugins=plugins, odoo_root=context.odoo_root))
    return path


def run_mypy(context: BenchContext, config: Path, cache_dir: Path, target: str) -> RunResult:
    """Run one mypy process and measure it.

    ``os.wait4`` gives the resource usage of exactly this child, so the peak RSS is
    per run (``getrusage(RUSAGE_CHILDREN)`` would report the maximum over all runs).

    Args:
        context: Benchmark paths and interpreter.
        config: mypy config file.
        cache_dir: Cache directory (kept between the cold and the warm run).
        target: Target path relative to the Odoo root, e.g. ``odoo/addons/crm``.

    Returns:
        The measurements and captured output.
    """
    command = [context.python, "-m", "mypy", "--config-file", str(config)]
    command += ["--cache-dir", str(cache_dir), target]
    with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
        start = time.perf_counter()
        process = subprocess.Popen(command, cwd=context.odoo_root, stdout=out, stderr=err)
        timer = threading.Timer(context.timeout, process.kill)
        timer.start()
        try:
            _, status, usage = os.wait4(process.pid, 0)
        finally:
            timer.cancel()
        seconds = time.perf_counter() - start
        process.returncode = os.waitstatus_to_exitcode(status)  # already reaped above
        out.seek(0)
        err.seek(0)
        return RunResult(
            seconds=round(seconds, 2),
            peak_rss_mb=round(usage.ru_maxrss / 1024, 1),  # Linux reports KiB
            exit_status=process.returncode,
            stdout=out.read().decode(errors="replace"),
            stderr=err.read().decode(errors="replace"),
        )


def parse_errors(stdout: str, odoo_root: Path) -> list[str]:
    """Extract the error lines of mypy's output in normalized form.

    Args:
        stdout: mypy's standard output.
        odoo_root: Directory that contains the ``odoo`` package (or its symlink).

    Returns:
        ``path:line:code:message`` entries in output order (duplicates kept); notes
        are dropped and errors without a code get ``-``.
    """
    errors = []
    for line in stdout.splitlines():
        match = ERROR_LINE.match(line)
        if match is None:
            continue
        path = _relative_path(match["path"], odoo_root)
        errors.append(f"{path}:{match['line']}:{match['code'] or '-'}:{match['message']}")
    return errors


def _relative_path(path: str, odoo_root: Path) -> str:
    candidate = Path(path)
    if not candidate.is_absolute():
        return path
    package = (odoo_root / "odoo").resolve()
    for root, prefix in ((odoo_root, Path()), (package, Path("odoo"))):
        if candidate.is_relative_to(root):
            return str(prefix / candidate.relative_to(root))
    return path


def measure_addon(context: BenchContext, label: str, addon: str, with_plugin: bool) -> AddonReport:
    """Run the cold and the warm mypy run of one addon.

    Args:
        context: Benchmark paths.
        label: Label of this benchmark run (names the cache directory).
        addon: Odoo addon name, e.g. ``crm``.
        with_plugin: Whether to enable the plugin.

    Returns:
        The addon's measurements; ``plugin_only`` is filled in later.
    """
    config = context.config_dir / ("plugin.toml" if with_plugin else "noplugin.toml")
    cache_dir = context.bench_dir / "cache" / label / addon
    shutil.rmtree(cache_dir, ignore_errors=True)
    target = f"odoo/addons/{addon}"
    cold = run_mypy(context, config, cache_dir, target)
    warm = run_mypy(context, config, cache_dir, target)
    errors = parse_errors(cold.stdout, context.odoo_root)
    crashed = [result for result in (cold, warm) if result.exit_status not in (0, 1)]
    crash = "" if not crashed else _tail(crashed[0].stdout + crashed[0].stderr)
    print(
        f"  {label}/{addon}: {len(errors)} errors, cold {cold.seconds}s, "
        f"warm {warm.seconds}s, exit {cold.exit_status}/{warm.exit_status}",
        flush=True,
    )
    return AddonReport(
        exit_status=cold.exit_status,
        warm_exit_status=warm.exit_status,
        cold_s=cold.seconds,
        warm_s=warm.seconds,
        rss_cold_mb=cold.peak_rss_mb,
        rss_warm_mb=warm.peak_rss_mb,
        total_errors=len(errors),
        warm_matches_cold=parse_errors(warm.stdout, context.odoo_root) == errors,
        errors=sorted(set(errors)),
        plugin_only=[],
        crash=crash,
    )


def _tail(text: str) -> str:
    return "\n".join(text.splitlines()[-CRASH_TAIL_LINES:])


def measure_all(
    context: BenchContext, label: str, addons: Sequence[str], with_plugin: bool, jobs: int
) -> dict[str, AddonReport]:
    """Measure several addons, ``jobs`` at a time (1 gives the most stable timings).

    Args:
        context: Benchmark paths.
        label: Label of this benchmark run.
        addons: Addon names.
        with_plugin: Whether to enable the plugin.
        jobs: Number of addons measured concurrently.

    Returns:
        Measurements by addon name, in the order of ``addons``.
    """
    write_config(context, with_plugin)
    with ThreadPoolExecutor(max_workers=jobs) as pool:
        results = pool.map(lambda addon: measure_addon(context, label, addon, with_plugin), addons)
        return dict(zip(addons, results, strict=True))


# -- reports -----------------------------------------------------------------------------


def new_report(context: BenchContext, label: str, with_plugin: bool) -> Report:
    """Create an empty report with the metadata of this environment."""
    return Report(
        label=label,
        plugin=with_plugin,
        created=datetime.now(UTC).isoformat(timespec="seconds"),
        git_commit=_git_commit(),
        mypy_version=_mypy_version(context.python),
        odoo_root=str(context.odoo_root),
        addons={},
    )


def _git_commit() -> str:
    def git(*args: str) -> str:
        result = subprocess.run(
            ["git", *args], cwd=REPO_ROOT, capture_output=True, text=True, check=False
        )
        return result.stdout.strip()

    # Only the plugin sources change the measured behaviour.
    dirty = "+dirty" if git("status", "--porcelain", "--", "src") else ""
    return git("rev-parse", "--short", "HEAD") + dirty


def _mypy_version(python: str) -> str:
    result = subprocess.run(
        [python, "-m", "mypy", "--version"], capture_output=True, text=True, check=False
    )
    return result.stdout.strip()


def load_report(path: Path) -> Report:
    """Read a report written by :func:`save_report`.

    Args:
        path: The JSON file.

    Returns:
        The report.

    Raises:
        BenchError: If the file is missing or is not a benchmark report.
    """
    try:
        # json.loads() returns Any: this is the script's only untyped boundary.
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as error:
        raise BenchError(f"cannot read benchmark report {path}: {error}") from error
    if not isinstance(data, dict) or not isinstance(data.get("addons"), dict):
        raise BenchError(f"{path} is not a benchmark report")
    return cast("Report", data)


def save_report(path: Path, report: Report) -> None:
    """Write a report as indented JSON."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=1) + "\n")


def baseline_report(
    context: BenchContext, addons: Sequence[str], refresh: bool, jobs: int
) -> Report:
    """Return the run without the plugin, measuring only the addons it lacks.

    Args:
        context: Benchmark paths.
        addons: Addons that must be present.
        refresh: Discard the stored baseline and measure everything again.
        jobs: Number of addons measured concurrently.

    Returns:
        The (possibly extended) baseline, saved to ``noplugin.json``.
    """
    path = context.report_path(BASELINE_LABEL)
    if path.exists() and not refresh:
        report = load_report(path)
    else:
        report = new_report(context, BASELINE_LABEL, with_plugin=False)
    missing = [addon for addon in addons if addon not in report["addons"]]
    if missing:
        print(f"measuring without the plugin: {', '.join(missing)}", flush=True)
        report["addons"].update(measure_all(context, BASELINE_LABEL, missing, False, jobs))
        save_report(path, report)
    return report


def add_plugin_only(report: Report, baseline: Report) -> None:
    """Fill ``plugin_only``: errors with the plugin that the baseline does not have."""
    for addon, result in report["addons"].items():
        without = set(baseline["addons"][addon]["errors"])
        result["plugin_only"] = [error for error in result["errors"] if error not in without]


# -- comparison --------------------------------------------------------------------------


def group_errors(errors: Iterable[str]) -> dict[tuple[str, str], list[str]]:
    """Group normalized errors by error code and message pattern.

    The pattern is the message with every quoted part replaced by ``"_"`` and every
    number by ``N``, so ``"Partner" has no attribute "x"`` and ``"Lead" has no
    attribute "y"`` match, and so do ``Argument 1 ...`` and ``Argument 2 ...``.

    Args:
        errors: Normalized ``path:line:code:message`` entries.

    Returns:
        Errors by ``(code, pattern)``, largest group first.
    """
    groups: dict[tuple[str, str], list[str]] = defaultdict(list)
    for error in errors:
        _, _, code, message = error.split(":", 3)
        pattern = NUMBER.sub("N", QUOTED.sub('"_"', message))
        groups[code, pattern].append(error)
    return dict(sorted(groups.items(), key=lambda item: (-len(item[1]), item[0])))


def diff_reports(old: Report, new: Report, baseline: Report | None) -> list[str]:
    """Describe how ``new`` differs from ``old``, addon by addon.

    Args:
        old: The earlier report.
        new: The report to judge.
        baseline: The run without the plugin, for the plugin's time overhead.

    Returns:
        Printable lines.
    """
    lines = [f"{new['label']} ({new['git_commit']}) vs {old['label']} ({old['git_commit']})"]
    lines.append(_summary_header())
    for addon, result in new["addons"].items():
        previous = old["addons"].get(addon)
        reference = baseline["addons"].get(addon) if baseline else None
        lines.append(_summary_row(addon, result, previous, reference))
    for addon, result in new["addons"].items():
        previous = old["addons"].get(addon)
        if previous is None:
            continue
        before, after = set(previous["plugin_only"]), set(result["plugin_only"])
        lines += _group_lines(f"{addon}: plugin-only NEW", sorted(after - before))
        lines += _group_lines(f"{addon}: plugin-only GONE", sorted(before - after))
    return lines


def _summary_header() -> str:
    columns = ("addon", "exit", "errors", "plugin-only", "cold s", "warm s", "rss MB")
    return "{:<14} {:>5} {:>13} {:>13} {:>19} {:>15} {:>13}".format(*columns)


def _summary_row(
    addon: str, result: AddonReport, previous: AddonReport | None, reference: AddonReport | None
) -> str:
    def pair(key: str, value: float) -> str:
        old = previous.get(key) if previous else None
        return f"{old}->{value}" if isinstance(old, int | float) and old != value else str(value)

    overhead = ""
    if reference and reference["cold_s"]:
        overhead = f" (x{result['cold_s'] / reference['cold_s']:.1f})"
    exit_status = f"{result['exit_status']}/{result['warm_exit_status']}"
    if not result["warm_matches_cold"]:
        exit_status += "!"
    return "{:<14} {:>5} {:>13} {:>13} {:>19} {:>15} {:>13}".format(
        addon,
        exit_status,
        pair("total_errors", result["total_errors"]),
        pair("plugin_only", len(result["plugin_only"])),
        pair("cold_s", result["cold_s"]) + overhead,
        pair("warm_s", result["warm_s"]),
        pair("rss_cold_mb", result["rss_cold_mb"]),
    )


def _group_lines(title: str, errors: list[str]) -> list[str]:
    if not errors:
        return []
    lines = [f"\n{title}: {len(errors)}"]
    for (code, pattern), members in group_errors(errors).items():
        lines.append(f"  [{code}] x{len(members)}: {pattern}")
        lines += [f"      {example}" for example in members[:EXAMPLES_PER_GROUP]]
    return lines


# -- command line ------------------------------------------------------------------------


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    """Parse the command line (see the module docstring)."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--label", help="name of this run; writes <bench-dir>/<label>.json")
    parser.add_argument("--compare", help="earlier label to diff against")
    parser.add_argument("--compare-only", help="diff this stored label, do not run mypy")
    parser.add_argument("--addons", nargs="+", default=list(DEFAULT_ADDONS))
    parser.add_argument("--refresh-baseline", action="store_true")
    parser.add_argument("--odoo-root", type=Path, default=DEFAULT_ODOO_ROOT)
    parser.add_argument("--bench-dir", type=Path, default=DEFAULT_BENCH_DIR)
    parser.add_argument("--python", default=sys.executable, help="interpreter with mypy")
    parser.add_argument("--jobs", type=int, default=1, help="addons measured at once")
    parser.add_argument("--timeout", type=float, default=1800, help="seconds per mypy run")
    args = parser.parse_args(argv)
    if bool(args.label) == bool(args.compare_only):
        parser.error("give exactly one of --label and --compare-only")
    if args.label == BASELINE_LABEL:
        parser.error(f"label {BASELINE_LABEL!r} is reserved for the run without the plugin")
    return args


def run(args: argparse.Namespace) -> Report:
    """Measure the baseline (if needed) and the plugin; save and return the plugin run."""
    context = BenchContext(
        python=args.python,
        odoo_root=args.odoo_root.absolute(),
        bench_dir=args.bench_dir.absolute(),
        timeout=args.timeout,
    )
    if not (context.odoo_root / "odoo" / "release.py").is_file():
        raise BenchError(f"{context.odoo_root} does not contain odoo/release.py")
    baseline = baseline_report(context, args.addons, args.refresh_baseline, args.jobs)
    print(f"measuring with the plugin: {', '.join(args.addons)}", flush=True)
    report = new_report(context, args.label, with_plugin=True)
    report["addons"] = measure_all(context, args.label, args.addons, True, args.jobs)
    add_plugin_only(report, baseline)
    save_report(context.report_path(args.label), report)
    return report


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point.

    Args:
        argv: Command line arguments without the program name.

    Returns:
        1 if a mypy run crashed, else 0.
    """
    args = parse_args(sys.argv[1:] if argv is None else argv)
    bench_dir = args.bench_dir.absolute()
    try:
        report = run(args) if args.label else load_report(bench_dir / f"{args.compare_only}.json")
        baseline_path = bench_dir / f"{BASELINE_LABEL}.json"
        baseline = load_report(baseline_path) if baseline_path.exists() else None
        old = load_report(bench_dir / f"{args.compare}.json") if args.compare else report
    except BenchError as error:
        print(f"error: {error}", file=sys.stderr)
        return 2
    print("\n".join(diff_reports(old, report, baseline)))
    crashes = [name for name, result in report["addons"].items() if result["crash"]]
    for name in crashes:
        print(f"\nCRASH in {name}:\n{report['addons'][name]['crash']}")
    return 1 if crashes else 0


if __name__ == "__main__":
    sys.exit(main())
