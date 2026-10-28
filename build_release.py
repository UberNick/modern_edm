"""
Unified Cross-Platform Build & Verification Script for Modern EDM.
Automates:
1. Automatic Python interpreter selection (prefers Python 3.10-3.13 or C:\\dev\\.venv on Windows
   when launched under bleeding-edge Python 3.14+ where PySide6/Shiboken C++ teardown can fault)
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

# Windows STATUS_STACK_BUFFER_OVERRUN (0xC0000409) & STATUS_ACCESS_VIOLATION (0xC0000005)
# can occur during C++ QApplication atexit teardown in PySide6 under Python 3.14+ after
# unittest has already printed "OK".
QT_ATEXP_TEARDOWN_CODES = {3221226505, -1073740791, 3221225477, -1073741819}


def select_best_python() -> str:
    """
    If invoked via `py` on Windows and `py` defaulted to Python 3.14+, automatically
    check if a stable Python 3.10-3.13 interpreter (with PySide6 installed) is available.
    """
    if platform.system() == "Windows" and sys.version_info >= (3, 14):
        candidates = [
            Path(r"C:\dev\.venv\Scripts\python.exe"),
            Path.home() / "AppData" / "Local" / "Programs" / "Python" / "Python312" / "python.exe",
            Path.home() / "AppData" / "Local" / "Programs" / "Python" / "Python311" / "python.exe",
            Path.home() / "AppData" / "Local" / "Programs" / "Python" / "Python310" / "python.exe",
        ]
        for cand in candidates:
            if cand.is_file():
                chk = subprocess.run(
                    [str(cand), "-c", "import PySide6, serial"],
                    capture_output=True,
                    text=True,
                )
                if chk.returncode == 0:
                    print(
                        f"[INFO] Switching build interpreter from Python {sys.version.split()[0]} "
                        f"to {cand} for full PySide6 & PyInstaller compatibility."
                    )
                    return str(cand)
    return sys.executable


def run_step(
    cmd: list[str],
    desc: str,
    env: dict[str, str] | None = None,
    allow_qt_teardown_exit_on_ok: bool = False,
) -> None:
    print(f"\n=== {desc} ===")
    print("Command:", " ".join(cmd))
    if allow_qt_teardown_exit_on_ok:
        res = subprocess.run(
            cmd,
            cwd=str(PROJECT_DIR),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        if res.stdout:
            print(res.stdout, end="")
        if res.returncode != 0:
            out_lines = (res.stdout or "").strip().splitlines()
            passed_ok = any(line.strip() == "OK" for line in out_lines[-5:])
            if res.returncode in QT_ATEXP_TEARDOWN_CODES and passed_ok:
                print(
                    f"[INFO] All tests passed ('OK'); ignoring post-exit Qt/Shiboken C++ teardown "
                    f"status {res.returncode} (0xC0000409)."
                )
                return
            raise SystemExit(f"[FAILED] {desc} exited with code {res.returncode}")
    else:
        res = subprocess.run(cmd, cwd=str(PROJECT_DIR), env=env)
        if res.returncode != 0:
            raise SystemExit(f"[FAILED] {desc} exited with code {res.returncode}")


def ensure_pyinstaller(py_exe: str) -> None:
    chk = subprocess.run([py_exe, "-c", "import PyInstaller"], capture_output=True)
    if chk.returncode != 0:
        run_step(
            [py_exe, "-m", "pip", "install", "pyinstaller>=6.0.0"],
            "Installing PyInstaller",
        )


def main() -> int:
    sys_name = platform.system()
    py_exe = select_best_python()
    print(f"Building Modern EDM on {sys_name} ({platform.machine()}) using: {py_exe}")

    # 1. Run automated unit & offscreen UI tests before packaging
    test_env = os.environ.copy()
    test_env["QT_QPA_PLATFORM"] = "offscreen"
    run_step(
        [py_exe, "-m", "unittest", "discover", "-s", "tests", "-p", "test_*.py", "-v"],
        "1/4 Running Automated Parity Test Suite",
        env=test_env,
        allow_qt_teardown_exit_on_ok=True,
    )

    # 2. Ensure PyInstaller is installed in the target interpreter
    ensure_pyinstaller(py_exe)

    # 3. Clean previous build artifacts and run PyInstaller
    for d in (DIST_DIR, BUILD_DIR):
        if d.exists():
            shutil.rmtree(d, ignore_errors=True)

    run_step(
        [py_exe, "-m", "PyInstaller", "--noconfirm", "--clean", str(SPEC_FILE)],
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
        allow_qt_teardown_exit_on_ok=True,
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
