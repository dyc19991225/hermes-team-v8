@echo off
chcp 65001 >nul
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\install-windows.ps1"
set "TeamSetupExit=%errorlevel%"
echo.
if not "%TeamSetupExit%"=="0" echo Setup did not finish. Read the message above, then run install.cmd again.
pause
exit /b %TeamSetupExit%
