# Development

[← README](../README.md)

## Setup and checks

```bash
uv sync
uv run pytest -n 6       # needs Odoo installed; set ODOO_PATH=/path/to/odoo if not auto-found
uv run ruff check src tests tools && uv run ruff format --check src tests tools && uv run pylint src tests tools && uv run mypy
```

The end-to-end tests run the real mypy on throw-away addons against the installed Odoo,
also with two runs on one cache and through `dmypy`. They fail when mypy crashes
(`INTERNAL ERROR`, exit status 2), not only on wrong messages.

`-n <workers>` (pytest-xdist) runs them in parallel; without it they run one by one.
Each worker has its own temporary directories and mypy cache (the session fixtures are
per worker), so each analyzes Odoo's own modules once. On Odoo 19 (14 CPUs): 287 s one
by one, 160 s with `-n 2`, 90 s with `-n 4`, 68 s with `-n 6` (2 GB in all), 58 s with
`-n 8`. A worker takes 0.4-0.7 GB, so mind the memory of the machine more than its CPUs.

## Other Odoo versions

`tools/odoo/fetch.sh` makes a shallow, sparse checkout (no `static/` or `i18n/`, about
150-200 MB) under `~/.cache/odoo-typing/odoo/<version>/` and prints the `odoo` package
directory to test against:

```bash
ODOO_PATH=$(tools/odoo/fetch.sh 19.0) uv run pytest
```

- The tests read the version from its `release.py` (or `ODOO_VERSION`). For a version
  the plugin has no profile for yet, the end-to-end tests are skipped.
- `@pytest.mark.odoo(18, 19)` limits a test to some versions.
- In CI, set `ODOO_REQUIRED=1`: an Odoo that is not found (or whose version cannot be
  read) then fails the end-to-end tests instead of skipping them silently.

## Regression benchmark

[tools/bench/bench.py](../tools/bench/bench.py) runs mypy on real Odoo core addons
(default: `crm sale account website_sale project_sms sale_stock`, each as its own
target), with and without the plugin. For every addon it records the cold and warm wall
time (fresh cache, then the same cache again), the peak RSS, the exit status and the
normalized errors.

- *Plugin-only* errors are those that appear only with the plugin.
- Each run has its own `scan_cache_dir` (`XDG_CACHE_HOME` inside its cache directory),
  so a cold run scans every addon.
- With the plugin, a third run (*rescan*) starts from a new mypy cache but keeps the
  stored scans, as a new worktree or project on the same Odoo would. Its errors must
  match the cold run's, or the exit column shows `!`.

```bash
uv run python tools/bench/bench.py --label baseline              # measure and save
uv run python tools/bench/bench.py --label wp1 --compare baseline
uv run python tools/bench/bench.py --compare-only wp1 --compare baseline   # no mypy run
```

Options:

- `--odoo-version N` (default 18): `odoo_version` of the plugin config. Other versions
  default to the root `tools/odoo/fetch.sh N.0` builds and keep their reports and
  baseline in `<bench-dir>/odoo-N/`.
- `--odoo-root DIR`: a directory that holds only an `odoo` symlink (see the
  [note on `mypy_path`](configuration.md#simple-setup-without-the-overlay-root)).
- `--bench-dir DIR`: where `<label>.json`, the generated configs (`config/`) and the
  caches (`cache/<label>/<addon>`) go.
- `--addons a b ...`, or `--profile small|default|big` (3, 6 or 37 core addons), or
  `--project DIR` (every addon of a project directory).
- `--jobs N`: addons at once; 1 gives the most stable timings.
- `--thresholds FILE`: a JSON object of error code (or `"*"` for all) to the most
  plugin-only errors allowed; above it the script prints `THRESHOLD ...` and exits 1
  (`tools/bench/thresholds-18.json` is the current `small` run on Odoo 18).
- `--option "key = value"` (repeatable): a line added to the plugin's
  `[tool.odoo_typing]` table (`--option "literal_selection = true"`).
- The run without the plugin is saved once as `noplugin.json` and reused. Pass
  `--refresh-baseline` after upgrading mypy or Odoo; it rebuilds the baseline from the
  `--addons` given, so pass all of them.

`--compare` prints per-addon counts and times, then the plugin-only errors that are NEW
or GONE compared with the earlier label. They are grouped by error code and message
pattern (quoted names replaced by `"_"`), with 3 examples per group. The script exits
with status 1 if any mypy run crashed.

## Benchmark results

mypy 2.4.1, Odoo 18 core, each addon checked on its own with a fresh cache, then again
warm. Most *plugin-only* errors are real under Odoo's declared field types
(`str | Literal[False]`, `int | NewId` ids), or mypy inference complaints that real
recordset types expose. Times are wall-clock on a shared machine and vary by about 10 %.

| Addon | Errors without plugin | Errors with plugin | Plugin-only | Cold s (without → with) | Warm s (without → with) |
|---|---:|---:|---:|---:|---:|
| crm | 1797 | 1155 | 20 | 3.45 → 5.06 | 0.20 → 0.27 |
| sale | 2743 | 1847 | 30 | 4.33 → 6.73 | 0.25 → 0.33 |
| account | 10051 | 7267 | 140 | 9.31 → 11.54 | 0.33 → 0.30 |
| website_sale | 1853 | 1060 | 14 | 4.63 → 7.18 | 0.26 → 0.36 |
| project_sms | 65 | 51 | 0 | 2.82 → 4.15 | 0.20 → 0.28 |
| sale_stock | 1708 | 780 | 16 | 4.44 → 7.09 | 0.25 → 0.35 |

Warm runs report exactly the errors of the cold ones, and no run crashed. Peak RSS with
the plugin is 230-340 MB, 5-9 % more than plain mypy.

## Comparison with odoo-ls and pylint-odoo

See [odoo-ls-cli.md](odoo-ls-cli.md) for how this project compares with odoo-ls and
pylint-odoo on the probe addon in `tests/fixtures/addons/probe_mod`.
