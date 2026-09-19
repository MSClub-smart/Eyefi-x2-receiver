@echo off
REM Start in the system tray (hidden window) + auto-start the receiver.
REM Same mode the boot auto-start uses. Manage via the tray icon (double-click).
cd /d "%~dp0.."
set "PYW=%LocalAppData%\Programs\Python\Python312\pythonw.exe"
if exist "%PYW%" (
  start "EyeFi Tray" "%PYW%" -m EyeFiReceiver --tray
) else (
  start "EyeFi Tray" pythonw -m EyeFiReceiver --tray
)
