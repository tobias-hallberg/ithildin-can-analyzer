#!/usr/bin/env bash
# Build the Ithildin AppImage from a fresh PyInstaller bundle.
#
# Prereqs:
#   - `uv run pyinstaller packaging/ithildin.spec` already produced
#     dist/ithildin/ (a directory containing the `ithildin` binary).
#   - `appimagetool` on PATH, or set $APPIMAGETOOL to its location.
#   - libfuse2 installed on the build host (AppImage runtime dep).
#
# Output: dist/Ithildin-<version>-x86_64.AppImage at the repo root.

set -euo pipefail

REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd)"
DIST="${REPO_ROOT}/dist"
APPDIR="${DIST}/Ithildin.AppDir"
PYINSTALLER_OUT="${DIST}/ithildin"
PKG_LINUX="${REPO_ROOT}/packaging/linux"
RESOURCES="${REPO_ROOT}/src/ithildin/resources"
APPIMAGETOOL="${APPIMAGETOOL:-appimagetool}"

# Pull version straight from pyproject.toml — single source of truth.
VERSION="$(awk -F\" '/^version *= */{print $2; exit}' "${REPO_ROOT}/pyproject.toml")"
if [[ -z "${VERSION}" ]]; then
  echo "error: could not parse version from pyproject.toml" >&2
  exit 1
fi

if [[ ! -d "${PYINSTALLER_OUT}" ]]; then
  echo "error: ${PYINSTALLER_OUT} not found — run pyinstaller first" >&2
  exit 1
fi

echo ">> building AppImage for ithildin ${VERSION}"

rm -rf "${APPDIR}"
mkdir -p "${APPDIR}/usr/bin"
mkdir -p "${APPDIR}/usr/share/icons/hicolor/256x256/apps"
mkdir -p "${APPDIR}/usr/share/applications"
mkdir -p "${APPDIR}/usr/share/metainfo"

# PyInstaller --onedir output: the `ithildin` binary and a sibling
# _internal/ directory. Copy the whole tree under usr/bin so paths
# inside the binary stay correct.
cp -a "${PYINSTALLER_OUT}/." "${APPDIR}/usr/bin/"

# AppRun shim + top-level desktop file + top-level icon — required by
# appimagetool. Top-level copies are duplicated under usr/share/ for
# clean integration when the AppImage is extracted/installed.
install -m 0755 "${PKG_LINUX}/AppRun" "${APPDIR}/AppRun"
install -m 0644 "${PKG_LINUX}/ithildin.desktop" "${APPDIR}/ithildin.desktop"
install -m 0644 "${PKG_LINUX}/ithildin.desktop" \
  "${APPDIR}/usr/share/applications/ithildin.desktop"

if [[ -f "${RESOURCES}/app_icon.png" ]]; then
  install -m 0644 "${RESOURCES}/app_icon.png" "${APPDIR}/ithildin.png"
  install -m 0644 "${RESOURCES}/app_icon.png" \
    "${APPDIR}/usr/share/icons/hicolor/256x256/apps/ithildin.png"
else
  # Without an icon appimagetool refuses to build. Drop a 1x1
  # placeholder PNG so the build still produces an artifact for
  # smoke-testing; replace by committing src/ithildin/resources/app_icon.png.
  printf '\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc\xfc\xff\xff?\x03\x00\x06\xfe\x02\xfe\xa5\xd4\xc4u\x00\x00\x00\x00IEND\xaeB`\x82' \
    > "${APPDIR}/ithildin.png"
  cp "${APPDIR}/ithildin.png" \
    "${APPDIR}/usr/share/icons/hicolor/256x256/apps/ithildin.png"
fi

OUT="${DIST}/Ithildin-${VERSION}-x86_64.AppImage"
rm -f "${OUT}"

ARCH=x86_64 "${APPIMAGETOOL}" "${APPDIR}" "${OUT}"

echo ">> wrote ${OUT}"
