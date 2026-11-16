@echo off
chcp 65001 >nul
if not exist "%~dp0.setup-venv\Scripts\python.exe" (
  echo Run install.cmd first.
  pause
  exit /b 1
)
"%~dp0.setup-venv\Scripts\python.exe" -X utf8 -B "%~dp0scripts\windows_setup.py" --launch
if errorlevel 1 pause
