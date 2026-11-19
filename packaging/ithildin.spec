# PyInstaller spec for ithildin — shared by the Windows, Linux and macOS
# build pipelines. Run from the repo root:
#
#     uv run pyinstaller packaging/ithildin.spec --noconfirm
#
# Output: dist/ithildin/ithildin(.exe) plus a _internal/ tree. Windows
# Inno Setup and the Linux AppImage scripts both consume this directory.
# On macOS it additionally wraps that tree as dist/Ithildin.app, which
# packaging/macos/build-dmg.sh consumes.

# ruff: noqa
# type: ignore
from __future__ import annotations

import sys
import tomllib
from pathlib import Path

from PyInstaller.utils.hooks import collect_dynamic_libs, collect_submodules

SPEC_DIR = Path(SPECPATH).resolve()
REPO_ROOT = SPEC_DIR.parent
SRC = REPO_ROOT / "src"
RESOURCES = SRC / "ithildin" / "resources"

# Bundle the entire resources/ tree so importlib.resources keeps working
# inside the frozen app. Destinations are relative to the bundle root.
datas = [(str(RESOURCES), "ithildin/resources")]

# python-can discovers interface backends lazily via entry points;
# PyInstaller's static analysis can't see those imports, so name them
# explicitly. The supported set tracks ui/adapter_prober.py.
hiddenimports = [
    "can.interfaces.pcan",
    "can.interfaces.kvaser",
    "can.interfaces.gs_usb",
    "can.interfaces.socketcan",
    "can.interfaces.ixxat",
    "can.interfaces.virtual",
    "gs_usb",
    "usb.backend.libusb1",
    "libusb_package",
]

# The libusb DLL gs_usb runs on, where libusb_package looks for it: inside
# its own package directory. libusb_package's own hook copies it to the
# bundle root, and pyinstaller-hooks-contrib's usb hook only bundles a
# libusb installed on the build machine, which CI has none of.
binaries = collect_dynamic_libs("libusb_package")

# mcp uses dynamic dispatch in a few places; pull in its submodules
# wholesale rather than chase individual misses at runtime. Skip
# mcp.cli — it imports typer, which is an optional mcp[cli] extra
# that ithildin doesn't pull in.
hiddenimports += collect_submodules(
    "mcp", filter=lambda name: not name.startswith("mcp.cli")
)

# Trim the bundle — these get pulled in transitively but we never use them.
# Note: `pydoc` and `unittest` are NOT safe to exclude — pyqtgraph and a
# few transitive deps import them at top level.
excludes = [
    "tkinter",
    "PyQt5",
    "PyQt6",
    "PySide2",
]

# Icon: .ico for Windows, .icns for macOS, .png for Linux. All optional —
# the build still produces a working bundle without them, just with the
# Qt default.
if sys.platform == "win32":
    icon_path = RESOURCES / "app_icon.ico"
elif sys.platform == "darwin":
    icon_path = RESOURCES / "app_icon.icns"
else:
    icon_path = RESOURCES / "app_icon.png"
icon = str(icon_path) if icon_path.exists() else None


block_cipher = None


a = Analysis(
    [str(SRC / "ithildin" / "app.py")],
    pathex=[str(SRC)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="ithildin",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=icon,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="ithildin",
)

if sys.platform == "darwin":
    with open(REPO_ROOT / "pyproject.toml", "rb") as fh:
        version = tomllib.load(fh)["project"]["version"]
    app = BUNDLE(
        coll,
        name="Ithildin.app",
        icon=icon,
        bundle_identifier="se.ithildin.analyzer",
        info_plist={
            "CFBundleName": "Ithildin",
            "CFBundleDisplayName": "Ithildin",
            "CFBundleShortVersionString": version,
            "CFBundleVersion": version,
            "NSHighResolutionCapable": True,
            "LSMinimumSystemVersion": "14.0",
        },
    )
