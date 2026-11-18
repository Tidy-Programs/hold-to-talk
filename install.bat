@echo off
rem Installs (or updates) Hold to talk for your Windows account: double-click it.
rem With no arguments it opens the setup window (install-gui.ps1); with any
rem argument it runs the text installer, install.ps1, which gets the arguments,
rem e.g.  install.bat -Yes   or   install.bat -Console  (the text installer with
rem its questions). No administrator rights.
setlocal
title Install Hold to talk
set "PS=%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe"
if not exist "%PS%" set "PS=powershell.exe"
if not exist "%~dp0install.ps1" (
    echo install.ps1 is missing next to install.bat. Extract the whole ZIP first
    echo ^(right-click the ZIP ^> Extract All^), then run install.bat from the new folder.
    if not defined HTT_NO_PAUSE pause
    exit /b 1
)
if defined HTT_FALLBACK_REASON goto fallback
if not "%~1"=="" goto text
rem The setup window: install-gui.ps1 -Launch starts it without a console
rem window and returns 0 once it is up (this console then closes), or 1 after
rem printing why it could not be shown (then the text installer runs here).
if not exist "%~dp0install-gui.ps1" (
    echo The setup window ^(install-gui.ps1^) is missing. Using the text installer instead.
    goto text
)
"%PS%" -NoProfile -ExecutionPolicy Bypass -File "%~dp0install-gui.ps1" -Launch
if "%errorlevel%"=="0" exit /b 0
goto text
:fallback
echo %HTT_FALLBACK_REASON%. Using the text installer instead.
set "HTT_FALLBACK_REASON="
:text
"%PS%" -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" %*
set "RC=%errorlevel%"
echo.
if "%RC%"=="0" (
    echo Finished.
) else if "%RC%"=="3" (
    echo Stopped at your request. Nothing was installed.
) else (
    echo Something went wrong ^(see the message above^). Nothing is broken: fix the
    echo problem and run install.bat again; it carries on where it stopped.
)
if not defined HTT_NO_PAUSE pause
exit /b %RC%
