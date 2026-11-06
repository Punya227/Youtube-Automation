@echo off
setlocal EnableDelayedExpansion
cd /d "%~dp0"
echo ============================================
echo  Shorts Autopilot - setup
echo ============================================

rem --- find Python 3.11+ ---------------------------------------------------
set "PY="
for %%V in (3.13 3.12 3.11 3.14) do (
    if not defined PY (
        py -%%V -c "import sys" >nul 2>&1 && set "PY=py -%%V"
    )
)
if not defined PY (
    python -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>&1 && set "PY=python"
)
if not defined PY (
    echo [ERROR] Python 3.11 or newer was not found.
    echo         Install it from https://www.python.org/downloads/ ^(tick "Add python.exe to PATH"^)
    echo         or run:  winget install Python.Python.3.12
    pause
    exit /b 1
)
for /f "delims=" %%i in ('%PY% -c "import sys; print(sys.version.split()[0])"') do set "PYVER=%%i"
echo [OK] Using Python %PYVER% (%PY%)

rem --- virtual environment ---------------------------------------------------
if not exist ".venv\Scripts\python.exe" (
    echo Creating virtual environment...
    %PY% -m venv .venv || (echo [ERROR] venv creation failed & pause & exit /b 1)
)
echo Installing dependencies (this can take a few minutes)...
".venv\Scripts\python.exe" -m pip install --upgrade pip --quiet
".venv\Scripts\python.exe" -m pip install -r requirements.txt --quiet || (echo [ERROR] pip install failed & pause & exit /b 1)
echo [OK] Dependencies installed

rem --- configuration -------------------------------------------------------------
if not exist ".env" (
    copy ".env.example" ".env" >nul
    echo [OK] Created .env from .env.example  ^(UPLOAD_MODE=mock^)
) else (
    echo [OK] .env already exists - left unchanged
)

rem --- folders + database ---------------------------------------------------------
".venv\Scripts\python.exe" -c "from config import load_config, ensure_directories; from database import Database; c=load_config(); ensure_directories(c); Database(c.db_path); print('[OK] Folders and database ready in', c.data_dir)" || (echo [ERROR] initialisation failed & pause & exit /b 1)

rem --- FFmpeg ------------------------------------------------------------------------
where ffprobe >nul 2>&1
if errorlevel 1 (
    echo [WARN] FFmpeg/FFprobe not found on PATH. Video validation needs it.
    echo        Install with:  winget install Gyan.FFmpeg   then open a new terminal.
    echo        Or set FFPROBE_PATH and FFMPEG_PATH in .env.
) else (
    echo [OK] FFmpeg found
)

if not exist "secrets\client_secret.json" (
    echo [INFO] secrets\client_secret.json not found - needed only for LIVE mode. See README section 3.
)

echo.
echo Setup complete. Next:  start.bat   ^(dashboard: http://localhost:8501^)
pause
