@echo off
REM Debug launcher: runs the GUI with a console so errors are visible, then pauses.
REM Use this if the normal launcher's window just disappears.
cd /d "%~dp0.."
set "PY=%LocalAppData%\Programs\Python\Python312\python.exe"
if not exist "%PY%" set "PY=python"
echo Running: "%PY%" -m EyeFiReceiver
echo (Close the GUI window; any error will appear below.)
echo ------------------------------------------------------------
"%PY%" -m EyeFiReceiver
echo ------------------------------------------------------------
echo Done. If a red Traceback appears above, copy it to me.
pause
