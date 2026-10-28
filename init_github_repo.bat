@echo off
setlocal
cd /d "%~dp0"

set "REPO_NAME=%~1"
if "%REPO_NAME%"=="" set "REPO_NAME=modern_edm"

set "VISIBILITY=%~2"
if "%VISIBILITY%"=="" set "VISIBILITY=--public"

echo ============================================================
echo Initializing Git repository in %CD%
echo ============================================================

if not exist ".git" (
    git init -b main
) else (
    echo [INFO] .git directory already exists.
)

git add .
git status -s

git diff --cached --quiet
if errorlevel 1 (
    git commit -m "Initial commit: Modern EDM v3.3.0 (100%% EDMWin v3.2 parity, Windows/macOS PyInstaller build)"
) else (
    echo [INFO] Working tree already committed.
)

where gh >nul 2>nul
if %errorlevel% equ 0 (
    echo.
    echo ============================================================
    echo Creating GitHub repository '%REPO_NAME%' via GitHub CLI (gh)
    echo ============================================================
    gh repo create "%REPO_NAME%" %VISIBILITY% --source=. --remote=origin --push
    if errorlevel 1 (
        echo [WARN] 'gh repo create' returned a non-zero status. Pushing to origin...
        git push -u origin main
    )
) else (
    echo.
    echo [ERROR] GitHub CLI (gh) is not installed or not on PATH.
    echo Install GitHub CLI or add your remote origin manually, then run:
    echo     git push -u origin main
)

echo.
echo [DONE] Repository setup complete.
endlocal
