# -*- mode: python ; coding: utf-8 -*-

import os
import sys

from PyInstaller.utils.hooks import collect_submodules


project_root = os.path.abspath(SPECPATH)
signing_identity = os.environ.get("PROJECT_X_SIGNING_IDENTITY") or None
cloudflared_name = "cloudflared.exe" if sys.platform == "win32" else "cloudflared"
cloudflared_path = os.path.join(project_root, "vendor", cloudflared_name)
binaries = []
if os.path.isfile(cloudflared_path):
    binaries.append((cloudflared_path, "."))
else:
    raise SystemExit(
        f"Missing {cloudflared_path}. Run the platform build script so Project X "
        "can bundle its secure tunnel client."
    )

hiddenimports = (
    collect_submodules("pystray")
    + collect_submodules("aiortc")
    + collect_submodules("av")
    + [
        "aiohttp.web",
        "pynput.keyboard._darwin" if sys.platform == "darwin" else "pynput.keyboard._win32",
        "pynput.mouse._darwin" if sys.platform == "darwin" else "pynput.mouse._win32",
    ]
)

a = Analysis(
    [os.path.join(project_root, "desktop_app.py")],
    pathex=[project_root],
    binaries=binaries,
    datas=[(os.path.join(project_root, "static"), "static")],
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "pytest"],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="Project X",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    codesign_identity=signing_identity,
    disable_windowed_traceback=False,
)

collection = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="Project X",
)

if sys.platform == "darwin":
    app = BUNDLE(
        collection,
        name="Project X.app",
        bundle_identifier="com.projectx",
        codesign_identity=signing_identity,
        info_plist={
            "CFBundleDisplayName": "Project X",
            "CFBundleName": "Project X",
            "LSUIElement": True,
            "NSCameraUsageDescription": "Project X shares the host camera when the host enables Camera.",
            "NSMicrophoneUsageDescription": "Project X shares host audio when the host enables microphone listening.",
            "NSHighResolutionCapable": True,
        },
    )
