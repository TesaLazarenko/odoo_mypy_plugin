#!/usr/bin/env bash
# Fetch an Odoo version for tests and benchmarks: a shallow, sparse git checkout
# (no static/ or i18n/ directories: JS, images and translations the plugin never reads)
# plus a "root" with the layout of the deb/pip package, where odoo/addons holds every
# addon (git checkouts keep most addons in a separate top-level addons/ directory).
#
# Usage: tools/odoo/fetch.sh <version> [--update]     e.g. tools/odoo/fetch.sh 19.0
# Prints the odoo package directory to use as ODOO_PATH (tests) or --odoo-root/odoo.
#
# Location: $ODOO_TYPING_ODOO_CACHE (default ~/.cache/odoo-typing/odoo)/<version>/{src,root}
set -euo pipefail

version="${1:?usage: fetch.sh <version> [--update], e.g. 19.0}"
update="${2:-}"
[[ "$version" =~ ^[0-9]+\.[0-9]+$ ]] || { echo "fetch.sh: version must look like 19.0" >&2; exit 2; }
base="${ODOO_TYPING_ODOO_CACHE:-$HOME/.cache/odoo-typing/odoo}/$version"
src="$base/src"
root="$base/root"
repo="${ODOO_TYPING_ODOO_REPO:-https://github.com/odoo/odoo.git}"

if [[ ! -d "$src/.git" ]]; then
    mkdir -p "$base"
    git clone --quiet --depth 1 --branch "$version" --filter=blob:none --no-checkout \
        "$repo" "$src"
    git -C "$src" sparse-checkout set --no-cone \
        '/*' '!/addons/*/static/' '!/addons/*/i18n/' \
        '!/odoo/addons/*/static/' '!/odoo/addons/*/i18n/' '!/doc/'
    git -C "$src" checkout --quiet "$version"
elif [[ "$update" == "--update" ]]; then
    git -C "$src" fetch --quiet --depth 1 origin "$version"
    git -C "$src" checkout --quiet --force FETCH_HEAD
fi

# The package layout: odoo/<entries of the real package>, odoo/addons/<every addon>.
# Addons are real directories of hard links (cp -al: no extra disk space), not symlinks:
# mypy resolves a symlinked addon to its real directory, which has no odoo/addons
# parent package, and then names each of its files twice (crm.x and odoo.addons.crm.x).
rm -rf "$root"
mkdir -p "$root/odoo/addons"
for entry in "$src"/odoo/*; do
    [[ "$(basename "$entry")" == addons ]] || ln -s "$entry" "$root/odoo/$(basename "$entry")"
done
cp -al "$src"/odoo/addons/. "$src"/addons/. "$root/odoo/addons/"

echo "$root/odoo"
