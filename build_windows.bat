@echo off
setlocal
cd /d "%~dp0"

echo ============================================================
echo Building ModernEDM.exe (Standalone Windows x64 Executable)
echo ============================================================

set "PYTHON_EXE=python"
if exist "C:\dev\.venv\Scripts\python.exe" (
    "C:\dev\.venv\Scripts\python.exe" -c "import PySide6, serial" >nul 2>&1
    if not errorlevel 1 (
        set "PYTHON_EXE=C:\dev\.venv\Scripts\python.exe"
    )
)

"%PYTHON_EXE%" build_release.py
if errorlevel 1 (
    echo.
    echo [ERROR] Build failed!
    exit /b 1
)

echo.
echo [DONE] Standalone Windows executable created at:
echo        %~dp0dist\ModernEDM.exe
endlocal
