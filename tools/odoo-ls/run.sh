#!/usr/bin/env bash
# Run odoo-ls in CLI (--parse) mode on a set of addons and print a readable report.
#
# Usage: tools/odoo-ls/run.sh [TRACKED_DIR ...]
#   TRACKED_DIR  directories whose files get diagnostics (default: tests/fixtures/addons)
#
# Environment:
#   ODOO_PKG        path to the `odoo` Python package, i.e. the dir holding release.py
#                   (default: detected with `python3 -c "import odoo"`)
#   EXTRA_ADDONS    extra addons paths, colon-separated (the tracked dirs are added anyway)
#   ODOO_LS_VERSION odoo-ls version installed by install.sh (default: 1.6.2)
#   OUTPUT          raw JSON output path (default: .tools/odoo-ls/output.json)
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
VERSION="${ODOO_LS_VERSION:-1.6.2}"
BIN="$ROOT/.tools/odoo-ls/$VERSION/odoo_ls_server"
OUTPUT="${OUTPUT:-$ROOT/.tools/odoo-ls/output.json}"

[[ -x "$BIN" ]] || "$ROOT/tools/odoo-ls/install.sh" "$VERSION"

ODOO_PKG="${ODOO_PKG:-$(python3 -c 'import os, odoo; print(os.path.dirname(odoo.__file__))')}"
[[ -f "$ODOO_PKG/release.py" ]] || { echo "No release.py in $ODOO_PKG" >&2; exit 1; }

# --community-path must be the directory that CONTAINS the `odoo` package.
# - git checkout (odoo-bin next to it): use the checkout root, core addons live in <root>/addons.
# - pip/deb install: the parent is site-/dist-packages, which odoo-ls refuses
#   ("Public entry point ... is equal to odoo path ... will be ignored"), so we
#   point it at a private directory with only an `odoo` symlink.
PARENT="$(dirname "$ODOO_PKG")"
if [[ -f "$PARENT/odoo-bin" ]]; then
  COMMUNITY="$PARENT"
  ADDONS=("$PARENT/addons" "$ODOO_PKG/addons")
else
  COMMUNITY="$ROOT/.tools/odoo-ls/odoo-root"
  mkdir -p "$COMMUNITY"
  ln -sfn "$ODOO_PKG" "$COMMUNITY/odoo"
  ADDONS=("$COMMUNITY/odoo/addons")
fi

TRACKED=("$@")
[[ ${#TRACKED[@]} -gt 0 ]] || TRACKED=("$ROOT/tests/fixtures/addons")

ARGS=(--parse -c "$COMMUNITY" -o "$OUTPUT" --log-level error --color never
      --logs-directory "$ROOT/.tools/odoo-ls/logs")
for a in "${ADDONS[@]}"; do [[ -d "$a" ]] && ARGS+=(-a "$a"); done
IFS=':' read -r -a EXTRA <<< "${EXTRA_ADDONS:-}"
for a in "${EXTRA[@]}"; do [[ -n "$a" ]] && ARGS+=(-a "$a"); done
for t in "${TRACKED[@]}"; do
  t="$(cd "$t" && pwd)"
  ARGS+=(-a "$t" -t "$t")
done

mkdir -p "$(dirname "$OUTPUT")"
echo "odoo-ls $VERSION: community=$COMMUNITY tracked=${TRACKED[*]}" >&2
# Exit code is always 0, even when diagnostics are found -> report.py decides.
"$BIN" "${ARGS[@]}" >/dev/null 2>&1

python3 "$ROOT/tools/odoo-ls/report.py" "$OUTPUT" "${TRACKED[@]}"
