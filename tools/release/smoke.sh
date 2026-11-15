#!/usr/bin/env bash
# Install a built wheel into a fresh virtual environment (not the project's: nothing of
# the source tree is importable) and run mypy with the plugin on a small addon.
#
# Usage: tools/release/smoke.sh <wheel> <odoo package dir> [<odoo major>]
#   e.g. tools/release/smoke.sh dist/odoo_typing-0.2.0-py3-none-any.whl "$(tools/odoo/fetch.sh 19.0)" 19
# SMOKE_MYPY (default "mypy==2.4.1") is the mypy requirement to install with it and
# SMOKE_PYTHON (default 3.12) the Python of the environment. Exit status 0 when mypy
# reports exactly the probe's error, else 1.
set -euo pipefail

usage="usage: smoke.sh <wheel> <odoo package dir> [<odoo major>]"
# One wheel: a dist/*.whl that matches several would shift the other arguments.
(($# >= 2 && $# <= 3)) && [[ "$1" == *.whl ]] || { echo "smoke: $usage" >&2; exit 2; }
wheel="$(realpath "$1")"
odoo="$(realpath "$2")"
major="${3:-19}"
[[ -f "$odoo/release.py" ]] || { echo "smoke: $odoo is not an odoo package directory" >&2; exit 2; }
repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT

uv venv --quiet --python "${SMOKE_PYTHON:-3.12}" "$work/venv"
uv pip install --quiet --python "$work/venv/bin/python" "$wheel" "${SMOKE_MYPY:-mypy==2.4.1}"
"$work/venv/bin/python" -I -c '
import importlib.metadata, mypy.plugin, odoo_typing.plugin
assert issubclass(odoo_typing.plugin.plugin(importlib.metadata.version("mypy")), mypy.plugin.Plugin)
print("odoo-typing", importlib.metadata.version("odoo-typing"), "with mypy", importlib.metadata.version("mypy"))'
for tool in odoo-typing-root odoo-typing-baseline odoo-typing-stubs; do
    "$work/venv/bin/$tool" --help > /dev/null
done

project="$work/project"
mkdir -p "$project/root" "$project/addons"
ln -s "$odoo" "$project/root/odoo"
cp -r "$repo/tests/fixtures/addons/clean_mod" "$project/addons/"
cat > "$project/addons/clean_mod/models/probe.py" << 'PY'
from odoo import models


class Probe(models.Model):
    _name = "x.probe"

    def check(self) -> None:
        self.env["res.partner"].create({"no_such_field": 1})
PY
echo "from . import partner, probe" > "$project/addons/clean_mod/models/__init__.py"
cat > "$project/pyproject.toml" << TOML
[tool.mypy]
plugins = ["odoo_typing.plugin"]
mypy_path = ["root", "addons"]
check_untyped_defs = true
show_traceback = true

[[tool.mypy.overrides]]
module = ["odoo", "odoo.*"]
follow_imports = "silent"

[tool.odoo_typing]
odoo_version = $major
odoo_path = "root/odoo"
addons_path = ["addons"]
TOML

status=0
output="$(cd "$project" && XDG_CACHE_HOME="$work/xdg" "$work/venv/bin/mypy" --cache-dir "$work/cache" addons/clean_mod 2>&1)" || status=$?
echo "$output"
# Exactly the probe's error: the plugin is loaded, knows res.partner, and the clean
# addon stays clean (no crash: status 1, not 2).
[[ "$status" == 1 ]] || { echo "smoke: mypy exited with $status, expected 1" >&2; exit 1; }
grep -q 'probe.py:8: error: .*"no_such_field".*\[odoo-vals-key\]' <<< "$output" \
    || { echo "smoke: the plugin did not report the probe's error" >&2; exit 1; }
[[ "$(grep -c ': error:' <<< "$output")" == 1 ]] || { echo "smoke: unexpected errors" >&2; exit 1; }
echo "smoke: ok"
