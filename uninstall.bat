@echo off
rem Uninstalls Hold to talk (runs uninstall.ps1 from a temporary copy so the
rem install folder can be deleted). Safe to double-click; it asks first and
rem keeps your settings and history unless you say otherwise.
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