# -*- mode: python ; coding: utf-8 -*-
"""
Cross-Platform PyInstaller Specification for Modern EDM (Windows .exe & macOS .app).
Usage:
    pyinstaller --noconfirm --clean modern_edm.spec
"""

import sys
from pathlib import Path

block_cipher = None
project_dir = Path(SPECPATH).resolve()

datas = [
    (str(project_dir / "sample_excavation.cfg"), "."),
]

hiddenimports = [
    "PySide6.QtCore",
    "PySide6.QtGui",
    "PySide6.QtWidgets",
    "serial",
    "serial.tools",
    "serial.tools.list_ports",
    "sqlite3",
    "drivers.total_station",
    "storage.cfg_parser",
    "storage.cloud_sync",
    "storage.database",
    "storage.label_printer",
    "storage.legacy_migrator",
    "survey.math_engine",
    "ui.dialogs",
    "ui.main_window",
    "ui.plot_canvas",
]

excludes = [
    "tkinter",
    "matplotlib",
    "scipy",
    "pandas",
    "PySide6.QtQml",
    "PySide6.QtQuick",
    "PySide6.Qt3DCore",
    "PySide6.Qt3DRender",
    "PySide6.QtWebEngineCore",
    "PySide6.QtWebEngineWidgets",
    "PySide6.QtMultimedia",
    "PySide6.QtBluetooth",
    "PySide6.QtNetwork",
    "PySide6.QtSql",
    "PySide6.QtTest",
    "PySide6.QtXml",
]

a = Analysis(
    [str(project_dir / "main.py")],
    pathex=[str(project_dir)],
    binaries=[],
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

if sys.platform == "darwin":
    # macOS: Build an onedir bundle wrapped inside a native macOS .app bundle
    exe = EXE(
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,
        name="ModernEDM",
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=True,
        console=False,
        disable_windowed_traceback=False,
        argv_emulation=False,
        target_arch=None,
        codesign_identity=None,
        entitlements_file=None,
    )
    coll = COLLECT(
        exe,
        a.binaries,
        a.zipfiles,
        a.datas,
        strip=False,
        upx=True,
        upx_exclude=[],
        name="ModernEDM",
    )
    app = BUNDLE(
        coll,
        name="ModernEDM.app",
        icon=None,
        bundle_identifier="com.oldstoneage.modernedm",
        info_plist={
            "CFBundleName": "Modern EDM",
            "CFBundleDisplayName": "Modern EDM — Total Station Recorder",
            "CFBundleShortVersionString": "3.3.0",
            "CFBundleVersion": "3.3.0",
            "NSHighResolutionCapable": True,
            "LSMinimumSystemVersion": "11.0",
            "NSBluetoothAlwaysUsageDescription": (
                "Modern EDM uses Bluetooth Serial (SPP) to communicate with Total Stations."
            ),
        },
    )
else:
    # Windows / Linux: Build a single-file standalone GUI executable (like edm2.exe)
    exe = EXE(
        pyz,
        a.scripts,
        a.binaries,
        a.zipfiles,
        a.datas,
        [],
        name="ModernEDM",
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=True,
        upx_exclude=[],
        runtime_tmpdir=None,
        console=False,
        disable_windowed_traceback=False,
        argv_emulation=False,
        target_arch=None,
        codesign_identity=None,
        entitlements_file=None,
    )
