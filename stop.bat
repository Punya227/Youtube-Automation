@echo off
cd /d "%~dp0"
if not exist "youtube_shorts" mkdir "youtube_shorts"
type nul > "youtube_shorts\worker.stop"
echo Stop requested. The worker finishes the current upload chunk, saves its progress and exits
echo within a few seconds (an interrupted upload resumes automatically on next start).
