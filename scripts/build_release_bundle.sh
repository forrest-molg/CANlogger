#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DIST_DIR="$ROOT_DIR/dist"

if [[ $# -ge 1 && -n "${1}" ]]; then
  VERSION="$1"
else
  VERSION="$(git -C "$ROOT_DIR" describe --tags --always 2>/dev/null || date +%Y%m%d%H%M%S)"
fi

PREFIX="canlogger-${VERSION}"
VERSIONED_ARCHIVE="$DIST_DIR/${PREFIX}-linux-x86_64.tar.gz"
STABLE_ARCHIVE="$DIST_DIR/canlogger-linux-x86_64.tar.gz"

mkdir -p "$DIST_DIR"

# Archive tracked files only for reproducible release bundles.
git -C "$ROOT_DIR" archive --format=tar --prefix="${PREFIX}/" HEAD | gzip > "$VERSIONED_ARCHIVE"
cp "$VERSIONED_ARCHIVE" "$STABLE_ARCHIVE"

(
  cd "$DIST_DIR"
  sha256sum "$(basename "$STABLE_ARCHIVE")" > "$(basename "$STABLE_ARCHIVE").sha256"
)

echo "Created bundle: $VERSIONED_ARCHIVE"
echo "Created bundle: $STABLE_ARCHIVE"
echo "Created checksum: ${STABLE_ARCHIVE}.sha256"
