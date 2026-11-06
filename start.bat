@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo Run setup.bat first.
    pause
    exit /b 1
)
if exist "youtube_shorts\worker.stop" del "youtube_shorts\worker.stop"

echo Starting the upload worker in its own window...
echo (If the Windows startup task already runs a worker, this one exits immediately.)
start "Shorts Autopilot - Worker (close or run stop.bat to stop)" /min ".venv\Scripts\python.exe" scheduler.py

echo Starting the dashboard at http://localhost:8501  (Ctrl+C here to stop the dashboard)
".venv\Scripts\python.exe" -m streamlit run app.py --server.address 127.0.0.1 --server.port 8501

echo.
echo Dashboard stopped. The worker keeps running in its own window, so scheduled uploads continue.
echo To stop the worker as well, run stop.bat.
pause
