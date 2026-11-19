@echo off
rem Uninstalls Hold to talk (runs from a temporary copy so the install folder
rem can be deleted). Safe to double-click. With no arguments it opens the
rem uninstaller window (uninstall-gui.ps1), which asks first and keeps your
rem settings and history unless you tick the box; with any argument it runs the
rem text uninstaller, uninstall.ps1, which gets the arguments (-Yes, -DeleteData).
setlocal
title Uninstall Hold to talk
set "HERE=%~dp0"
set "PS=%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe"
if not exist "%PS%" set "PS=powershell.exe"
set "TMPD=%TEMP%\htt-uninstall-%RANDOM%%RANDOM%"
mkdir "%TMPD%" >nul 2>&1
if errorlevel 1 goto :noprivate
rem The folder is private to this account, like the installer's own helper
rem folder (Get-WorkDir in install.ps1): inherited access is removed and only
rem this account is let in, so nobody else can add or replace a script in it.
set "MYSID="
for /f "tokens=2 delims=," %%S in ('"%SystemRoot%\System32\whoami.exe" /user /fo csv /nh') do set "MYSID=%%~S"
if not defined MYSID goto :noprivate
"%SystemRoot%\System32\icacls.exe" "%TMPD%" /inheritance:r /grant:r "*%MYSID%:(OI)(CI)F" >nul 2>&1
if errorlevel 1 goto :noprivate
copy /y "%HERE%uninstall.ps1" "%TMPD%\" >nul
if errorlevel 1 goto :nocopy
copy /y "%HERE%htt-common.ps1" "%TMPD%\" >nul
if errorlevel 1 goto :nocopy
rem The window and its icon go along when they are there (the text uninstaller
rem does not need them).
if exist "%HERE%uninstall-gui.ps1" copy /y "%HERE%uninstall-gui.ps1" "%TMPD%\" >nul
if exist "%HERE%app.ico" copy /y "%HERE%app.ico" "%TMPD%\" >nul
if not "%~1"=="" goto :text
if not exist "%TMPD%\uninstall-gui.ps1" (
    echo The uninstaller window ^(uninstall-gui.ps1^) is missing. Using the text uninstaller instead.
    goto :text
)
rem uninstall-gui.ps1 -Launch starts the window without a console window and
rem returns 0 once it is up (this console then closes; the window removes the
rem temporary copy when it ends), or 1 after printing why it could not be shown
rem (then the text uninstaller runs here).
"%PS%" -NoProfile -ExecutionPolicy Bypass -File "%TMPD%\uninstall-gui.ps1" -Launch -BatDir "%HERE%."
if "%errorlevel%"=="0" exit /b 0
:text
"%PS%" -NoProfile -ExecutionPolicy Bypass -File "%TMPD%\uninstall.ps1" -BatDir "%HERE%." %*
set "RC=%errorlevel%"
rd /s /q "%TMPD%" >nul 2>&1
echo.
if not defined HTT_NO_PAUSE pause
if "%RC%"=="10" goto :selfdelete
if "%RC%"=="2" exit /b 0
exit /b %RC%

:nocopy
rd /s /q "%TMPD%" >nul 2>&1
echo Could not read uninstall.ps1 / htt-common.ps1 next to this file.
echo Nothing was changed.
if not defined HTT_NO_PAUSE pause
exit /b 1

:noprivate
rd /s /q "%TMPD%" >nul 2>&1
echo Could not make a private temporary folder for the uninstaller.
echo Nothing was changed.
if not defined HTT_NO_PAUSE pause
exit /b 1

:selfdelete
cd /d "%TEMP%"
(goto) 2>nul & del /f /q "%HERE%uninstall.bat" & rd "%HERE%"