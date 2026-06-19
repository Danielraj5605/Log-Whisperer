@echo off
setlocal EnableDelayedExpansion

:: ============================================================
::  Log Whisperer — Windows Setup Script
::  Run this once after cloning the repo.
:: ============================================================

title Log Whisperer Setup

echo.
echo  =====================================================
echo   Log Whisperer ^| First-Time Setup
echo  =====================================================
echo.

:: ── Step 1: Check Python ────────────────────────────────────
echo  [1/5] Checking Python version...

python --version >nul 2>&1
if errorlevel 1 (
    echo.
    echo  [ERROR] Python not found!
    echo.
    echo  Please install Python 3.11 or newer from:
    echo  https://www.python.org/downloads/
    echo.
    echo  Make sure to check "Add Python to PATH" during install.
    echo.
    pause
    exit /b 1
)

:: Check Python version is 3.11+
for /f "tokens=2 delims= " %%v in ('python --version 2^>^&1') do set PYVER=%%v
for /f "tokens=1,2 delims=." %%a in ("%PYVER%") do (
    set PYMAJOR=%%a
    set PYMINOR=%%b
)

if %PYMAJOR% LSS 3 (
    echo.
    echo  [ERROR] Python %PYVER% is too old. Log Whisperer needs Python 3.11+.
    echo  Download: https://www.python.org/downloads/
    echo.
    pause
    exit /b 1
)
if %PYMAJOR% EQU 3 if %PYMINOR% LSS 11 (
    echo.
    echo  [ERROR] Python %PYVER% is too old. Log Whisperer needs Python 3.11+.
    echo  Download: https://www.python.org/downloads/
    echo.
    pause
    exit /b 1
)

echo  [OK] Python %PYVER% found.
echo.

:: ── Step 2: Create virtual environment ──────────────────────
echo  [2/5] Creating virtual environment (.venv)...

if exist ".venv" (
    echo  [OK] .venv already exists, skipping creation.
) else (
    python -m venv .venv
    if errorlevel 1 (
        echo.
        echo  [ERROR] Failed to create virtual environment.
        echo  Try running: python -m pip install --upgrade pip
        echo.
        pause
        exit /b 1
    )
    echo  [OK] Virtual environment created.
)
echo.

:: ── Step 3: Activate and upgrade pip ────────────────────────
echo  [3/5] Activating virtual environment and upgrading pip...

call .venv\Scripts\activate.bat
if errorlevel 1 (
    echo.
    echo  [ERROR] Failed to activate virtual environment.
    echo  Try running the script as Administrator, or check antivirus settings.
    echo.
    pause
    exit /b 1
)

python -m pip install --upgrade pip --quiet
echo  [OK] pip is up to date.
echo.

:: ── Step 4: Install Log Whisperer and all dependencies ──────
echo  [4/5] Installing Log Whisperer and dependencies...
echo  (This may take a minute on first run)
echo.

pip install -e . --quiet
if errorlevel 1 (
    echo.
    echo  [ERROR] Dependency installation failed.
    echo  Try manually running:
    echo    .venv\Scripts\activate
    echo    pip install -e .
    echo.
    pause
    exit /b 1
)

echo  [OK] All dependencies installed.
echo.

:: ── Step 5: Install globally so the command works everywhere ──
echo  [5/6] Installing logwhisper command globally (no activation needed)...

:: Deactivate venv temporarily to install into user Python
call deactivate 2>nul
python -m pip install -e . --user --quiet
if errorlevel 1 (
    echo  [WARN] Global install failed. You will need to run .venv\Scripts\activate
    echo         before using logwhisper in new terminals.
    goto :skip_path_fix
)

echo  [OK] Global install complete.

:: Add Python user Scripts to PATH if not already there
echo  Adding Python Scripts folder to your PATH...
for /f "delims=" %%p in ('python -c "import sys, os; print(os.path.join(os.environ.get('APPDATA',''), 'Python', 'Python%d%d' %% (sys.version_info.major, sys.version_info.minor), 'Scripts'))"') do set USERSCRIPTS=%%p

powershell -Command "$cur = [System.Environment]::GetEnvironmentVariable('PATH','User'); if ($cur -notlike '*%USERSCRIPTS%*') { [System.Environment]::SetEnvironmentVariable('PATH', $cur + ';%USERSCRIPTS%', 'User'); Write-Host '  [OK] Added to PATH: %USERSCRIPTS%' } else { Write-Host '  [OK] Already on PATH.' }"

:skip_path_fix
echo.

:: ── Step 6: Run guided setup ─────────────────────────────────
echo  [6/6] Running first-time configuration...
echo.
echo  You will be asked for your Gemini API key.
echo  Get one for free at: https://aistudio.google.com/app/apikey
echo.

logwhisper setup
echo.

:: ── Done ─────────────────────────────────────────────────────
echo.
echo  =====================================================
echo   Setup complete!
echo  =====================================================
echo.
echo  logwhisper is now available from any terminal.
echo.
echo  Watch a log file for anomalies:
echo    logwhisper watch --file .\test.log
echo.
echo  Interactive AI chat about errors:
echo    logwhisper chat
echo.
echo  Full monitoring mode (auto-detect project):
echo    logwhisper run
echo.
echo  If a command is ever not found, run once:
echo    .venv\Scripts\activate
echo.
echo  =====================================================
echo.
pause
