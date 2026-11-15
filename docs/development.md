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
(`INTERNAL ERROR`, exit status 2), not only on wrong messages, and a clean run must
print nothing on stderr: `result.stderr` of `run_mypy` holds mypy's stderr and the
plugin's warnings.

`-n <workers>` (pytest-xdist) runs them in parallel; without it they run one by one.
Each worker has its own temporary directories and mypy cache (the session fixtures are
per worker), so each analyzes Odoo's own modules once. On Odoo 19 (14 CPUs): 287 s one
by one, 160 s with `-n 2`, 90 s with `-n 4`, 68 s with `-n 6` (2 GB in all), 58 s with
`-n 8`. A worker takes 0.4-0.7 GB, so mind the memory of the machine more than its CPUs.

mypy runs in the pytest process (`run_mypy_in_process` in `tests/conftest.py`), and a
build leaves the garbage collector tuned for a process of its own; after each test that
ran mypy the harness restores it and collects. If the process then still holds more
than its RSS budget, that test fails, the message goes to stderr at once, and the run
stops (with xdist, the other workers stop after their current test), so that a leak
across tests is reported while the machine still has memory. The budget is
`ODOO_TYPING_TEST_RSS_MB`, by default the smaller of 3000 MB and the machine's memory
divided by the number of workers plus 2 (1500 MB for `-n 6` on 12 GB). It is checked
only between tests and only for the pytest processes: one test that takes the memory
by itself, or memory taken outside pytest, still runs the machine out of it.

## Odoo versions and pinned commits

`tools/odoo/fetch.sh <version>` makes a shallow, sparse checkout (no `static/` or
`i18n/`, about 150-200 MB) under `~/.cache/odoo-typing/odoo/<version>/` at the commit
[tools/odoo/commits.txt](../tools/odoo/commits.txt) pins for that version (CI tests the
same commits), and prints the `odoo` package directory to test against:

```bash
ODOO_PATH=$(tools/odoo/fetch.sh 19.0) && export ODOO_PATH && uv run pytest -n 6
```

Not `ODOO_PATH=$(tools/odoo/fetch.sh 19.0) uv run pytest` on one line: when the fetch
fails, that still runs the tests, with an empty `ODOO_PATH` (which fails them all).

- At its pin, and back at a pin it has checked out before (after `--latest`), `fetch.sh`
  needs no network. It fails when the commit's `odoo/release.py` is another major
  version (a sha copied to the wrong line, or an `ODOO_TYPING_ODOO_COMMIT` left from
  another version).
- It rebuilds `root/` (the package layout) only when the commit or the cache layout
  changes (`LAYOUT` in the script; bump it with any change to the sparse patterns or to
  how `root/` is built), aside and then renamed into place. Calling it while tests or the
  benchmark use that version is therefore safe as long as its commit and layout stay the
  same. Moving the pin, or `--latest`, while a test run uses that version is not: the
  checkout changes in place, and the core entries of `root/odoo` are symlinks into it.
  Calls for one version wait for each other (`flock`, where it exists).
- `--latest` takes the newest commit of the branch instead (`--update` is its old name).
  `ODOO_TYPING_ODOO_COMMIT` checks out another commit, `ODOO_TYPING_ODOO_CACHE` and
  `ODOO_TYPING_ODOO_REPO` change the cache directory and the upstream repository.
- The tests read the version from its `release.py` (or `ODOO_VERSION`). For a version
  the plugin has no profile for yet, the end-to-end tests are skipped.
- `@pytest.mark.odoo(18, 19)` limits a test to some versions; a version the plugin does
  not support (a typo such as `odoo(190)`) fails the test.
- In CI, set `ODOO_REQUIRED=1`: an Odoo that is not found, whose version cannot be read
  or that has no profile then fails the end-to-end tests instead of skipping them
  silently. An empty `ODOO_PATH` always fails them.

To move a pin (to test a newer Odoo):

1. Run the tests on the newest commit:
   `ODOO_PATH=$(tools/odoo/fetch.sh 19.0 --latest) && export ODOO_PATH && uv run pytest -n 6`,
   or read the summary of the weekly *Odoo latest* workflow, which does the same in CI.
2. Fix what fails, then put the commit `fetch.sh` printed on stderr
   (`fetch.sh: Odoo 19.0 at <sha> (<date>)`) into `tools/odoo/commits.txt`, in the same
   change as the fixes. CI then caches and tests that commit.
3. For 18.0, also run the benchmark on the new pin with `--refresh-baseline` (the run
   without the plugin changes with Odoo) and regenerate `tools/bench/thresholds-18.json`
   from that `--profile small` run: bench.yml compares the pinned 18.0 against it.

## CI workflows

- [ci.yml](../.github/workflows/ci.yml) (pushes to `main`, pull requests, and called by
  the release): ruff, pylint and mypy (also for Python 3.11 and 3.13), then the tests on
  each pinned Odoo with mypy 2.1.0 and 2.4.1 on Python 3.12, plus Python 3.11 (Odoo 17,
  mypy 2.1.0) and 3.13 (Odoo 20, mypy 2.4.1). Odoo checkouts are cached per pinned
  commit and version of `fetch.sh`; a newer run of the same branch or pull request
  cancels the older one.
- [odoo-latest.yml](../.github/workflows/odoo-latest.yml) (weekly, or by hand): the tests
  on the newest commit of each Odoo branch. A red run changes nothing; its summary lists
  the commits tested.
- [bench.yml](../.github/workflows/bench.yml) (weekly, or by hand): the benchmark below
  (`--profile small`) on the pinned Odoo 18, against `tools/bench/thresholds-18.json`.
  The report is uploaded also when a threshold is exceeded.
- [release.yml](../.github/workflows/release.yml) (a `v*` tag): the whole CI on the
  tagged commit, a check that the tag is `v` + the version in `pyproject.toml` and that
  `CHANGELOG.md` has its section, `uv build`, `twine check`, and
  [tools/release/smoke.sh](../tools/release/smoke.sh), which installs the wheel in a
  fresh environment and runs mypy with it on a small addon against the pinned Odoo 19.
  Only then is it published to PyPI (trusted publishing, environment `pypi`). The smoke
  test also runs locally, here on the newest wheel in `dist/` (or pass the exact file):

  ```bash
  uv build && odoo_path=$(tools/odoo/fetch.sh 19.0) && tools/release/smoke.sh "$(ls -t dist/odoo_typing-*.whl | head -1)" "$odoo_path" 19
  ```

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
tools/odoo/fetch.sh 18.0                                         # the pinned Odoo 18
uv run python tools/bench/bench.py --label baseline              # measure and save
uv run python tools/bench/bench.py --label wp1 --compare baseline
uv run python tools/bench/bench.py --compare-only wp1 --compare baseline   # no mypy run
```

Options:

- `--odoo-version N` (default 18): `odoo_version` of the plugin config. Other versions
  keep their reports and baseline in `<bench-dir>/odoo-N/`.
- `--odoo-root DIR`: a directory that holds only an `odoo` symlink (see the
  [note on `mypy_path`](configuration.md#simple-setup-without-the-overlay-root)).
  Default: the root `tools/odoo/fetch.sh N.0` builds
  (`~/.cache/odoo-typing/odoo/N.0/root`).
- `--bench-dir DIR` (default `~/.cache/odoo-typing/bench`): where `<label>.json`, the
  generated configs (`config/`) and the caches (`cache/<label>/<addon>`) go.
- `--addons a b ...`, or `--profile small|default|big` (3, 6 or 37 core addons), or
  `--project DIR` (every addon of a project directory).
- `--jobs N`: addons at once; 1 gives the most stable timings.
- `--thresholds FILE`: a JSON object of error code (or `"*"` for all) to the most
  plugin-only errors allowed; above it the script prints `THRESHOLD ...` and exits 1
  (`tools/bench/thresholds-18.json` is the `small` run on the pinned Odoo 18,
  regenerated when the 18.0 pin moves).
- `--option "key = value"` (repeatable): a line added to the plugin's
  `[tool.odoo_typing]` table (`--option "literal_selection = true"`).
- `--timeout SECONDS` (default 1800): the longest a mypy run may take.
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
