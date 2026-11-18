@echo off
rem Run Hold to talk from this folder (for developers). Normal users install
rem it with install.bat and start it from the Start menu instead.
rem   start.bat           starts the app with no console window (it lives in the
rem                       tray) and opens its window; if it is already running,
rem                       just opens the window
rem   start.bat console   run in this window with a detailed log (debugging;
rem                       Ctrl+C quits)
rem Uses the same Python environment as the installed app
rem (%LOCALAPPDATA%\HoldToTalk\venv) and the same Python-finding and repair
rem logic: install.ps1 -EnvOnly creates or repairs it and installs whatever
rem requirements.txt / requirements-cuda.txt now ask for.
setlocal
cd /d "%~dp0"
set "PS=%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe"
if not exist "%PS%" set "PS=powershell.exe"
set "VENV=%LOCALAPPDATA%\HoldToTalk\venv"
if defined HTT_ENV_DIR set "VENV=%HTT_ENV_DIR%"
"%PS%" -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" -EnvOnly
if errorlevel 1 goto :fail
if /i "%~1"=="console" (
    "%VENV%\Scripts\python.exe" "%~dp0dictate.py" -v
    if errorlevel 1 goto :fail
    exit /b 0
)
start "" "%VENV%\Scripts\pythonw.exe" "%~dp0dictate.py" --open
exit /b 0

:fail
echo.
echo Something went wrong. See the message above (or dictate.log in
echo %%LOCALAPPDATA%%\HoldToTalk\data\logs).
pause
exit /b 1
