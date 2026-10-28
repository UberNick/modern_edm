"""
Unified Cross-Platform Build & Verification Script for Modern EDM.
Automates:
1. Dependency check (PySide6, pyserial, pyinstaller)
2. Full unit + offscreen UI test suite execution (tests/test_edm.py)
3. PyInstaller packaging via modern_edm.spec:
   - Windows -> dist/ModernEDM.exe (single-file standalone GUI executable)
   - macOS   -> dist/ModernEDM.app (native macOS Application Bundle + optional .dmg/.zip)
4. Post-build smoke test (--headless-check on the compiled binary)
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent
SPEC_FILE = PROJECT_DIR / "modern_edm.spec"
DIST_DIR = PROJECT_DIR / "dist"
BUILD_DIR = PROJECT_DIR / "build"


def run_step(cmd: list[str], desc: str, env: dict[str, str] | None = None) -> None:
    print(f"\n=== {desc} ===")
    print("Command:", " ".join(cmd))
    res = subprocess.run(cmd, cwd=str(PROJECT_DIR), env=env)
    if res.returncode != 0:
        raise SystemExit(f"[FAILED] {desc} exited with code {res.returncode}")


def ensure_pyinstaller() -> None:
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        run_step(
            [sys.executable, "-m", "pip", "install", "pyinstaller>=6.0.0"],
            "Installing PyInstaller",
        )


def main() -> int:
    sys_name = platform.system()
    print(f"Building Modern EDM on {sys_name} ({platform.machine()}) with Python {sys.version.split()[0]}")

    # 1. Run automated unit & offscreen UI tests before packaging
    test_env = os.environ.copy()
    test_env["QT_QPA_PLATFORM"] = "offscreen"
    run_step(
        [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-p", "test_*.py", "-v"],
        "1/4 Running Automated Parity Test Suite",
        env=test_env,
    )

    # 2. Ensure PyInstaller is installed
    ensure_pyinstaller()

    # 3. Clean previous build artifacts and run PyInstaller
    for d in (DIST_DIR, BUILD_DIR):
        if d.exists():
            shutil.rmtree(d, ignore_errors=True)

    run_step(
        [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", str(SPEC_FILE)],
        f"2/4 Building Standalone Package for {sys_name}",
    )

    # 4. Locate built binary and run post-build headless smoke check
    if sys_name == "Windows":
        built_bin = DIST_DIR / "ModernEDM.exe"
    elif sys_name == "Darwin":
        built_bin = DIST_DIR / "ModernEDM.app" / "Contents" / "MacOS" / "ModernEDM"
    else:
        built_bin = DIST_DIR / "ModernEDM"

    if not built_bin.exists():
        raise SystemExit(f"[ERROR] Expected output binary not found at {built_bin}")

    run_step(
        [str(built_bin), "--headless-check"],
        "3/4 Running Post-Build Smoke Check (--headless-check)",
        env=test_env,
    )

    # 5. On macOS, optionally ad-hoc sign and zip the .app bundle for distribution
    if sys_name == "Darwin":
        app_bundle = DIST_DIR / "ModernEDM.app"
        run_step(
            ["codesign", "--force", "--deep", "--sign", "-", str(app_bundle)],
            "4/4 Applying Ad-Hoc Code Signature to ModernEDM.app",
        )
        zip_out = DIST_DIR / "ModernEDM-macOS"
        shutil.make_archive(str(zip_out), "zip", root_dir=str(DIST_DIR), base_dir="ModernEDM.app")
        print(f"\n[SUCCESS] macOS App Bundle ready: {app_bundle}")
        print(f"[SUCCESS] Distributable Archive : {zip_out}.zip")
    else:
        size_mb = built_bin.stat().st_size / (1024 * 1024)
        print(f"\n[SUCCESS] Standalone Executable ready: {built_bin} ({size_mb:.2f} MB)")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
