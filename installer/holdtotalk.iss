; holdtotalk.iss - Inno Setup script for HoldToTalk-Setup-<version>.exe.
; Built by tools\build_setup.py (do not run ISCC on this file by hand: the build
; passes the values below and generates the two include files).
;
; This Setup.exe is only a CARRIER. It installs nothing itself: no install
; folder, no Start menu group, no uninstaller of its own, no entry in Installed
; apps. It unpacks the files named in install-files.txt (plus files.sha256, the
; hash list) into Inno's private temporary folder, runs the existing setup
; window (install-gui.ps1) from there without any console window, waits for
; it, and exits with the window's exit code (0 installed, 1 failed, 2 stopped,
; 3 closed before installing). install.ps1, run by that window, does the real
; install, the shortcuts and the Installed apps entry, exactly as install.bat does.
;
; Values passed by the build (/D):
;   AppName, AppVersion, AppVersionNumeric (4 numbers, e.g. 0.9.0.0),
;   StageDir (the folder holding the staged files), OutDir, OutName (no .exe).
; Generated into StageDir by the build:
;   files.inc  the [Files] lines (one per file of install-files.txt, plus files.sha256)
;   place.inc  the Pascal procedure PlaceFiles that moves them into their folders
;   cleanup.inc  the script of the hidden PowerShell that removes Inno's temporary
;              folder after this exe has exited (see build_setup.py)
;
; Plain ASCII.

#ifndef AppName
  #error AppName is not defined (run tools\build_setup.py)
#endif
#ifndef AppVersion
  #error AppVersion is not defined
#endif
#ifndef AppVersionNumeric
  #error AppVersionNumeric is not defined
#endif
#ifndef StageDir
  #error StageDir is not defined
#endif
#ifndef OutDir
  #error OutDir is not defined
#endif
#ifndef OutName
  #error OutName is not defined
#endif

[Setup]
AppId={{5F81807B-991C-4910-86B6-BBDCCAE660E9}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher=Hold to talk contributors
VersionInfoVersion={#AppVersionNumeric}
VersionInfoProductVersion={#AppVersionNumeric}
VersionInfoProductName={#AppName}
VersionInfoCompany=Hold to talk contributors
VersionInfoDescription={#AppName} Setup
VersionInfoCopyright=
SourceDir={#StageDir}
; Time stamps are stored in UTC, and the build gives every file one fixed time, so
; the build machine's clock and time zone are not recorded in the exe.
TimeStampsInUTC=yes
SetupIconFile=app.ico
OutputDir={#OutDir}
OutputBaseFilename={#OutName}
Compression=lzma2
SolidCompression=yes
; Never asks for administrator rights.
PrivilegesRequired=lowest
; The 64-bit PowerShell is started (install.bat uses it too); Python needs 64-bit anyway.
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
; A carrier only: nothing of its own is installed or registered.
CreateAppDir=no
Uninstallable=no
CreateUninstallRegKey=no
UsePreviousAppDir=no
UsePreviousGroup=no
UsePreviousSetupType=no
UsePreviousTasks=no
UsePreviousLanguage=no
DisableProgramGroupPage=yes
AllowNoIcons=yes
; Inno's own pages are never shown: the setup window is the whole interface.
DisableStartupPrompt=yes
DisableWelcomePage=yes
DisableDirPage=yes
DisableReadyPage=yes
DisableReadyMemo=yes
DisableFinishedPage=yes
ShowLanguageDialog=no
CloseApplications=no
RestartApplications=no
SetupLogging=no

[Files]
#include AddBackslash(StageDir) + "files.inc"

[Code]
procedure ExitProcess(ExitCode: Cardinal); external 'ExitProcess@kernel32 stdcall';
function SetEnvironmentVariable(Name, Value: String): Boolean; external 'SetEnvironmentVariableW@kernel32 stdcall';

var
  WindowExitCode: Integer;

#include AddBackslash(StageDir) + "place.inc"
#include AddBackslash(StageDir) + "cleanup.inc"

procedure ShowNoWindowMessage();
{ One plain message when the setup window could not be opened. Not shown in
  /SILENT or /VERYSILENT mode (the exit code 1 says it). }
begin
  if not WizardSilent then
    MsgBox('The setup window could not be opened on this PC.' + #13#10 + #13#10 +
      '{#AppName} can also be installed from the ZIP on the project''s page: download it, extract it, and double-click install.bat.',
      mbError, MB_OK);
end;

function RunSetupWindow(): Integer;
{ Starts the setup window hidden-console, waits for it and returns its exit code.
  Returns 1 (after one plain message) when PowerShell cannot be started or the
  window process ends with a code outside 0..3 (0 installed, 1 failed, 2 stopped,
  3 closed before installing). }
var
  Ps, Gui, Params, Extra: String;
  Rc: Integer;
begin
  Result := 1;
  Ps := ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe');
  Gui := ExpandConstant('{tmp}\app\install-gui.ps1');
  if (not FileExists(Ps)) or (not FileExists(Gui)) then
  begin
    ShowNoWindowMessage();
    Exit;
  end;
  { -FromSetupExe tells the window that its folder is Setup's private temporary
    folder: it never offers the text installer (install.bat would be deleted
    under it) and opens documents from a copy. }
  Params := '-NoProfile -ExecutionPolicy Bypass -STA -WindowStyle Hidden -File "' + Gui + '" -FromSetupExe';
  { Test pass-through: /HTTARGS="..." is appended to the window's arguments, so a
    test can add -AutoRun and the five test overrides. It is safe: install-gui.ps1
    refuses -AutoRun (which ticks the terms box by itself) unless all five
    overrides point into the temp folder and a registry key under
    Software\HoldToTalkTest\, so these arguments can never make a silent real
    install; and they come after -File, so PowerShell treats them as the
    script's own arguments, never as PowerShell switches or commands. }
  Extra := Trim(ExpandConstant('{param:HTTARGS|}'));
  if Extra <> '' then
    Params := Params + ' ' + Extra;
  if Exec(Ps, Params, ExpandConstant('{tmp}'), SW_HIDE, ewWaitUntilTerminated, Rc) then
  begin
    if (Rc >= 0) and (Rc <= 3) then
      Result := Rc
    else
    begin
      ShowNoWindowMessage();
      Result := 1;
    end;
  end
  else
  begin
    ShowNoWindowMessage();
    Result := 1;
  end;
end;

procedure RemoveTempFolderLater();
{ The temporary folder cannot be removed by this process while it runs: a hidden
  PowerShell does it a moment after this process has ended. It runs a small script
  that is written to a file of its own in the temp folder and started with -File;
  the folder's name goes to it in an environment variable, never through cmd or
  into a command text. The script removes only a folder named
  is-<letters and digits>.tmp directly in the temp folder (retrying for up to 30
  seconds) and then deletes itself. }
var
  Rc: Integer;
  ScriptFile: String;
begin
  ScriptFile := GenerateUniqueName(ExtractFileDir(RemoveBackslash(ExpandConstant('{tmp}'))), '.ps1');
  if not SaveStringToFile(ScriptFile, CleanupScript, False) then
    Exit;
  SetEnvironmentVariable('HTT_CLEAN_TARGET', ExpandConstant('{tmp}'));
  Exec(ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe'),
    '-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "' + ScriptFile + '"',
    '', SW_HIDE, ewNoWait, Rc);
end;

function InitializeSetup(): Boolean;
begin
  PlaceFiles();
  WindowExitCode := RunSetupWindow();
  if WizardSilent then
  begin
    { /SILENT and /VERYSILENT show no page: let Setup finish normally, so Inno
      removes its temporary folder itself, and report the window's exit code. }
    Result := True;
  end
  else
  begin
    { Run by a person: no Inno page may appear after the window closes, so leave
      now. The unpacked files go first, and the rest of the temporary folder is
      removed a moment later by a hidden command, once this process has ended. }
    DelTree(ExpandConstant('{tmp}\app'), True, True, True);
    RemoveTempFolderLater();
    ExitProcess(WindowExitCode);
    Result := False;
  end;
end;

function GetCustomSetupExitCode(): Integer;
begin
  Result := WindowExitCode;
end;

function ShouldSkipPage(PageID: Integer): Boolean;
begin
  Result := True;
end;
