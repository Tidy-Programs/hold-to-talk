# htt-common.ps1 - where things live, shared by install.ps1 and uninstall.ps1.
# Dot-sourced, never run by itself. Windows PowerShell 5.1; keep this file
# plain ASCII (5.1 reads a file without a byte-order mark as ANSI).
#
# Layout (per user, no administrator rights):
#   program files   %LOCALAPPDATA%\Programs\HoldToTalk
#   environment     %LOCALAPPDATA%\HoldToTalk\venv
#   your data       %LOCALAPPDATA%\HoldToTalk\data   (settings, history, logs)
#   Start menu      <Start menu>\Programs\Hold to talk.lnk
#   Desktop         <Desktop>\Hold to talk.lnk       (the real, possibly
#                   redirected, Desktop from the shell's known-folder API)
#   Installed apps  HKCU\Software\Microsoft\Windows\CurrentVersion\Uninstall\HoldToTalk
#
# Test overrides (parameters of install.ps1 / uninstall.ps1, or these
# environment variables). Any one of them switches on "test mode": the real
# app is never signalled and the real profile is not touched.
#   HTT_INSTALL_ROOT   program folder          HTT_ENV_DIR      environment
#   HTT_START_MENU_DIR folder of the .lnk      HTT_DESKTOP_DIR  folder of the .lnk
#   HTT_REG_KEY        key path under HKCU     HTT_SKIP_DEPS=1  no pip
#   HTT_PIP_ARGS       extra pip arguments (e.g. --dry-run)
#   HTT_USE_RANGES=1   install from requirements*.txt, not the lock files
#   HTT_RUN_KEY        throw-away key under HKCU\Software\HoldToTalkTest\ that stands
#                      in for the Start with Windows (Run) key; with it the
#                      uninstaller really removes the entry from that key

$script:HttLinkName = 'Hold to talk.lnk'
$script:HttDefaultRegKey = 'Software\Microsoft\Windows\CurrentVersion\Uninstall\HoldToTalk'

function Get-HttEnv([string]$Name) {
    $v = [Environment]::GetEnvironmentVariable($Name)
    if ([string]::IsNullOrWhiteSpace($v)) { return $null }
    return $v
}

function Get-KnownFolderPath([string]$Guid, [string]$FallbackSpecial) {
    # SHGetKnownFolderPath: honours a redirected Desktop (e.g. into OneDrive).
    try {
        if (-not ('HttShell' -as [type])) {
            Add-Type -TypeDefinition @'
using System;
using System.Runtime.InteropServices;
public static class HttShell {
    [DllImport("shell32.dll")]
    static extern int SHGetKnownFolderPath(
        [MarshalAs(UnmanagedType.LPStruct)] Guid rfid, uint dwFlags,
        IntPtr hToken, out IntPtr ppszPath);
    public static string Get(Guid id) {
        IntPtr p;
        if (SHGetKnownFolderPath(id, 0, IntPtr.Zero, out p) != 0) return null;
        string s = Marshal.PtrToStringUni(p);
        Marshal.FreeCoTaskMem(p);
        return s;
    }
}
'@
        }
        $path = [HttShell]::Get([Guid]$Guid)
        if ($path) { return $path }
    } catch { }
    return [Environment]::GetFolderPath($FallbackSpecial)
}

function Get-HttLayout {
    param($InstallRoot, $EnvDir, $StartMenuDir, $DesktopDir, $RegKey, [switch]$Strict)
    $local = $env:LOCALAPPDATA
    if (-not $local) { $local = Join-Path $env:USERPROFILE 'AppData\Local' }
    if (-not $InstallRoot)   { $InstallRoot   = Get-HttEnv 'HTT_INSTALL_ROOT' }
    if (-not $EnvDir)        { $EnvDir        = Get-HttEnv 'HTT_ENV_DIR' }
    if (-not $StartMenuDir)  { $StartMenuDir  = Get-HttEnv 'HTT_START_MENU_DIR' }
    if (-not $DesktopDir)    { $DesktopDir    = Get-HttEnv 'HTT_DESKTOP_DIR' }
    if (-not $RegKey)        { $RegKey        = Get-HttEnv 'HTT_REG_KEY' }
    $given = @($InstallRoot, $EnvDir, $StartMenuDir, $DesktopDir, $RegKey) |
        Where-Object { $_ }
    $test = @($given).Count -gt 0
    if ($Strict -and $test -and @($given).Count -lt 5) {
        # A half-overridden run would mix real and temporary places.
        throw ('Test overrides must be given all together (install folder, environment, ' +
               'Start menu folder, Desktop folder and registry key), or not at all.')
    }

    if (-not $InstallRoot)  { $InstallRoot = Join-Path $local 'Programs\HoldToTalk' }
    if (-not $EnvDir)       { $EnvDir = Join-Path $local 'HoldToTalk\venv' }
    if (-not $StartMenuDir) {
        $StartMenuDir = Get-KnownFolderPath 'A77F5D77-2E2B-44C3-A6A2-ABA601054A51' 'Programs'
    }
    if (-not $DesktopDir) {
        $DesktopDir = Get-KnownFolderPath 'B4BFCC3A-DB2C-424C-B029-7FE99A87C641' 'DesktopDirectory'
    }
    if (-not $RegKey) { $RegKey = $script:HttDefaultRegKey }

    # The data folder is where the app itself puts it (settings.py):
    # HOLDTOTALK_HOME, else <environment's parent>\data.
    $appHome = Split-Path -Parent $EnvDir
    $data = Get-HttEnv 'HOLDTOTALK_HOME'
    if (-not $data) { $data = Join-Path $appHome 'data' }

    [pscustomobject]@{
        InstallRoot  = $InstallRoot
        EnvDir       = $EnvDir
        AppHome      = $appHome
        DataDir      = $data
        StartMenuLnk = Join-Path $StartMenuDir $script:HttLinkName
        DesktopLnk   = Join-Path $DesktopDir $script:HttLinkName
        RegKey       = $RegKey
        TestMode     = $test
    }
}

function Read-SourceFact([string]$Dir, [string]$File, [string]$Pattern, [string]$Default) {
    # One value out of a source file (the version and name live in settings.py).
    try {
        $text = Get-Content -Raw -LiteralPath (Join-Path $Dir $File) -Encoding UTF8
        $m = [regex]::Match($text, $Pattern)
        if ($m.Success) { return $m.Groups[1].Value }
    } catch { }
    return $Default
}

function Test-IsLink([string]$Path) {
    # $true when the path itself is a reparse point (a junction or a symbolic
    # link): it points somewhere else, and is never followed when removing.
    try {
        $a = [IO.File]::GetAttributes($Path)
        return [bool]($a -band [IO.FileAttributes]::ReparsePoint)
    } catch { return $false }
}

function Remove-TreeNoFollow([string]$Path) {
    # Removes a file or a folder tree WITHOUT following links. A reparse point
    # (junction, symbolic link) is removed as the link itself and what it
    # points to is left untouched. Windows PowerShell 5.1's Remove-Item -Recurse
    # can walk into a junction, so the tree is walked here by hand. Throws on
    # failure.
    $attr = [IO.File]::GetAttributes($Path)
    $isDir = [bool]($attr -band [IO.FileAttributes]::Directory)
    $isLink = [bool]($attr -band [IO.FileAttributes]::ReparsePoint)
    if (-not $isDir) {
        [IO.File]::SetAttributes($Path, [IO.FileAttributes]::Normal)
        [IO.File]::Delete($Path)
        return
    }
    if (-not $isLink) {
        # An ordinary folder (and, above, a file) with the ReadOnly attribute cannot be
        # deleted until the attribute is cleared; the other attributes are kept.
        if ($attr -band [IO.FileAttributes]::ReadOnly) {
            [IO.File]::SetAttributes($Path, ($attr -band (-bnot [IO.FileAttributes]::ReadOnly)))
        }
        foreach ($e in @([IO.Directory]::GetFileSystemEntries($Path))) { Remove-TreeNoFollow $e }
    } else {
        try { [IO.File]::SetAttributes($Path, [IO.FileAttributes]::Directory) } catch { }
    }
    # Directory.Delete(path, false) on a junction removes the link, not its target.
    [IO.Directory]::Delete($Path, $false)
}

function Remove-PathWithRetry([string]$Path, [int]$Tries = 5, [switch]$RefuseLink) {
    # Returns $true when the path is gone. A running program or a virus
    # scanner can hold a file for a moment, hence the retries. Links inside the
    # tree are removed as links (never followed). With -RefuseLink, a path that
    # is itself a link is not removed at all ($false).
    if ($RefuseLink -and (Test-IsLink $Path)) { return $false }
    for ($i = 1; $i -le $Tries; $i++) {
        if (-not (Test-Path -LiteralPath $Path) -and -not (Test-IsLink $Path)) { return $true }
        try { Remove-TreeNoFollow $Path }
        catch { Start-Sleep -Milliseconds (300 * $i) }
    }
    return -not ((Test-Path -LiteralPath $Path) -or (Test-IsLink $Path))
}

function Get-HttRunKeys {
    # The HKCU keys that hold the Start with Windows entry and Task Manager's
    # on/off flag for it. HTT_RUN_KEY (a test override, honoured only under
    # Software\HoldToTalkTest\) points both at a throw-away key, so the
    # removal can be tried without touching the real entry; tray.py reads
    # the same variable.
    $o = Get-HttEnv 'HTT_RUN_KEY'
    if ($o -and $o -like 'Software\HoldToTalkTest\*') {
        return [pscustomobject]@{ Run = $o; Approved = ($o.TrimEnd('\') + '\StartupApproved'); Override = $true }
    }
    return [pscustomobject]@{
        Run = 'Software\Microsoft\Windows\CurrentVersion\Run'
        Approved = 'Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\Run'
        Override = $false }
}

function Write-TermsRecord {
    # The record that the Terms of Use were accepted (terms.py reads the same
    # file): version, UTC time and how ("installer"; the app writes "app"). Local
    # only, nothing personal. install.bat -Yes writes none. Written to a temp file first, then moved.
    param([string]$DataDir, [string]$Version, [string]$How)
    New-Item -ItemType Directory -Force -Path $DataDir | Out-Null
    $at = (Get-Date).ToUniversalTime().ToString('yyyy-MM-dd\THH:mm:ss\Z', [Globalization.CultureInfo]::InvariantCulture)
    $nl = "`n"
    $json = '{' + $nl + '  "version": "' + $Version + '",' + $nl +
            '  "accepted_at": "' + $at + '",' + $nl +
            '  "how": "' + $How + '"' + $nl + '}' + $nl
    $tmp = Join-Path $DataDir ('.terms-' + [Guid]::NewGuid().ToString('N').Substring(0, 8) + '.tmp')
    [IO.File]::WriteAllText($tmp, $json, (New-Object Text.UTF8Encoding($false)))
    Move-Item -LiteralPath $tmp -Destination (Join-Path $DataDir 'terms-accepted.json') -Force
}

function Remove-AppDataFiles {
    # Delete what the app itself creates in its data folder, by name, and
    # nothing else: HOLDTOTALK_HOME (or a data folder that is a link) can
    # point anywhere, so the folder is never removed recursively. Anything
    # else is left in place and named. Returns the names left behind.
    param([string]$Dir)
    $left = @()
    $item = Get-Item -LiteralPath $Dir -Force -ErrorAction Stop
    if (-not $item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
        Write-Warning "$Dir is not a plain folder; left alone."
        return @($Dir)
    }
    # Every name the app can leave here: settings.py (config.json, its .tmp
    # while saving, .bad / .bad-<time> copies of an unreadable file, .copying
    # and .migrated while moving data from an older version), textrules.py
    # (the history database, its side files, and history.sqlite3.corrupt-<time>,
    # the quarantined copy of a damaged one, which holds dictated text),
    # terms.py and install.ps1 (terms-accepted.json, the record of accepting
    # the Terms of Use, and its .terms-*.tmp while being written) and
    # dictate.py (logs\dictate.log, .1), and audiomute.py (audio-muted.json, the
    # list of what was muted while the key is held, and .audio-muted-*.tmp
    # while it is being written).
    $files = '^config\.json$', '^config\.json\.(bad|migrated|copying)[^\\/]*$', '^\.cfg-[^\\/]*\.tmp$',
             '^terms-accepted\.json$', '^\.terms-[^\\/]*\.tmp$',
             '^audio-muted\.json$', '^\.audio-muted-[^\\/]*\.tmp$',
             '^history\.sqlite3$', '^history\.sqlite3(-wal|-shm|-journal)$',
             '^history\.sqlite3\.(migrated|copying|corrupt-)[^\\/]*$'
    foreach ($f in @(Get-ChildItem -LiteralPath $Dir -Force -File -ErrorAction SilentlyContinue)) {
        $known = $false
        foreach ($rx in $files) { if ($f.Name -match $rx) { $known = $true; break } }
        if ($known) {
            try { Remove-Item -LiteralPath $f.FullName -Force -ErrorAction Stop }
            catch { $left += $f.Name }
        } else { $left += $f.Name }
    }
    foreach ($d in @(Get-ChildItem -LiteralPath $Dir -Force -Directory -ErrorAction SilentlyContinue)) {
        if ($d.Name -ieq 'logs' -and -not ($d.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
            foreach ($f in @(Get-ChildItem -LiteralPath $d.FullName -Force -ErrorAction SilentlyContinue)) {
                if (-not $f.PSIsContainer -and $f.Name -match '^dictate\.log(\.1)?$') {
                    try { Remove-Item -LiteralPath $f.FullName -Force -ErrorAction Stop }
                    catch { $left += ('logs\' + $f.Name) }
                } else { $left += ('logs\' + $f.Name) }
            }
            if (-not @(Get-ChildItem -LiteralPath $d.FullName -Force -ErrorAction SilentlyContinue)) {
                Remove-Item -LiteralPath $d.FullName -Force -ErrorAction SilentlyContinue
            }
        } else { $left += ($d.Name + '\') }
    }
    if (-not $left.Count) { Remove-Item -LiteralPath $Dir -Force -ErrorAction SilentlyContinue }
    return @($left)
}

function Quote-NativeArg([string]$a) {
    if ($a -eq '') { return '""' }
    if ($a -notmatch '[\s"]') { return $a }
    $a = $a -replace '(\\*)"', '$1$1\"'
    $a = $a -replace '(\\+)$', '$1$1'
    return '"' + $a + '"'
}

function Invoke-Capture {
    # Run a program without a window and return @{Code; Out; Err}.
    param([string]$Exe, [string[]]$ArgList = @(), [int]$TimeoutSec = 60)
    $psi = New-Object System.Diagnostics.ProcessStartInfo
    $psi.FileName = $Exe
    $psi.Arguments = (($ArgList | ForEach-Object { Quote-NativeArg $_ }) -join ' ')
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow = $true
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $psi.StandardOutputEncoding = [Text.Encoding]::UTF8
    $psi.StandardErrorEncoding = [Text.Encoding]::UTF8
    try { $p = [System.Diagnostics.Process]::Start($psi) }
    catch { return @{ Code = -2; Out = ''; Err = $_.Exception.Message } }
    $out = $p.StandardOutput.ReadToEndAsync()
    $err = $p.StandardError.ReadToEndAsync()
    if (-not $p.WaitForExit($TimeoutSec * 1000)) {
        try { $p.Kill() } catch { }
        return @{ Code = -1; Out = ''; Err = 'timed out' }
    }
    $p.WaitForExit()
    return @{ Code = $p.ExitCode; Out = $out.Result; Err = $err.Result }
}

function Invoke-Console {
    # Run a program in this console (its output and progress bars are shown
    # as it prints them) and return its exit code. Checked for the installer
    # window: when this script's own stdout and stderr are pipes and it has no
    # console window, -NoNewWindow still hands the child those same pipes, so
    # pip's and the download's lines arrive one by one in the parent's reader.
    param([string]$Exe, [string[]]$ArgList = @())
    $line = (($ArgList | ForEach-Object { Quote-NativeArg $_ }) -join ' ')
    $p = Start-Process -FilePath $Exe -ArgumentList $line -NoNewWindow -PassThru
    $null = $p.Handle          # keeps the exit code readable after it exits
    $p.WaitForExit()
    return $p.ExitCode
}

function Get-AppProcesses([string]$EnvDir) {
    # python.exe / pythonw.exe launchers of this environment still running.
    try {
        $prefix = (Join-Path $EnvDir 'Scripts') + '\'
        Get-CimInstance Win32_Process -Filter "Name='pythonw.exe' OR Name='python.exe'" |
            Where-Object { $_.ExecutablePath -and $_.ExecutablePath.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase) }
    } catch { @() }
}

function Stop-RunningApp {
    # Ask a running copy to quit (dictate.py --quit) and wait for it. Never
    # kills anything. In test mode the quit goes to a throw-away test
    # instance (--config), so it can never reach the real app.
    param($Layout, [string]$ProgramDir)
    $py = Join-Path $Layout.EnvDir 'Scripts\python.exe'
    $script = Join-Path $ProgramDir 'dictate.py'
    if (-not ((Test-Path -LiteralPath $py) -and (Test-Path -LiteralPath $script))) {
        Write-Host '  Not running (no earlier install to ask).'
        return
    }
    $quitArgs = @('-B', $script, '--quit')
    if ($Layout.TestMode) {
        $quitArgs += @('--config', (Join-Path $Layout.AppHome 'quit-probe\config.json'))
    }
    $r = Invoke-Capture $py $quitArgs 30
    if ($r.Code -eq 0) { Write-Host '  Asked the running app to quit.' }
    else { Write-Host '  The app was not running.' }
    if (-not $Layout.TestMode) {
        $deadline = (Get-Date).AddSeconds(20)
        while ((Get-Date) -lt $deadline -and @(Get-AppProcesses $Layout.EnvDir).Count -gt 0) {
            Start-Sleep -Milliseconds 500
        }
        if (@(Get-AppProcesses $Layout.EnvDir).Count -gt 0) {
            Write-Host '  It is still running. If a step below fails because a file is in use,'
            Write-Host '  quit it from its icon in the notification area (Quit) and run this again.'
        }
    }
}
