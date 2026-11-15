#!/usr/bin/env bash
# Fetch an Odoo version for tests and benchmarks: a shallow, sparse git checkout
# (no static/ or i18n/ directories: JS, images and translations the plugin never reads)
# plus a "root" with the layout of the deb/pip package, where odoo/addons holds every
# addon (git checkouts keep most addons in a separate top-level addons/ directory).
#
# Usage: tools/odoo/fetch.sh <version> [--latest]     e.g. tools/odoo/fetch.sh 19.0
# Checks out the commit tools/odoo/commits.txt pins for <version> (or
# $ODOO_TYPING_ODOO_COMMIT), so that CI and local runs test the same Odoo; at the pin it
# needs no network, nor back at a pin it had checked out before (after --latest). A
# version without a pin, or --latest, takes the newest commit of its branch (--update is
# the old name of --latest); a commit whose odoo/release.py is another major version
# fails. Prints the odoo package directory to use as ODOO_PATH (tests) or
# --odoo-root/odoo, and the commit on stderr. Assign it first, so that a failed fetch
# stops the shell instead of leaving ODOO_PATH empty:
#   ODOO_PATH=$(tools/odoo/fetch.sh 19.0) && export ODOO_PATH && uv run pytest -n 6
#
# Location: $ODOO_TYPING_ODOO_CACHE (default ~/.cache/odoo-typing/odoo)/<version>/{src,root}
# Upstream: $ODOO_TYPING_ODOO_REPO (default https://github.com/odoo/odoo.git)
set -euo pipefail

usage="usage: fetch.sh <version> [--latest], e.g. 19.0"
(($# >= 1 && $# <= 2)) || { echo "fetch.sh: $usage" >&2; exit 2; }
version="$1"
mode="${2:-}"
[[ "$version" =~ ^[0-9]+\.[0-9]+$ ]] || { echo "fetch.sh: version must look like 19.0" >&2; exit 2; }
[[ -z "$mode" || "$mode" == --latest || "$mode" == --update ]] || { echo "fetch.sh: $usage" >&2; exit 2; }
pins="$(dirname "${BASH_SOURCE[0]}")/commits.txt"
base="${ODOO_TYPING_ODOO_CACHE:-$HOME/.cache/odoo-typing/odoo}/$version"
src="$base/src"
root="$base/root"
repo="${ODOO_TYPING_ODOO_REPO:-https://github.com/odoo/odoo.git}"
delay="${ODOO_TYPING_FETCH_RETRY_DELAY:-10}"  # seconds before the 2nd attempt, twice that before the 3rd
# The layout of the cache: bump it with any change to the sparse patterns below or to
# how root/ is built, so that every existing checkout and root, local or restored by CI
# (whose cache keys also hash this file), is rebuilt to the new layout.
LAYOUT=2
# The sparse patterns: everything but static/, i18n/ and doc/.
patterns=('/*' '!/addons/*/static/' '!/addons/*/i18n/'
    '!/odoo/addons/*/static/' '!/odoo/addons/*/i18n/' '!/doc/')

pin=""
if [[ -z "$mode" ]]; then
    pin="${ODOO_TYPING_ODOO_COMMIT:-$(awk -v v="$version" '$1 == v { print $2 }' "$pins" 2> /dev/null || true)}"
fi
[[ -z "$pin" || "$pin" =~ ^[0-9a-f]{40}$ ]] || { echo "fetch.sh: the pin of $version is not a full sha: $pin" >&2; exit 2; }

mkdir -p "$base"
if command -v flock > /dev/null; then  # one fetch.sh per version at a time
    exec 9> "$base/.lock"
    flock 9
fi

if [[ ! -d "$src/.git" ]]; then
    rm -rf "$src"
    git init --quiet "$src"
fi
# A new checkout, or one made by a fetch.sh with other patterns: apply ours (a checkout
# fetches the blobs of newly included paths), and rebuild root/ from the result.
if [[ "$(git -C "$src" sparse-checkout list 2> /dev/null || true)" != "$(printf '%s\n' "${patterns[@]}")" ]]; then
    rm -f "$base/root.commit"
    git -C "$src" sparse-checkout set --no-cone "${patterns[@]}"
fi
git -C "$src" remote set-url origin "$repo" 2> /dev/null || git -C "$src" remote add origin "$repo"
head="$(git -C "$src" rev-parse --verify --quiet HEAD || true)"
# Back at a pin whose commit is still in the object store (after --latest): no network.
# No transport for the lookup, or git would fetch a missing commit, with its history.
if [[ -n "$pin" && "$head" != "$pin" ]] \
    && GIT_ALLOW_PROTOCOL=none git -C "$src" cat-file -e "$pin^{commit}" 2> /dev/null \
    && git -C "$src" checkout --quiet --force --detach "$pin" 2> /dev/null; then
    head="$pin"
fi
if [[ -z "$pin" || "$head" != "$pin" ]]; then
    # GitHub serves any reachable commit by its sha (actions/checkout fetches the same way).
    # The checkout downloads the blobs (most of the data): retried with the fetch.
    for attempt in 1 2 3; do
        if git -C "$src" fetch --quiet --depth 1 --filter=blob:none origin "${pin:-$version}" \
            && git -C "$src" checkout --quiet --force --detach FETCH_HEAD; then
            break
        fi
        if ((attempt == 3)); then
            echo "fetch.sh: cannot fetch or check out ${pin:-the branch $version} from $repo" >&2
            exit 1
        fi
        sleep $((attempt * delay))
    done
    head="$(git -C "$src" rev-parse HEAD)"
fi
[[ -z "$pin" || "$head" == "$pin" ]] || { echo "fetch.sh: got $head, not $pin" >&2; exit 1; }
# A pin copied from another version's line, or an ODOO_TYPING_ODOO_COMMIT left from
# another version: never build this version's root from another Odoo.
major="$(sed -n -E "/^version_info *=/{s/^version_info *= *\( *['\"]?(saas~)?([0-9]+).*/\2/p;q}" "$src/odoo/release.py" 2> /dev/null || true)"
if [[ "$major" != "${version%%.*}" ]]; then
    echo "fetch.sh: $head is Odoo ${major:-of an unknown version} (odoo/release.py), not $version" >&2
    exit 1
fi
echo "fetch.sh: Odoo $version at $head ($(git -C "$src" log -1 --format=%cs HEAD))" >&2

# The package layout: odoo/<entries of the real package>, odoo/addons/<every addon>.
# Addons are real directories of hard links (cp -al: no extra disk space), not symlinks:
# mypy resolves a symlinked addon to its real directory, which has no odoo/addons
# parent package, and then names each of its files twice (crm.x and odoo.addons.crm.x).
# Built only when the commit or the layout changed, aside and then renamed into place, so
# that a test run or a benchmark reading the root at an unchanged commit does not lose it
# (the core entries are symlinks into src/: a new commit changes them at once).
stamp="$head $LAYOUT"
if [[ ! -d "$root/odoo" || "$(cat "$base/root.commit" 2> /dev/null)" != "$stamp" ]]; then
    new="$base/root.new"
    rm -rf "$new" "$base/root.old"
    mkdir -p "$new/odoo/addons"
    for entry in "$src"/odoo/*; do
        [[ "$(basename "$entry")" == addons ]] || ln -s "$entry" "$new/odoo/$(basename "$entry")"
    done
    cp -al "$src"/odoo/addons/. "$src"/addons/. "$new/odoo/addons/"
    if [[ -e "$root" ]]; then
        mv "$root" "$base/root.old"
    fi
    mv "$new" "$root"
    echo "$stamp" > "$base/root.commit"
    rm -rf "$base/root.old"
fi

echo "$root/odoo"
