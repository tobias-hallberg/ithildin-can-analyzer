#!/usr/bin/env bash
# Build the Ithildin DMG from a fresh PyInstaller bundle.
#
# Prereqs:
#   - `uv run pyinstaller packaging/ithildin.spec` already produced
#     dist/Ithildin.app on macOS.
#
# Output: dist/Ithildin-<version>-arm64.dmg at the repo root.

set -euo pipefail

REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
DIST="${REPO_ROOT}/dist"
APP="${DIST}/Ithildin.app"
STAGING="${DIST}/dmg-staging"

VERSION="$(awk -F\" '/^version *= */{print $2; exit}' "${REPO_ROOT}/pyproject.toml")"
if [[ -z "${VERSION}" ]]; then
  echo "error: could not parse version from pyproject.toml" >&2
  exit 1
fi

if [[ ! -d "${APP}" ]]; then
  echo "error: ${APP} not found — run pyinstaller first" >&2
  exit 1
fi

echo ">> building DMG for ithildin ${VERSION}"

rm -rf "${STAGING}"
mkdir -p "${STAGING}"
cp -R "${APP}" "${STAGING}/"
ln -s /Applications "${STAGING}/Applications"

OUT="${DIST}/Ithildin-${VERSION}-arm64.dmg"
rm -f "${OUT}"
hdiutil create -volname Ithildin -srcfolder "${STAGING}" -ov -format UDZO "${OUT}"
rm -rf "${STAGING}"

echo ">> wrote ${OUT}"
