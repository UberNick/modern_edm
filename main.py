"""
Entrypoint for Modern EDM (Cross-Platform Total Station Recording Application).
Supports both standard Python execution and frozen PyInstaller builds on Windows & macOS.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sys
from pathlib import Path


def get_bundle_dir() -> Path:
    """Returns the read-only resource directory (inside _MEIPASS when frozen by PyInstaller)."""
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(getattr(sys, "_MEIPASS")).resolve()
    return Path(__file__).resolve().parent


def get_writable_data_dir() -> Path:
    """
    Returns a persistent, user-writable directory for the SQLite database and .CFG files:
    - Windows frozen (.exe): Directory containing the .exe (portable field mode, matching edmwin.exe),
      falling back to ~/Documents/ModernEDM if the .exe folder is read-only.
    - macOS frozen (.app bundle): ~/Documents/ModernEDM (avoids macOS App Translocation & read-only .dmg mounts).
    - Unfrozen dev mode: C:\\dev\\edm\\modern_edm source directory.
    """
    if getattr(sys, "frozen", False):
        exe_path = Path(sys.executable).resolve()
        if sys.platform == "darwin" or ".app/Contents/MacOS" in exe_path.as_posix():
            mac_dir = Path.home() / "Documents" / "ModernEDM"
            mac_dir.mkdir(parents=True, exist_ok=True)
            return mac_dir

        exe_dir = exe_path.parent
        if os.access(exe_dir, os.W_OK):
            return exe_dir

        fallback_dir = Path.home() / "Documents" / "ModernEDM"
        fallback_dir.mkdir(parents=True, exist_ok=True)
        return fallback_dir

    return Path(__file__).resolve().parent


def ensure_default_cfg(writable_dir: Path, bundle_dir: Path) -> Path:
    """Copies sample_excavation.cfg from the PyInstaller bundle on first launch if needed."""
    target_cfg = writable_dir / "sample_excavation.cfg"
    bundled_cfg = bundle_dir / "sample_excavation.cfg"
    if not target_cfg.exists() and bundled_cfg.exists():
        try:
            shutil.copy2(bundled_cfg, target_cfg)
        except OSError:
            return bundled_cfg
    return target_cfg if target_cfg.exists() else bundled_cfg


def main() -> int:
    bundle_dir = get_bundle_dir()
    data_dir = get_writable_data_dir()
    default_cfg_path = ensure_default_cfg(data_dir, bundle_dir)
    default_db_path = data_dir / "excavation_project.sqlite"

    parser = argparse.ArgumentParser(description="Modern EDM Total Station Recorder")
    parser.add_argument(
        "--db",
        default=str(default_db_path),
        help="Path to SQLite project database",
    )
    parser.add_argument(
        "--cfg",
        default=str(default_cfg_path),
        help="Path to legacy .CFG or JSON configuration file",
    )
    parser.add_argument(
        "--headless-check",
        action="store_true",
        help="Initializes window in offscreen mode and verifies startup cleanly",
    )
    args, qt_args = parser.parse_known_args()

    if args.headless_check:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

    from PySide6.QtWidgets import QApplication
    from ui.main_window import EDMMainWindow

    app = QApplication([sys.argv[0], *qt_args])
    app.setApplicationName("Modern EDM")
    app.setOrganizationName("OldStoneAge")
    window = EDMMainWindow(db_path=args.db, cfg_path=args.cfg)
    window.show()

    if args.headless_check:
        app.processEvents()
        window.close()
        window.db.close()
        return 0

    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
