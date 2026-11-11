"""Unit tests of the regression benchmark script (tools/bench/bench.py)."""

from __future__ import annotations

from pathlib import Path

import bench


def _report(label: str, plugin_only: list[str]) -> bench.Report:
    addon = bench.AddonReport(
        exit_status=1,
        warm_exit_status=1,
        cold_s=2.0,
        warm_s=0.5,
        rss_cold_mb=100.0,
        rss_warm_mb=50.0,
        total_errors=len(plugin_only),
        warm_matches_cold=True,
        errors=plugin_only,
        plugin_only=plugin_only,
        crash="",
    )
    return bench.Report(
        label=label,
        plugin=True,
        created="",
        git_commit="abc",
        mypy_version="",
        odoo_root="",
        addons={"crm": addon},
    )


def test_parse_errors_mixed_output_keeps_normalized_errors_only(tmp_path: Path) -> None:
    package = tmp_path / "real_odoo"
    package.mkdir()
    (tmp_path / "root").mkdir()
    (tmp_path / "root" / "odoo").symlink_to(package)
    stdout = "\n".join(
        [
            'odoo/addons/crm/a.py:3: error: "Lead" has no attribute "x"  [attr-defined]',
            "odoo/addons/crm/a.py:3: note: See https://example.invalid",
            f"{package}/addons/crm/b.py:7: error: Something: odd",
            f"{tmp_path}/root/odoo/c.py:1: error: Bad  [misc]",
        ]
    )

    errors = bench.parse_errors(stdout, tmp_path / "root")

    assert errors == [
        'odoo/addons/crm/a.py:3:attr-defined:"Lead" has no attribute "x"',
        "odoo/addons/crm/b.py:7:-:Something: odd",
        "odoo/c.py:1:misc:Bad",
    ]


def test_group_errors_quoted_names_and_numbers_share_one_pattern() -> None:
    errors = [
        'a.py:1:attr-defined:"Lead" has no attribute "x"',
        'b.py:2:attr-defined:"Team" has no attribute "y"',
        "c.py:3:misc:Dict entry 0 is bad",
        "c.py:4:misc:Dict entry 1 is bad",
    ]

    groups = bench.group_errors(errors)

    assert list(groups) == [
        ("attr-defined", '"_" has no attribute "_"'),
        ("misc", "Dict entry N is bad"),
    ]
    assert groups["attr-defined", '"_" has no attribute "_"'] == errors[:2]


def test_diff_reports_changed_plugin_only_lists_new_and_gone() -> None:
    old = _report("old", ["a.py:1:misc:Gone one", "a.py:2:misc:Kept"])
    new = _report("new", ["a.py:2:misc:Kept", 'a.py:5:attr-defined:"X" has no attribute "y"'])

    lines = bench.diff_reports(old, new, baseline=None)

    text = "\n".join(lines)
    assert "crm: plugin-only NEW: 1" in text
    assert '[attr-defined] x1: "_" has no attribute "_"' in text
    assert "crm: plugin-only GONE: 1\n  [misc] x1: Gone one" in text
    assert "Kept" not in text


def test_run_mypy_crashing_process_reports_status_and_output(tmp_path: Path) -> None:
    fake_python = tmp_path / "python"
    fake_python.write_text("#!/bin/sh\necho 'x.py:1: error: boom  [misc]'\necho oops >&2\nexit 2\n")
    fake_python.chmod(0o755)
    context = bench.BenchContext(
        python=str(fake_python), odoo_root=tmp_path, bench_dir=tmp_path, timeout=60
    )

    result = bench.run_mypy(context, tmp_path / "cfg.toml", tmp_path / "cache", "odoo")

    assert (result.exit_status, result.stdout, result.stderr) == (
        2,
        "x.py:1: error: boom  [misc]\n",
        "oops\n",
    )
    assert result.peak_rss_mb > 0


# -- several Odoo versions (roadmap V2) ---------------------------------------------------------


def test_parse_args_default_version_keeps_existing_layout() -> None:
    args = bench.parse_args(["--label", "x"])

    assert args.odoo_version == 18
    assert args.odoo_root == bench.DEFAULT_ODOO_ROOT
    assert args.bench_dir == bench.DEFAULT_BENCH_DIR


def test_parse_args_other_version_uses_fetched_root_and_own_dir(tmp_path: Path) -> None:
    args = bench.parse_args(["--label", "x", "--odoo-version", "19", "--bench-dir", str(tmp_path)])

    assert args.odoo_root == bench.fetched_root(19)
    assert args.bench_dir == tmp_path / "odoo-19"


def test_parse_args_explicit_root_wins(tmp_path: Path) -> None:
    args = bench.parse_args(["--label", "x", "--odoo-version", "17", "--odoo-root", str(tmp_path)])

    assert args.odoo_root == tmp_path


def test_write_config_names_the_odoo_version(tmp_path: Path) -> None:
    context = bench.BenchContext(
        python="python", odoo_root=tmp_path, bench_dir=tmp_path, timeout=1, odoo_version=20
    )

    text = bench.write_config(context, with_plugin=True).read_text()

    assert "odoo_version = 20\n" in text


def test_write_config_namespace_odoo_uses_explicit_package_bases(tmp_path: Path) -> None:
    def config(version: int) -> str:
        context = bench.BenchContext(
            python="python",
            odoo_root=tmp_path,
            bench_dir=tmp_path / str(version),
            timeout=1,
            odoo_version=version,
        )
        return bench.write_config(context, with_plugin=False).read_text()

    assert "explicit_package_bases = true" not in config(18)
    assert "explicit_package_bases = true" in config(19)


def test_write_config_adds_plugin_options_only_with_plugin(tmp_path: Path) -> None:
    context = bench.BenchContext(
        "python", tmp_path, tmp_path, 1.0, options=("literal_selection = true",)
    )

    with_plugin = bench.write_config(context, with_plugin=True).read_text()
    without = bench.write_config(context, with_plugin=False).read_text()

    assert with_plugin.endswith("addons_path = []\nliteral_selection = true\n")
    assert "literal_selection" not in without
