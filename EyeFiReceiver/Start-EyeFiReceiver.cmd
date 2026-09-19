@echo off
REM Eye-Fi local receiver (GUI, no console window). Double-click to run.
cd /d "%~dp0.."
set "PYW=%LocalAppData%\Programs\Python\Python312\pythonw.exe"
if exist "%PYW%" (
  start "EyeFi Receiver" "%PYW%" -m EyeFiReceiver
) else (
  start "EyeFi Receiver" pythonw -m EyeFiReceiver
)
