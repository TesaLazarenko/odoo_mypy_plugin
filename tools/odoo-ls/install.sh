#!/usr/bin/env bash
# Download the odoo-ls server binary and its typeshed stubs into .tools/odoo-ls/<version>/.
#
# Usage: tools/odoo-ls/install.sh [VERSION]      (default: 1.6.2)
#
# Resulting layout (the binary looks for typeshed/ right next to itself):
#   .tools/odoo-ls/1.6.2/odoo_ls_server
#   .tools/odoo-ls/1.6.2/typeshed/stdlib/builtins.pyi
set -euo pipefail

VERSION="${1:-1.6.2}"
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
DEST="$ROOT/.tools/odoo-ls/$VERSION"
BASE_URL="https://github.com/odoo/odoo-ls/releases/download/$VERSION"

case "$(uname -s)-$(uname -m)" in
  Linux-x86_64)  ASSET="odoo-linux-x86_64-$VERSION.tar.gz" ;;
  Linux-aarch64) ASSET="odoo-linux-aarch64-$VERSION.tar.gz" ;;
  Darwin-arm64)  ASSET="odoo-darwin-aarch64-$VERSION.tar.gz" ;;
  Darwin-x86_64) ASSET="odoo-darwin-x86_64-$VERSION.tar.gz" ;;
  *) echo "Unsupported platform: $(uname -s)-$(uname -m)" >&2; exit 1 ;;
esac

if [[ -x "$DEST/odoo_ls_server" && -f "$DEST/typeshed/stdlib/builtins.pyi" ]]; then
  echo "odoo-ls $VERSION already installed in $DEST"
  exit 0
fi

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

echo "Downloading $ASSET and typeshed.zip ..."
curl -fsSL -o "$TMP/server.tar.gz" "$BASE_URL/$ASSET"
curl -fsSL -o "$TMP/typeshed.zip" "$BASE_URL/typeshed.zip"

rm -rf "$DEST" && mkdir -p "$DEST"
tar -xzf "$TMP/server.tar.gz" -C "$DEST"
# The zip's root IS the typeshed repo (stdlib/, stubs/ ...), so it must go into typeshed/.
# Python is used instead of `unzip`, which is often not installed.
python3 -I -c 'import sys, zipfile; zipfile.ZipFile(sys.argv[1]).extractall(sys.argv[2])' \
  "$TMP/typeshed.zip" "$DEST/typeshed"
chmod +x "$DEST/odoo_ls_server"

"$DEST/odoo_ls_server" --version
echo "Installed into $DEST"
