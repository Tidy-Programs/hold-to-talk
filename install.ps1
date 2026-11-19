# install.ps1 - installs (or updates) Hold to talk for the current user.
# Started by install.bat; also run by start.bat with -EnvOnly. Windows
# PowerShell 5.1, no administrator rights. Keep this file plain ASCII.
#
#   install.bat                     the normal way (double-click it)
#   install.bat -Yes                no questions: take the default answers. This
#                                   does NOT accept the Terms of Use for whoever
#                                   uses the app later: no acceptance is
#                                   recorded, and the app shows the terms and asks
#
# Test and developer switches (see htt-common.ps1 for the overrides):
#   -EnvOnly     only find Python and make the environment ready (start.bat)
#   -NoDeps      do not run pip (also HTT_SKIP_DEPS=1)
#   -SkipModel   do not offer the speech-model download
#   -NoLaunch    do not offer to start the app at the end
#   -Gpu yes|no  pretend an NVIDIA card is (not) present; default: look
#   -PipArgs     extra pip arguments, e.g. --dry-run (also HTT_PIP_ARGS)
#   -UseRanges   install from the version ranges (requirements.txt) instead of the
#                exact, hash-checked lock files (also HTT_USE_RANGES=1)
#   -InstallRoot -EnvDir -StartMenuDir -DesktopDir -RegKey   see htt-common.ps1
#
# Switches for the installer window (install-gui.ps1 runs this script hidden and
# reads what it prints; install.bat with no arguments opens that window):
#   -Console     no effect; install.bat -Console picks the text installer
#   -Check       change nothing: print one line starting @@htt with the facts the
#                window shows (Python, the NVIDIA card, sizes, ...) and exit 0
#   -Progress    for the window: never asks anything, prints lines starting @@htt
#                between the normal text (step, activity, warning, error, done),
#                never starts the app. Needs -AcceptedTerms (else it stops with
#                exit code 3 and changes nothing); exit code 4: no suitable Python
#   -AcceptedTerms <version>   the installer window passes this after a person ticked
#                the box and chose "I agree" in it. It is the same statement as
#                typing I agree here, so passing it by hand means the same. Only
#                together with -Progress; the version must be the one in TERMS.md
#                (otherwise nothing is changed). The acceptance is recorded as
#                "installer". It cannot be combined with -Yes
#   -Nvidia yes|no|ask   the answer to the NVIDIA runtime question (default ask)
#   -Model yes|no|ask    the answer to "Download the speech model now?" (default ask)
#   With -Progress, ask means yes.
#   -DesktopShortcut yes|no   no: do not create the desktop shortcut (default yes)

[CmdletBinding()]
param(
    [switch]$EnvOnly,
    [switch]$Yes,
    [switch]$NoDeps,
    [switch]$SkipModel,
    [switch]$NoLaunch,
    [ValidateSet('auto', 'yes', 'no')][string]$Gpu = 'auto',
    [string]$PipArgs,
    [switch]$UseRanges,
    [string]$InstallRoot,
    [string]$EnvDir,
    [string]$StartMenuDir,
    [string]$DesktopDir,
    [string]$RegKey,
    [switch]$Console,
    [switch]$Check,
    [switch]$Progress,
    [string]$AcceptedTerms,
    [ValidateSet('yes', 'no', 'ask')][string]$Nvidia = 'ask',
    [ValidateSet('yes', 'no', 'ask')][string]$Model = 'ask',
    [ValidateSet('yes', 'no')][string]$DesktopShortcut = 'yes'
)

$ErrorActionPreference = 'Stop'
$SrcDir = Split-Path -Parent $MyInvocation.MyCommand.Path
. (Join-Path $SrcDir 'htt-common.ps1')

if (-not $NoDeps -and (Get-HttEnv 'HTT_SKIP_DEPS') -eq '1') { $NoDeps = $true }
if (-not $PipArgs) { $PipArgs = Get-HttEnv 'HTT_PIP_ARGS' }
if (-not $UseRanges -and (Get-HttEnv 'HTT_USE_RANGES') -eq '1') { $UseRanges = $true }

$PythonMinMinor = 10
$PythonMaxMinor = 13
# The helper scripts below are written to a folder made fresh for this run
# (only this account can open it) and run with python -I (isolated: neither
# the script's folder, the current folder nor PYTHON* variables are put on
# the module path), so a file planted in a shared folder such as %TEMP% (a
# json.py, say) is never imported by them.
$script:WorkDir = $null
$script:NoPython = $false

function Get-WorkDir {
    if ($script:WorkDir) { return $script:WorkDir }
    $dir = Join-Path ([IO.Path]::GetTempPath()) ('htt-' + [Guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $dir -ErrorAction Stop | Out-Null   # fails if it exists
    try {
        $acl = New-Object Security.AccessControl.DirectorySecurity
        $acl.SetAccessRuleProtection($true, $false)
        $me = [Security.Principal.WindowsIdentity]::GetCurrent().User
        $acl.AddAccessRule((New-Object Security.AccessControl.FileSystemAccessRule(
            $me, 'FullControl', 'ContainerInherit,ObjectInherit', 'None', 'Allow')))
        Set-Acl -LiteralPath $dir -AclObject $acl
    } catch { }      # the fresh folder already inherits this account's access
    $script:WorkDir = $dir
    return $dir
}

$ProbeCode = @'
import json, platform, struct, sys
try:
    import tkinter
    tk = True
except Exception:
    tk = False
try:
    import ensurepip, venv
    venv_ok = True
except Exception:
    venv_ok = False
print(json.dumps({"exe": sys.executable, "ver": list(sys.version_info[:3]),
                  "bits": struct.calcsize("P") * 8,
                  "machine": platform.machine(), "tk": tk, "venv": venv_ok}))
'@

$ModelCode = @'
import sys
from faster_whisper import download_model
name, mode = sys.argv[1], sys.argv[2]
rev = sys.argv[3] if len(sys.argv) > 3 and sys.argv[3] else None
if mode == "check":
    try:
        download_model(name, local_files_only=True, revision=rev)
    except Exception:
        sys.exit(3)
    sys.exit(0)
download_model(name, revision=rev)
'@

function Say([string]$Text = '') { if (-not $EnvOnly) { Write-Host $Text } }

# The step titles, in order, as -Check reports them and as the Step calls in the
# main part name them ("1. Looking for Python" is the first one).
$StepTitles = @(
    'Looking for Python',
    'Checking the files',
    'Making sure the app is not running',
    'Copying the program files',
    'Python environment and libraries',
    'Speech model',
    'Shortcuts and the Installed apps entry'
)

# -- lines for the installer window (-Check, -Progress) ------------------------------

function Write-Marker($Object) {
    # "@@htt " and one line of JSON. ConvertTo-Json escapes line breaks and quotes,
    # so a path with a space or a non-ASCII letter stays on one line.
    $json = ConvertTo-Json -InputObject $Object -Compress -Depth 6
    Write-Host ('@@htt ' + $json)
}

function Activity([string]$Text) {
    # A longer phase inside a step (only -Progress shows it to the window).
    if ($Progress) { Write-Marker ([ordered]@{ kind = 'activity'; text = $Text }) }
}

function Warn-User([string]$Text) {
    # A non-fatal problem the person should see on the last page of the window.
    Write-Warning $Text
    if ($Progress) { Write-Marker ([ordered]@{ kind = 'warning'; text = $Text }) }
}

function Step([string]$Text) {
    if (-not $EnvOnly) { Write-Host ''; Write-Host $Text -ForegroundColor Cyan }
    if ($Progress) {
        $m = [regex]::Match($Text, '^(\d+)\.\s+(.*)$')
        $n = if ($m.Success) { [int]$m.Groups[1].Value } else { 0 }
        $title = if ($m.Success) { $m.Groups[2].Value } else { $Text }
        Write-Marker ([ordered]@{ kind = 'step'; n = $n; total = $StepTitles.Count; title = $title })
    }
}

function Ask-YesNo([string]$Question, [bool]$Default = $true) {
    # -Progress never reads input: the default answer, every time.
    if ($Yes -or $Progress) { return $Default }
    $hint = if ($Default) { 'Y/n' } else { 'y/N' }
    while ($true) {
        $a = Read-Host "$Question [$hint]"
        if ($null -eq $a) { return $Default }      # input closed: the default answer
        $a = $a.Trim().ToLower()
        if ($a -eq '') { return $Default }
        if ($a -in @('y', 'yes')) { return $true }
        if ($a -in @('n', 'no')) { return $false }
    }
}

# -- Python ----------------------------------------------------------------------

function Read-PythonInfo([string]$Exe, [string[]]$Pre) {
    # Run the probe with a candidate; returns the parsed facts or $null and
    # the reason it could not be used.
    $r = Invoke-Capture $Exe (@($Pre) + @('-I', $script:ProbeFile)) 60
    if ($r.Code -ne 0) { return @{ Info = $null; Why = 'did not run' } }
    try { $info = $r.Out.Trim() | ConvertFrom-Json } catch { return @{ Info = $null; Why = 'gave no answer' } }
    return @{ Info = $info; Why = $null }
}

function Get-PythonProblem($info) {
    # $null when the Python is usable, else a short reason.
    if ($info.exe -match '\\WindowsApps\\') { return 'is the Microsoft Store shortcut or version' }
    if ($info.machine -ne 'AMD64' -or $info.bits -ne 64) {
        return "is not 64-bit x64 ($($info.machine), $($info.bits)-bit)"
    }
    $major = [int]$info.ver[0]; $minor = [int]$info.ver[1]
    if ($major -ne 3 -or $minor -lt $PythonMinMinor -or $minor -gt $PythonMaxMinor) {
        return "is version $($info.ver -join '.'), outside 3.$PythonMinMinor - 3.$PythonMaxMinor"
    }
    if (-not $info.tk) { return 'has no tkinter (reinstall it with "tcl/tk and IDLE" ticked)' }
    if (-not $info.venv) { return 'cannot create environments (venv is missing)' }
    return $null
}

function Search-Python {
    # -> @{ Python = the usable one or $null; Notes = what was looked at }
    $script:ProbeFile = Join-Path (Get-WorkDir) 'probe.py'
    Set-Content -LiteralPath $script:ProbeFile -Value $ProbeCode -Encoding ASCII
    $candidates = @()
    $forced = Get-HttEnv 'HTT_PYTHON'
    if ($forced) { $candidates += , @{ Label = "HTT_PYTHON ($forced)"; Exe = $forced; Pre = @() } }
    foreach ($v in '3.12', '3.13', '3.11', '3.10') {
        $candidates += , @{ Label = "py -$v"; Exe = 'py'; Pre = @("-$v") }
    }
    $candidates += , @{ Label = 'python'; Exe = 'python'; Pre = @() }

    $notes = @()
    foreach ($c in $candidates) {
        $cmd = Get-Command $c.Exe -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
        if (-not $cmd) { $notes += "  $($c.Label): not found"; continue }
        if ($cmd.Source -match '\\WindowsApps\\') {
            $notes += "  $($c.Label): $($cmd.Source) is the Microsoft Store shortcut, not Python"
            continue
        }
        $got = Read-PythonInfo $cmd.Source $c.Pre
        if (-not $got.Info) { $notes += "  $($c.Label): $($got.Why)"; continue }
        $problem = Get-PythonProblem $got.Info
        if ($problem) { $notes += "  $($c.Label): $($got.Info.exe) $problem"; continue }
        return @{ Notes = $notes; Python = [pscustomobject]@{
            Exe = $got.Info.exe; Version = ($got.Info.ver -join '.'); Via = $c.Label
        } }
    }
    return @{ Notes = $notes; Python = $null }
}

function Find-Python {
    $found = Search-Python
    if ($found.Python) { return $found.Python }
    $notes = $found.Notes
    if ($Progress) { $script:NoPython = $true }      # exit code 4
    $msg = @(
        'Hold to talk needs 64-bit Python, and no suitable copy was found on this PC.',
        '',
        'What to install (once):',
        '  1. Go to https://www.python.org/downloads/windows/',
        '  2. Download the "Windows installer (64-bit)" for Python 3.12.',
        '     On a PC with an ARM processor, still choose this 64-bit (x64) installer:',
        '     it runs there. Do not use the Microsoft Store version.',
        '  3. Run it. Leave "tcl/tk and IDLE" and "py launcher" ticked (they are by default).',
        '  4. Then run install.bat again.',
        '',
        'Python 3.12 is the tested version; 3.10 to 3.13 are accepted.',
        '',
        'What was looked at:') + $notes
    throw ($msg -join [Environment]::NewLine)
}

# -- environment -----------------------------------------------------------------

function Get-ReqFile([string]$Dir, [string]$Base) {
    # The file pip installs from: the lock file (exact versions and hashes)
    # unless -UseRanges was given or there is none; requirements*.txt are the
    # developer-facing ranges the locks are made from (see README).
    $lock = Join-Path $Dir ($Base + '.lock.txt')
    if (-not $UseRanges -and (Test-Path -LiteralPath $lock)) { return $lock }
    return (Join-Path $Dir ($Base + '.txt'))
}

function Get-RequirementsHash([string]$Dir, [bool]$WithCuda) {
    $parts = @((Get-Content -Raw -LiteralPath (Get-ReqFile $Dir 'requirements')) -replace "`r`n", "`n")
    if ($WithCuda) {
        $parts += '--cuda--'
        $parts += (Get-Content -Raw -LiteralPath (Get-ReqFile $Dir 'requirements-cuda')) -replace "`r`n", "`n"
    } else {
        $parts += '--no-cuda--'
    }
    $bytes = [Text.Encoding]::UTF8.GetBytes(($parts -join "`n"))
    $sha = [Security.Cryptography.SHA256]::Create()
    try { return ([BitConverter]::ToString($sha.ComputeHash($bytes)) -replace '-', '').ToLower() }
    finally { $sha.Dispose() }
}

function Test-NvidiaGpu {
    if ($Gpu -eq 'yes') { return $true }
    if ($Gpu -eq 'no') { return $false }
    if (Test-Path -LiteralPath (Join-Path $env:SystemRoot 'System32\nvidia-smi.exe')) { return $true }
    try {
        foreach ($v in @(Get-CimInstance Win32_VideoController)) {
            if ($v.Name -match 'NVIDIA') { return $true }
        }
    } catch { }
    return $false
}

function Invoke-Pip([string]$EnvPy, [string[]]$Requirements) {
    # --only-binary :all: -> nothing is ever built from source (no build
    # script of a package runs on this PC). A lock file carries hashes, so
    # pip also refuses any file that is not the one that was tested.
    $argv = @('-I', '-m', 'pip', 'install', '--disable-pip-version-check', '--only-binary', ':all:')
    foreach ($r in $Requirements) { $argv += @('-r', $r) }
    if ($PipArgs) { $argv += ($PipArgs -split '\s+' | Where-Object { $_ }) }
    return ((Invoke-Console $EnvPy $argv) -eq 0)
}

function Install-Requirements([string]$EnvPy, [string]$Dir, [string]$Base) {
    $file = Get-ReqFile $Dir $Base
    if (Invoke-Pip $EnvPy @($file)) { return $true }
    if ($file -like '*.lock.txt') {
        # The locks are tested with Python 3.12 on 64-bit Windows; another
        # accepted Python may lack a wheel of an exact version.
        Warn-User 'Installing the exact, tested versions failed. Trying the version ranges instead.'
        return (Invoke-Pip $EnvPy @((Join-Path $Dir ($Base + '.txt'))))
    }
    return $false
}

function Resolve-GpuChoice([string]$ProgramDir, $Layout, [bool]$Detected) {
    # Whether to install the NVIDIA runtime (proprietary, under NVIDIA's own
    # licence). $false when no NVIDIA card was found. An interactive install
    # asks (default yes); -Yes installs it and prints the notice. The answer
    # is recorded by the requirements hash the environment keeps (with or
    # without the runtime), so a run that finds it already installed does not
    # ask again, and start.bat (-EnvOnly) respects an earlier "no". A "no"
    # installs a working processor-only app; run the installer again to add it.
    if (-not $Detected) { return $false }
    $hashFile = Join-Path $Layout.EnvDir 'htt-requirements.sha256'
    $have = if (Test-Path -LiteralPath $hashFile) { (Get-Content -LiteralPath $hashFile -Raw).Trim() } else { '' }
    if ($have -and $have -eq (Get-RequirementsHash $ProgramDir $true)) { return $true }
    if ($EnvOnly) {
        if ($have -and $have -eq (Get-RequirementsHash $ProgramDir $false)) { return $false }
        return $true
    }
    Write-Host ''
    Write-Host '  An NVIDIA graphics card was found. Speech recognition can use it, but only with' -ForegroundColor Cyan
    Write-Host '  NVIDIA''s runtime: about 1.3 GB to download, about 2 GB on disk.' -ForegroundColor Cyan
    Write-Host '  Installing it means accepting NVIDIA''s licence for those files.' -ForegroundColor Cyan
    Write-Host '  If you answer no, the app is installed without it and recognises speech on the'
    Write-Host '  processor, which is slower. You can run the installer again later to add it.'
    if ($Yes) {
        Write-Host '  (-Yes: installing the NVIDIA runtime, the default answer.)'
        return $true
    }
    # The window passes its person's answer (-Nvidia); "ask" there means yes.
    if ($Nvidia -eq 'yes') {
        Write-Host '  (-Nvidia yes: installing the NVIDIA runtime.)'
        return $true
    }
    if ($Nvidia -eq 'no') {
        Write-Host '  Not installing the NVIDIA runtime. The app will use the processor (slower).'
        return $false
    }
    if (Ask-YesNo '  Install the NVIDIA runtime?' $true) { return $true }
    Write-Host '  Not installing the NVIDIA runtime. The app will use the processor (slower).'
    return $false
}

function Ensure-Environment($Python, $Layout, [string]$ProgramDir, [bool]$UseGpu) {
    $envDir = $Layout.EnvDir
    $envPy = Join-Path $envDir 'Scripts\python.exe'
    $make = $true
    if (Test-Path -LiteralPath $envPy) {
        $got = Read-PythonInfo $envPy @()
        $problem = if ($got.Info) { Get-PythonProblem $got.Info } else { $got.Why }
        if (-not $problem) {
            $make = $false
            Say "  Python environment: found and working ($($got.Info.ver -join '.'))."
        } else {
            Write-Host "  The Python environment does not work ($problem). Making a new one."
        }
    } elseif (Test-Path -LiteralPath $envDir) {
        Write-Host '  The Python environment is incomplete. Making a new one.'
    } else {
        Say '  Making the Python environment (a private copy of Python for this app).'
    }
    if ($make) {
        if (-not (Remove-PathWithRetry $envDir)) {
            throw "The old Python environment could not be removed ($envDir). Quit Hold to talk (its icon in the notification area > Quit) and run this again."
        }
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $envDir) | Out-Null
        Activity 'Making the Python environment (a private copy of Python for this app)'
        $code = Invoke-Console $Python.Exe @('-I', '-m', 'venv', $envDir)
        if ($code -ne 0 -or -not (Test-Path -LiteralPath $envPy)) {
            throw "Python could not create the environment at $envDir."
        }
    }

    $hashFile = Join-Path $envDir 'htt-requirements.sha256'
    $want = Get-RequirementsHash $ProgramDir $UseGpu
    $have = if (Test-Path -LiteralPath $hashFile) { (Get-Content -LiteralPath $hashFile -Raw).Trim() } else { '' }
    if ($NoDeps) {
        Say '  Skipping the dependency download (-NoDeps).'
        return $envPy
    }
    if ($want -eq $have) {
        Say '  Dependencies: up to date.'
        return $envPy
    }
    Write-Host ''
    Write-Host '  Installing the libraries the app needs (a few hundred MB; takes a few minutes).' -ForegroundColor Cyan
    Activity 'Installing the libraries the app needs'
    if (-not (Install-Requirements $envPy $ProgramDir 'requirements')) {
        throw 'Installing the libraries failed. Check your internet connection and run install.bat again; it carries on where it stopped.'
    }
    if ($UseGpu) {
        Write-Host ''
        Write-Host '  Installing the NVIDIA runtime (about 1.3 GB to download).' -ForegroundColor Cyan
        Activity 'Installing the NVIDIA runtime (about 1.3 GB to download)'
        if (-not (Install-Requirements $envPy $ProgramDir 'requirements-cuda')) {
            Warn-User 'The NVIDIA runtime could not be installed. The app will recognise speech on the processor instead. Run install.bat again to retry.'
            return $envPy
        }
    }
    Set-Content -LiteralPath $hashFile -Value $want -Encoding ASCII
    return $envPy
}

# -- the rest of the install -----------------------------------------------------

function Test-ManifestEntry([string]$Entry) {
    # $null when a line of install-files.txt is a plain relative path inside the
    # app folder; otherwise why not. Refused: a rooted path (leading slash or
    # backslash), a drive letter or any colon, "..", a quote character, and braces
    # (the Setup.exe build would read them as Inno constants). tools/build_setup.py
    # applies the same rules.
    if ($Entry -match '^[\\/]') { return 'it starts with a slash or backslash' }
    if ($Entry -match '^[A-Za-z]:') { return 'it starts with a drive letter' }
    if ($Entry.Contains(':')) { return 'it contains a colon' }
    if ($Entry.Contains('..')) { return 'it contains ".."' }
    if ($Entry.Contains('"') -or $Entry.Contains("'")) { return 'it contains a quote character' }
    if ($Entry.Contains('{') -or $Entry.Contains('}')) { return 'it contains a brace' }
    return $null
}

function Get-Manifest([string]$Dir) {
    $path = Join-Path $Dir 'install-files.txt'
    if (-not (Test-Path -LiteralPath $path)) { throw "install-files.txt is missing next to install.bat ($Dir). Extract the whole ZIP first." }
    Get-Content -LiteralPath $path | ForEach-Object {
        $l = $_.Trim()
        if ($l -and -not $l.StartsWith('#')) {
            $bad = Test-ManifestEntry $l
            if ($bad) { throw "install-files.txt has an entry that is not allowed ($l): $bad." }
            $l -replace '/', '\'
        }
    }
}

function Test-DownloadIntact([string]$Dir) {
    # The Setup.exe carries a list of hashes, files.sha256, next to this script
    # (one line "<64 lowercase hex>  <path with forward slashes>" per file of
    # install-files.txt, after the line "# htt-sha256 1"). If it is there, every
    # listed file must be present and match, and nothing else may be in it. A ZIP
    # or a checkout has no such file: nothing is checked and nothing is printed.
    $sumFile = Join-Path $Dir 'files.sha256'
    if (-not (Test-Path -LiteralPath $sumFile)) { return }
    $damaged = "This download is damaged or was changed. Download it again from the project's page."
    $want = @{}
    $format = $false
    foreach ($raw in (Get-Content -LiteralPath $sumFile -Encoding ASCII)) {
        $l = $raw.TrimEnd()
        if (-not $l) { continue }
        if ($l -eq '# htt-sha256 1') { $format = $true; continue }
        $m = [regex]::Match($l, '^([0-9a-f]{64})  (\S.*)$')
        if (-not $m.Success) { throw $damaged }
        $key = $m.Groups[2].Value.ToLowerInvariant()
        if ($want.ContainsKey($key)) { throw $damaged }
        $want[$key] = $m.Groups[1].Value
    }
    if (-not $format) { throw $damaged }
    $files = @(Get-Manifest $Dir)
    if ($files.Count -ne $want.Count) { throw $damaged }
    foreach ($rel in $files) {
        $key = ($rel -replace '\\', '/').ToLowerInvariant()
        if (-not $want.ContainsKey($key)) { throw $damaged }
        $path = Join-Path $Dir $rel
        if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw $damaged }
        $have = (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($have -ne $want[$key]) { throw $damaged }
    }
}

function Copy-WithRetry([string]$From, [string]$To) {
    for ($i = 1; $i -le 4; $i++) {
        try { Copy-Item -LiteralPath $From -Destination $To -Force -ErrorAction Stop; return }
        catch {
            if ($i -eq 4) { throw "Could not copy $(Split-Path -Leaf $From) ($($_.Exception.Message)). If Hold to talk is running, quit it from its icon in the notification area and run this again." }
            Start-Sleep -Milliseconds (400 * $i)
        }
    }
}

function Install-Files($Files, [string]$From, [string]$To) {
    foreach ($f in $Files) {
        $bad = Test-ManifestEntry ([string]$f)
        if ($bad) { throw "A file name in the list is not allowed ($f): $bad." }
    }
    $same = [IO.Path]::GetFullPath($From).TrimEnd('\') -ieq [IO.Path]::GetFullPath($To).TrimEnd('\')
    if ($same) { Say '  Already running from the install folder: nothing to copy.'; return }
    $missing = @($Files | Where-Object { -not (Test-Path -LiteralPath (Join-Path $From $_)) })
    if ($missing.Count) { throw ('Files missing from this folder (extract the whole ZIP): ' + ($missing -join ', ')) }
    New-Item -ItemType Directory -Force -Path $To | Out-Null
    foreach ($rel in $Files) {
        $dst = Join-Path $To $rel
        $toFull = [IO.Path]::GetFullPath($To).TrimEnd('\') + '\'
        if (-not [IO.Path]::GetFullPath($dst).StartsWith($toFull, [StringComparison]::OrdinalIgnoreCase)) { throw "A file name in the list points outside the install folder ($rel)." }
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $dst) | Out-Null
        Copy-WithRetry (Join-Path $From $rel) $dst
        try { Unblock-File -LiteralPath $dst -ErrorAction Stop } catch { }
    }
    # Files an older version installed that are no longer part of the app.
    $known = @{}; foreach ($f in $Files) { $known[$f.ToLower()] = $true }
    $ext = '.py', '.bat', '.ps1', '.ico', '.md', '.txt', '.html', '.css', '.js'
    Get-ChildItem -LiteralPath $To -Recurse -File -ErrorAction SilentlyContinue |
        Where-Object { $_.FullName -notmatch '\\__pycache__\\' -and $ext -contains $_.Extension.ToLower() } |
        ForEach-Object {
            $rel = $_.FullName.Substring($To.TrimEnd('\').Length + 1).ToLower()
            if (-not $known.ContainsKey($rel)) {
                Say "  Removing an old file: $rel"
                Remove-Item -LiteralPath $_.FullName -Force -ErrorAction SilentlyContinue
            }
        }
    Say "  Copied $(@($Files).Count) files to $To"
}

function New-Shortcut([string]$Path, [string]$Target, [string]$Arguments, [string]$WorkDir, [string]$Icon, [string]$Description) {
    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $Path) | Out-Null
    $shell = New-Object -ComObject WScript.Shell
    try {
        $lnk = $shell.CreateShortcut($Path)
        $lnk.TargetPath = $Target
        $lnk.Arguments = $Arguments
        $lnk.WorkingDirectory = $WorkDir
        $lnk.IconLocation = "$Icon,0"
        $lnk.Description = $Description
        $lnk.Save()
    } finally {
        [void][Runtime.InteropServices.Marshal]::ReleaseComObject($shell)
    }
}

function Get-DirSizeKB([string[]]$Dirs) {
    $sum = 0
    foreach ($d in $Dirs) {
        if (Test-Path -LiteralPath $d) {
            $m = Get-ChildItem -LiteralPath $d -Recurse -File -Force -ErrorAction SilentlyContinue | Measure-Object Length -Sum
            if ($m.Sum) { $sum += $m.Sum }
        }
    }
    return [int][Math]::Min([Math]::Ceiling($sum / 1KB), [int]::MaxValue)
}

function Write-UninstallEntry($Layout, [string]$Name, [string]$Version) {
    $k = "HKCU:\$($Layout.RegKey)"
    New-Item -Path $k -Force | Out-Null
    $icon = Join-Path $Layout.InstallRoot 'app.ico'
    $values = [ordered]@{
        DisplayName     = $Name
        DisplayVersion  = $Version
        Publisher       = 'Hold to talk contributors'
        DisplayIcon     = $icon
        InstallLocation = $Layout.InstallRoot
        UninstallString = '"' + (Join-Path $Layout.InstallRoot 'uninstall.bat') + '"'
        InstallDate     = (Get-Date).ToString('yyyyMMdd')
    }
    foreach ($n in $values.Keys) { Set-ItemProperty -Path $k -Name $n -Value $values[$n] -Type String }
    Set-ItemProperty -Path $k -Name NoModify -Value 1 -Type DWord
    Set-ItemProperty -Path $k -Name NoRepair -Value 1 -Type DWord
    Set-ItemProperty -Path $k -Name EstimatedSize -Value (Get-DirSizeKB @($Layout.InstallRoot, $Layout.EnvDir)) -Type DWord
}

function Get-ModelName([string]$ProgramDir, [bool]$UseGpu) {
    $key = if ($UseGpu) { 'model' } else { 'cpu_model' }
    $default = if ($UseGpu) { 'large-v3-turbo' } else { 'small' }
    return Read-SourceFact $ProgramDir 'settings.py' ('"' + $key + '":\s*"([^"]+)"') $default
}

function Test-ModelOnDisk([string]$EnvPy, [string]$ProgramDir, [string]$name) {
    # -> @{ Code; Rev }: Code 0 = the model is on this PC, 3 = it is not,
    # anything else = it could not be checked.
    $script:ModelFile = Join-Path (Get-WorkDir) 'model.py'
    Set-Content -LiteralPath $script:ModelFile -Value $ModelCode -Encoding ASCII
    # The exact revision the app loads (MODEL_REVISIONS in stt.py); empty when
    # the model is not pinned.
    $rev = Read-SourceFact $ProgramDir 'stt.py' ('"' + [regex]::Escape($name) + '":\s*"([0-9a-f]{40})"') ''
    $r = Invoke-Capture $EnvPy @('-I', $script:ModelFile, $name, 'check', $rev) 120
    return @{ Code = $r.Code; Rev = $rev }
}

function Offer-ModelDownload([string]$EnvPy, [string]$ProgramDir, [bool]$UseGpu) {
    $name = Get-ModelName $ProgramDir $UseGpu
    $size = if ($UseGpu) { '1.5 GB' } else { '0.5 GB' }
    $checked = Test-ModelOnDisk $EnvPy $ProgramDir $name
    $r = @{ Code = $checked.Code }; $rev = $checked.Rev
    if ($r.Code -eq 0) { Say "  The speech model ($name) is already on this PC."; return }
    if ($r.Code -ne 3) { Warn-User 'Could not check for the speech model. It will download on first start.'; return }
    Say "  The app needs a speech model ($name, about $size to download)."
    Say '  If you skip this, the first start downloads it and shows "Getting ready" for'
    Say '  many minutes with nothing else to see. Downloading it now shows progress.'
    # -Model is the answer the installer window passes; "ask" asks (in the window: yes).
    $download = switch ($Model) {
        'yes' { $true }
        'no' { $false }
        default { Ask-YesNo "  Download the speech model now?" $true }
    }
    if (-not $download) {
        Say '  Skipped. It will download the first time the app starts.'
        return
    }
    Activity "Downloading the speech model ($name, about $size to download)"
    if ((Invoke-Console $EnvPy @('-I', $script:ModelFile, $name, 'get', $rev)) -ne 0) {
        Warn-User 'The download did not finish. The app will try again on first start.'
    } else {
        Say '  The speech model is ready.'
    }
}

# -- the Terms of Use ------------------------------------------------------------

function ConvertTo-ConsoleText([string]$s) {
    # The terms are UTF-8; a console may not be. Dashes and curly quotes
    # become plain ASCII, and the markdown bold marks go.
    $s = $s.Replace([string][char]0x2014, '-').Replace([string][char]0x2013, '-')
    $s = $s.Replace([string][char]0x2018, "'").Replace([string][char]0x2019, "'")
    $s = $s.Replace([string][char]0x201C, '"').Replace([string][char]0x201D, '"')
    return $s.Replace('**', '')
}

function Split-Wrapped([string]$Text, [int]$Width, [string]$First, [string]$Next) {
    # Word-wrap one paragraph to $Width columns; the first line starts with
    # $First, the others with $Next.
    $lines = @(); $cur = $First; $empty = $true
    foreach ($w in ($Text -split '\s+' | Where-Object { $_ })) {
        if ($empty) { $cur = $cur + $w; $empty = $false }
        elseif (($cur.Length + 1 + $w.Length) -gt $Width) { $lines += $cur; $cur = $Next + $w }
        else { $cur = $cur + ' ' + $w }
    }
    if (-not $empty) { $lines += $cur }
    return $lines
}

function Format-TermsLines([string[]]$Source) {
    # TERMS.md as console lines: each paragraph on one line in the file,
    # wrapped here; "- " items get a hanging indent; headings stand apart.
    $out = @()
    foreach ($raw in $Source) {
        $l = (ConvertTo-ConsoleText $raw).TrimEnd()
        if ($l.Trim() -eq '' -or $l.Trim() -eq '---') {
            if ($out.Count -and $out[-1] -ne '') { $out += '' }
        } elseif ($l.StartsWith('## ') -or $l.StartsWith('# ')) {
            if ($out.Count -and $out[-1] -ne '') { $out += '' }
            $out += $l.TrimStart('#', ' ').ToUpper()
            $out += ''
        } elseif ($l.StartsWith('- ')) {
            $out += Split-Wrapped $l.Substring(2) 78 '  - ' '    '
        } else {
            $out += Split-Wrapped $l 78 '' ''
        }
    }
    return $out
}

function Get-TermsFile([string]$Dir) {
    $path = Join-Path $Dir 'TERMS.md'
    if (-not (Test-Path -LiteralPath $path)) {
        throw "TERMS.md is missing next to install.bat ($Dir). Extract the whole ZIP first (right-click the ZIP > Extract All)."
    }
    return $path
}

function Read-TermsFile([string]$Dir) {
    # -> @{ Path; Lines; Version }. Stops the installer if the terms are
    # missing or are not the version this program was made for.
    $path = Get-TermsFile $Dir
    $lines = @(Get-Content -LiteralPath $path -Encoding UTF8)
    $want = Read-SourceFact $Dir 'settings.py' 'TERMS_VERSION\s*=\s*"([^"]+)"' ''
    $m = [regex]::Match(($lines | Select-Object -First 1), '^Version (\S+) ')
    if (-not $want -or -not $m.Success -or $m.Groups[1].Value -ne $want) {
        throw 'TERMS.md is not the version of the terms that belongs to this program. Extract the whole ZIP again.'
    }
    if (@(Get-TermsSummary $lines).Count -eq 0) {
        throw 'TERMS.md has no summary section. Extract the whole ZIP again.'
    }
    return @{ Path = $path; Lines = $lines; Version = $want }
}

function Get-TermsSummary([string[]]$Lines) {
    # The lines between the "## Summary" heading and the next "---".
    $out = @(); $in = $false
    foreach ($l in $Lines) {
        if (-not $in) { if ($l.Trim().ToLower() -eq '## summary') { $in = $true }; continue }
        if ($l.Trim() -eq '---') { break }
        $out += $l
    }
    return $out
}

function Show-TermsPaged([string[]]$Lines) {
    # The whole text in this window, a screen at a time.
    $all = Format-TermsLines $Lines
    $page = 20
    try { $hgt = $Host.UI.RawUI.WindowSize.Height; if ($hgt -gt 10) { $page = $hgt - 3 } } catch { }
    $n = 0
    foreach ($l in $all) {
        Write-Host $l
        $n++
        if (($n % $page) -eq 0 -and $n -lt $all.Count) {
            try { $a = (Read-Host '-- Enter: more, Q: stop reading --').Trim().ToLower() } catch { break }
            if ($a -eq 'q') { break }
        }
    }
    Write-Host ''
    Write-Host '-- end of the terms --'
}

function Confirm-Terms($Terms) {
    # Shows the summary, then asks for the words "I agree". Returns "installer"
    # when accepted, $null when not. The terms are shown first and the person
    # can read all of them here or in Notepad. -Yes installs without questions
    # but does not accept for anyone: it returns "unattended", which writes no
    # record, and the app asks the person who uses it.
    Write-Host ''
    Write-Host "Please read the Terms of Use (version $($Terms.Version)) before you continue:" -ForegroundColor Yellow
    foreach ($l in (Format-TermsLines (Get-TermsSummary $Terms.Lines))) { Write-Host $l }
    Write-Host ''
    Write-Host 'By agreeing you accept the whole terms.'
    Write-Host 'This includes the sections on no warranty, limits of liability, assumption of risk, indemnity and disputes (sections 9 to 14).'
    Write-Host ''
    Write-Host 'That was only a summary. The full terms are in the file TERMS.md:'
    Write-Host "  $($Terms.Path)"
    Write-Host 'The other documents it refers to are LICENSE, THIRD-PARTY-NOTICES.md and SECURITY.md in the same folder.'
    if ($Yes) {
        Write-Host ''
        Write-Host 'Using the -Yes switch installs without questions, but it does not accept the Terms of Use for the person who will use the app. The app will show the terms and ask that person to choose I agree before it dictates.' -ForegroundColor Yellow
        return 'unattended'
    }
    Write-Host ''
    Write-Host 'To read the full terms now, type READ (here, a screen at a time) or NOTEPAD (opens them in Notepad).'
    Write-Host 'To accept the terms and install, type the words:  I agree'
    Write-Host 'Anything else stops the installer, and nothing on this PC is changed.'
    while ($true) {
        try { $a = (Read-Host 'Your answer').Trim() } catch { return $null }
        if ($a -ieq 'I agree') { return 'installer' }
        if ($a -ieq 'read') { Show-TermsPaged $Terms.Lines; continue }
        if ($a -ieq 'notepad') {
            if ($layout.TestMode) { Write-Host "(test mode: Notepad is not opened; the file is $($Terms.Path))" }
            else {
                try { Start-Process -FilePath 'notepad.exe' -ArgumentList ('"' + $Terms.Path + '"') }
                catch { Write-Host 'Notepad could not be opened. Type READ to read the terms here instead.' }
            }
            continue
        }
        return $null
    }
}

# -- main ------------------------------------------------------------------------

function Write-Facts($Layout, [string]$AppName, [string]$Version, [bool]$GpuFound) {
    # -Check: everything the installer window shows before it installs, as one
    # "@@htt" line. Changes nothing (the private work folder, made for the Python
    # probe, is removed by the finally block below).
    $installed = Read-SourceFact $Layout.InstallRoot 'settings.py' 'APP_VERSION\s*=\s*"([^"]+)"' ''
    $termsVersion = Read-SourceFact $SrcDir 'settings.py' 'TERMS_VERSION\s*=\s*"([^"]+)"' ''

    $found = Search-Python
    if ($found.Python) {
        $py = [ordered]@{ ok = $true; version = $found.Python.Version; exe = $found.Python.Exe; via = $found.Python.Via }
    } else {
        $py = [ordered]@{ ok = $false; notes = @($found.Notes) }
    }

    # The environment: does it work, and what do the requirements hashes it kept say.
    $envPy = Join-Path $Layout.EnvDir 'Scripts\python.exe'
    $envWorks = $false
    if (Test-Path -LiteralPath $envPy) {
        $got = Read-PythonInfo $envPy @()
        $envWorks = [bool]($got.Info -and -not (Get-PythonProblem $got.Info))
    }
    $hashFile = Join-Path $Layout.EnvDir 'htt-requirements.sha256'
    $have = if (Test-Path -LiteralPath $hashFile) { (Get-Content -LiteralPath $hashFile -Raw).Trim() } else { '' }
    $hashCuda = Get-RequirementsHash $SrcDir $true
    $hashPlain = Get-RequirementsHash $SrcDir $false
    $runtimeInstalled = [bool]($have -and $have -eq $hashCuda)
    $depsCurrent = [bool]($envWorks -and $have -and ($have -eq $hashCuda -or $have -eq $hashPlain))

    # The model the app will use (with or without the NVIDIA runtime), and whether it is
    # on this PC: only when the environment works to ask it; otherwise null.
    $modelGpu = Get-ModelName $SrcDir $true
    $modelCpu = Get-ModelName $SrcDir $false
    $present = $null
    if ($envWorks) {
        $withRuntime = if ($runtimeInstalled) { $true } elseif ($have -and $have -eq $hashPlain) { $false } else { $GpuFound }
        $which = if ($withRuntime) { $modelGpu } else { $modelCpu }
        $c = Test-ModelOnDisk $envPy $SrcDir $which
        if ($c.Code -eq 0) { $present = $true } elseif ($c.Code -eq 3) { $present = $false }
    }

    $files = @(Get-Manifest $SrcDir)
    $bytes = [long]0
    foreach ($f in $files) {
        $p = Join-Path $SrcDir $f
        if (Test-Path -LiteralPath $p) { $bytes += (Get-Item -LiteralPath $p).Length }
    }

    $drive = $null; $free = $null
    try {
        $root = [IO.Path]::GetPathRoot([IO.Path]::GetFullPath($Layout.InstallRoot))
        $free = (New-Object IO.DriveInfo($root)).AvailableFreeSpace
        $drive = $root.TrimEnd('\')
    } catch { $drive = $null; $free = $null }

    $pythonw = Join-Path $Layout.EnvDir 'Scripts\pythonw.exe'
    Write-Marker ([ordered]@{
        kind = 'facts'; appName = $AppName; version = $Version; termsVersion = $termsVersion
        testMode = [bool]$Layout.TestMode
        layout = [ordered]@{ installRoot = $Layout.InstallRoot; envDir = $Layout.EnvDir; dataDir = $Layout.DataDir
                             startMenuLnk = $Layout.StartMenuLnk; desktopLnk = $Layout.DesktopLnk }
        installedVersion = $(if ($installed) { $installed } else { $null })
        python = $py
        gpu = [ordered]@{ found = $GpuFound; runtimeInstalled = $runtimeInstalled }
        env = [ordered]@{ exists = $envWorks; depsCurrent = $depsCurrent }
        model = [ordered]@{ name = $modelGpu; cpuName = $modelCpu; downloadMB = 1500; cpuDownloadMB = 500; present = $present }
        files = [ordered]@{ count = $files.Count; sizeBytes = $bytes }
        # The libraries (requirements.lock.txt, Python 3.12, without pip itself) were
        # measured once, not guessed: the download is the sum of the sizes of the lock
        # file's win_amd64 / pure-python wheels on PyPI (30 packages, 96 MB); the disk
        # use is the size of site-packages of a finished environment without the
        # nvidia-* folders, pip and setuptools (306 MB). The NVIDIA figures are the
        # ones the question above states (1.3 GB to download, 2 GB on disk; an
        # environment's nvidia-* folders measured 2.0 GB).
        estimates = [ordered]@{ libsDownloadMB = 96; libsDiskMB = 306; nvidiaDownloadMB = 1300; nvidiaDiskMB = 2000 }
        disk = [ordered]@{ drive = $drive; freeBytes = $free }
        steps = @($StepTitles)
        launch = [ordered]@{ exe = $pythonw; args = ('"' + (Join-Path $Layout.InstallRoot 'dictate.py') + '" --open'); workDir = $Layout.InstallRoot }
    })
}

$machine = ($Check -or $Progress)
if ($machine) {
    # The window reads our output as UTF-8 (no byte-order mark).
    try { [Console]::OutputEncoding = New-Object Text.UTF8Encoding($false) } catch { }
}

try {
    # What may not be combined: all refused before anything is looked at or changed.
    if ($Check -and $Progress) { throw '-Check and -Progress cannot be used together.' }
    if ($EnvOnly -and $machine) { throw '-EnvOnly cannot be used with -Check or -Progress.' }
    if (-not [string]::IsNullOrWhiteSpace($AcceptedTerms) -and -not $Progress) {
        throw '-AcceptedTerms is only for the installer window, together with -Progress. To accept the Terms of Use here, run install.bat and type I agree.'
    }
    if ($Yes -and ($Progress -or -not [string]::IsNullOrWhiteSpace($AcceptedTerms))) {
        throw '-Yes cannot be used with -Progress or -AcceptedTerms: -Yes never accepts the Terms of Use for anyone.'
    }
    if ($Progress -and [string]::IsNullOrWhiteSpace($AcceptedTerms)) {
        $msg = 'Not installed. Nothing was changed on this PC. The Terms of Use were not accepted (-Progress needs -AcceptedTerms).'
        Write-Host ''
        Write-Host $msg
        Write-Marker ([ordered]@{ kind = 'error'; text = $msg })
        exit 3
    }

    # A download from the Setup.exe is checked against its hash list first.
    Test-DownloadIntact $SrcDir

    $layout = Get-HttLayout -InstallRoot $InstallRoot -EnvDir $EnvDir -StartMenuDir $StartMenuDir `
        -DesktopDir $DesktopDir -RegKey $RegKey -Strict:(-not $EnvOnly)
    $appName = Read-SourceFact $SrcDir 'settings.py' 'APP_NAME\s*=\s*"([^"]+)"' 'Hold to talk'
    $version = Read-SourceFact $SrcDir 'settings.py' 'APP_VERSION\s*=\s*"([^"]+)"' '0'
    $gpuFound = Test-NvidiaGpu
    $useGpu = $gpuFound

    if ($Check) {
        Write-Facts $layout $appName $version $gpuFound
        exit 0
    }

    if ($EnvOnly) {
        # start.bat: the source folder is the program folder.
        $python = Find-Python
        Say "Using Python $($python.Version) ($($python.Via))"
        $useGpu = Resolve-GpuChoice $SrcDir $layout $gpuFound
        [void](Ensure-Environment $python $layout $SrcDir $useGpu)
        exit 0
    }

    Write-Host ''
    Write-Host "$appName $version - installer" -ForegroundColor Green
    Write-Host 'Everything is installed for your Windows account only. No administrator rights.'
    if ($layout.TestMode) { Write-Host '(test mode: temporary places, the real app is not touched)' -ForegroundColor Yellow }
    Write-Host "  Program files:   $($layout.InstallRoot)"
    Write-Host "  Python copy:     $($layout.EnvDir)"
    Write-Host "  Your data:       $($layout.DataDir)  (kept as it is)"

    # The Terms of Use come first: nothing is changed before they are accepted.
    $terms = Read-TermsFile $SrcDir
    if ($Progress) {
        # The installer window showed the terms and a person accepted them there.
        if ($AcceptedTerms.Trim() -ne $terms.Version) {
            throw "The Terms of Use version given ($AcceptedTerms) is not the version of this program (version $($terms.Version)). Nothing was changed. Extract the whole ZIP again and open the installer again."
        }
        $how = 'installer'
        Write-Host ''
        Write-Host "The Terms of Use (version $($terms.Version)) were accepted in the installer window."
    } else {
        $how = Confirm-Terms $terms
    }
    if (-not $how) {
        Write-Host ''
        Write-Host 'Not installed. Nothing was changed on this PC. Run install.bat again when you want to continue.'
        exit 3   # 3 = stopped at the terms; install.bat says so without calling it an error
    }

    Step '1. Looking for Python'
    $python = Find-Python
    Write-Host "  Using Python $($python.Version) from $($python.Exe)"
    Write-Host "  (found by: $($python.Via))"

    Step '2. Checking the files'
    $files = @(Get-Manifest $SrcDir)
    $missing = @($files | Where-Object { -not (Test-Path -LiteralPath (Join-Path $SrcDir $_)) })
    if ($missing.Count) { throw ('Files missing from this folder (extract the whole ZIP first): ' + ($missing -join ', ')) }
    Say "  $($files.Count) files to install."

    # Python and the files are in order: from here on the PC is changed, so
    # record that the terms were accepted (a small file in your data folder).
    if ($how -eq 'unattended') {
        Say '  No acceptance of the Terms of Use was recorded: the app will ask the person who uses it.'
    } else {
        Write-TermsRecord $layout.DataDir $terms.Version $how
        Say "  Recorded that the Terms of Use (version $($terms.Version)) were accepted ($how) in $($layout.DataDir)"
    }

    Step '3. Making sure the app is not running'
    Stop-RunningApp $layout $layout.InstallRoot

    Step '4. Copying the program files'
    Install-Files $files $SrcDir $layout.InstallRoot

    Step '5. Python environment and libraries'
    $useGpu = Resolve-GpuChoice $layout.InstallRoot $layout $gpuFound
    $envPy = Ensure-Environment $python $layout $layout.InstallRoot $useGpu

    Step '6. Speech model'
    if ($SkipModel -or $NoDeps) { Say '  Skipped.' }
    else { Offer-ModelDownload $envPy $layout.InstallRoot $useGpu }

    Step '7. Shortcuts and the Installed apps entry'
    $pythonw = Join-Path $layout.EnvDir 'Scripts\pythonw.exe'
    $dictate = Join-Path $layout.InstallRoot 'dictate.py'
    $icon = Join-Path $layout.InstallRoot 'app.ico'
    $lnkArgs = '"' + $dictate + '" --open'
    New-Shortcut $layout.StartMenuLnk $pythonw $lnkArgs $layout.InstallRoot $icon "$appName - dictate by holding a key"
    Say "  Start menu: $($layout.StartMenuLnk)"
    $desktopMade = $null
    if ($DesktopShortcut -eq 'no') {
        Say '  Desktop:    no shortcut (as asked).'
    } else {
        New-Shortcut $layout.DesktopLnk $pythonw $lnkArgs $layout.InstallRoot $icon "$appName - dictate by holding a key"
        Say "  Desktop:    $($layout.DesktopLnk)"
        $desktopMade = $layout.DesktopLnk
    }
    Write-UninstallEntry $layout $appName $version
    Say "  Settings > Apps > Installed apps now lists $appName (to remove it later)."

    Write-Host ''
    Write-Host "Done. $appName $version is installed." -ForegroundColor Green
    Write-Host ''
    Write-Host 'Optional: AI cleanup. The app can tidy what you say (fillers, false starts) with a'
    Write-Host 'small AI model that runs on this PC through Ollama. It needs Ollama and a model of'
    Write-Host 'about 4.3 GB:  ollama pull gemma4:e2b-it-qat   The app works fine without it; this'
    Write-Host 'installer does not install Ollama.'
    Write-Host ''
    Write-Host 'If nothing is heard: Settings > Privacy & security > Microphone must allow'
    Write-Host '"Let desktop apps access your microphone".'

    # The installer window starts the app itself, when the person asks for it.
    if (-not $NoLaunch -and -not $layout.TestMode -and -not $Progress) {
        Write-Host ''
        if (Ask-YesNo "Start $appName now?" $true) {
            Start-Process -FilePath $pythonw -ArgumentList $lnkArgs -WorkingDirectory $layout.InstallRoot
            Write-Host 'Started. Its icon appears in the notification area; the window opens in a moment.'
        } else {
            Write-Host "Start it any time from the Start menu: type ""$appName""."
        }
    }
    if ($Progress) {
        Write-Marker ([ordered]@{
            kind = 'done'; installRoot = $layout.InstallRoot; envDir = $layout.EnvDir; dataDir = $layout.DataDir
            startMenuLnk = $layout.StartMenuLnk; desktopLnk = $desktopMade
            termsRecorded = ($how -ne 'unattended')
            launch = [ordered]@{ exe = $pythonw; args = $lnkArgs; workDir = $layout.InstallRoot }
        })
    }
    exit 0
} catch {
    Write-Host ''
    Write-Host 'The installer stopped:' -ForegroundColor Red
    Write-Host $_.Exception.Message -ForegroundColor Red
    if ($machine) { Write-Marker ([ordered]@{ kind = 'error'; text = [string]$_.Exception.Message }) }
    if ($script:NoPython) { exit 4 }      # -Progress: no suitable Python
    exit 1
} finally {
    if ($script:WorkDir) { Remove-Item -LiteralPath $script:WorkDir -Recurse -Force -ErrorAction SilentlyContinue }
}
