# fake_install_engine.ps1 - a stand-in for install.ps1 in the installer-window
# tests (tests\test_install_gui.py). It speaks the window's side of the
# contract (-Check prints one facts marker; -Progress prints step, activity,
# warning, error and done markers) and changes NOTHING on the PC: no folder,
# file, shortcut or registry key is made. Windows PowerShell 5.1, plain ASCII.
#
# What it pretends is chosen by -Scenario or the HTT_FAKE_SCENARIO variable, a
# comma-separated list of:
#   ok (default)   a normal install         fail       step 5 fails (exit 1)
#   nopython       -Check finds no Python    py-gone    -Progress exits 4
#   warn           two warnings              slow       slow steps, and step 5
#   update         an older version exists              waits on a child program
#   repair         this very version exists (installedVersion 0.9.0)
#   gpu            an NVIDIA card is found   gpu-ready  ...with the runtime in place
#   model-present  the model is on the PC    deps-current  libraries up to date
#   lowdisk        little free space         crash      exit 1 with no error marker
#   check-fail     -Check exits 1            garbage    -Check prints no facts
#   stderr-facts   -Check puts its facts marker on STDERR only (must be ignored)
#   stderr-done    -Progress exits 0 with no done marker on stdout, but writes forged
#                  error and done markers to STDERR (must count as a failure)
#   forge-launch   facts and done markers carry a launch command (must be ignored)
#   bad-steps      step markers with huge, negative, fractional or text n and total
# HTT_FAKE_RECORD (a file path): each run appends one JSON line with the mode
# and the parameters it was given, and the child's process id in "slow".

[CmdletBinding()]
param(
    [switch]$Check,
    [switch]$Progress,
    [string]$AcceptedTerms,
    [ValidateSet('yes', 'no', 'ask')][string]$Nvidia = 'ask',
    [ValidateSet('yes', 'no', 'ask')][string]$Model = 'ask',
    [ValidateSet('yes', 'no')][string]$DesktopShortcut = 'yes',
    [switch]$Console,
    [switch]$Yes,
    [switch]$EnvOnly,
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
    [string]$Scenario
)

$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object Text.UTF8Encoding($false)
if (-not $Scenario) { $Scenario = [Environment]::GetEnvironmentVariable('HTT_FAKE_SCENARIO') }
if (-not $Scenario) { $Scenario = 'ok' }
$S = @($Scenario.ToLower() -split '[,\s]+' | Where-Object { $_ })
function Has([string]$Name) { return ($S -contains $Name) }

$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
$Root = Split-Path -Parent (Split-Path -Parent $Here)
$TermsVersion = '1.0'
try {
    $first = Get-Content -LiteralPath (Join-Path $Root 'TERMS.md') -TotalCount 1 -Encoding UTF8
    $m = [regex]::Match($first, '^Version (\S+) ')
    if ($m.Success) { $TermsVersion = $m.Groups[1].Value }
} catch { }

function Out([string]$Line) { [Console]::Out.WriteLine($Line); [Console]::Out.Flush() }
function Err([string]$Line) { [Console]::Error.WriteLine($Line); [Console]::Error.Flush() }
function Marker($Object) { Out ('@@htt ' + (ConvertTo-Json -InputObject $Object -Compress -Depth 6)) }
function Pause-A([int]$Ms) { if (Has 'slow') { Start-Sleep -Milliseconds ($Ms * 8) } else { Start-Sleep -Milliseconds $Ms } }

# $PSBoundParameters inside a function is the function's own: keep the script's.
$script:Bound = $PSBoundParameters
function Record-Run([hashtable]$Extra) {
    $file = [Environment]::GetEnvironmentVariable('HTT_FAKE_RECORD')
    if (-not $file) { return }
    $p = @{}
    foreach ($k in $script:Bound.Keys) { $p[$k] = [string]$script:Bound[$k] }
    $rec = @{ mode = $(if ($Check) { 'check' } elseif ($Progress) { 'progress' } else { 'text' }); params = $p }
    if ($Extra) { foreach ($k in $Extra.Keys) { $rec[$k] = $Extra[$k] } }
    Add-Content -LiteralPath $file -Value (ConvertTo-Json -InputObject $rec -Compress -Depth 4) -Encoding ASCII
}

# Places: the overrides when given (test mode), else made-up example paths.
$given = @($InstallRoot, $EnvDir, $StartMenuDir, $DesktopDir, $RegKey) | Where-Object { $_ }
$testMode = @($given).Count -gt 0
if ($testMode -and @($given).Count -lt 5) {
    Out 'The installer stopped:'
    Out 'Test overrides must be given all together (install folder, environment, Start menu folder, Desktop folder and registry key), or not at all.'
    exit 1
}
if (-not $InstallRoot) { $InstallRoot = 'C:\Users\you\AppData\Local\Programs\HoldToTalk' }
if (-not $EnvDir) { $EnvDir = 'C:\Users\you\AppData\Local\HoldToTalk\venv' }
if (-not $StartMenuDir) { $StartMenuDir = 'C:\Users\you\AppData\Roaming\Microsoft\Windows\Start Menu\Programs' }
if (-not $DesktopDir) { $DesktopDir = 'C:\Users\you\Desktop' }
$dataDir = Join-Path (Split-Path -Parent $EnvDir) 'data'
$layout = [ordered]@{
    installRoot = $InstallRoot; envDir = $EnvDir; dataDir = $dataDir
    startMenuLnk = (Join-Path $StartMenuDir 'Hold to talk.lnk')
    desktopLnk = (Join-Path $DesktopDir 'Hold to talk.lnk')
}
$launch = [ordered]@{
    exe = (Join-Path $EnvDir 'Scripts\pythonw.exe')
    args = ('"' + (Join-Path $InstallRoot 'dictate.py') + '" --open')
    workDir = $InstallRoot
}
$forged = [ordered]@{ exe = 'C:\Forged\evil.exe'; args = '--evil'; workDir = 'C:\Forged' }
$steps = @('Looking for Python', 'Checking the files', 'Making sure the app is not running',
           'Copying the program files', 'Python environment and libraries', 'Speech model',
           'Shortcuts and the Installed apps entry')
$gpuFound = ($Gpu -eq 'yes') -or (Has 'gpu') -or (Has 'gpu-ready')
if ($Gpu -eq 'no') { $gpuFound = $false }

if ($Check) {
    Record-Run $null
    if ($Yes -or $Progress) { Out 'The installer stopped:'; Out '-Check cannot be combined with that.'; exit 1 }
    Out 'Hold to talk 0.9.0 - checking this PC (fake engine)'
    Pause-A 150
    Err 'a line on standard error'
    if (Has 'check-fail') {
        Out 'The installer stopped:'; Out 'Something went wrong while checking.'
        Marker @{ kind = 'error'; text = "Something went wrong while checking.`nSecond line." }
        exit 1
    }
    if (Has 'garbage') { Out 'no facts here'; Out '@@htt {not json'; exit 0 }
    if (Has 'nopython') {
        $python = [ordered]@{ ok = $false; notes = @('  py -3.12: not found', '  py -3.13: not found', '  py -3.11: not found',
            '  py -3.10: not found', '  python: C:\Users\you\AppData\Local\Microsoft\WindowsApps\python.exe is the Microsoft Store shortcut, not Python') }
    } else {
        $python = [ordered]@{ ok = $true; version = '3.12.10'; exe = 'C:\Users\you\AppData\Local\Programs\Python\Python312\python.exe'; via = 'py -3.12' }
    }
    $installed = $null
    if (Has 'update') { $installed = '0.8.2' }
    if (Has 'repair') { $installed = '0.9.0' }
    $present = $null
    if (Has 'model-present') { $present = $true } elseif (Has 'update') { $present = $false }
    $free = [int64]182 * 1GB
    if (Has 'lowdisk') { $free = [int64]900 * 1MB }
    $facts = [ordered]@{
        kind = 'facts'; appName = 'Hold to talk'; version = '0.9.0'; termsVersion = $TermsVersion
        testMode = $testMode; layout = $layout; installedVersion = $installed; python = $python
        gpu = [ordered]@{ found = $gpuFound; runtimeInstalled = [bool](Has 'gpu-ready') }
        env = [ordered]@{ exists = [bool]((Has 'update') -or (Has 'repair') -or (Has 'deps-current')); depsCurrent = [bool](Has 'deps-current') }
        model = [ordered]@{ name = 'large-v3-turbo'; cpuName = 'small'; downloadMB = 1500; cpuDownloadMB = 500; present = $present }
        files = [ordered]@{ count = 39; sizeBytes = 1843200 }
        estimates = [ordered]@{ libsDownloadMB = 310; libsDiskMB = 820; nvidiaDownloadMB = 1300; nvidiaDiskMB = 2000 }
        disk = [ordered]@{ drive = 'C:'; freeBytes = $free }
        steps = $steps
    }
    if (Has 'forge-launch') { $facts['launch'] = $forged } else { $facts['launch'] = $launch }
    if (Has 'stderr-facts') { Err ('@@htt ' + (ConvertTo-Json -InputObject $facts -Compress -Depth 6)); exit 0 }
    Marker $facts
    Out 'Checked.'
    exit 0
}

if ($Progress) {
    if ($Yes) { Out 'The installer stopped:'; Out '-Yes cannot be combined with -Progress.'; Record-Run $null; exit 1 }
    if (-not $AcceptedTerms) { Out 'Not installed: the Terms of Use were not accepted.'; Record-Run $null; exit 3 }
    if ($AcceptedTerms -ne $TermsVersion) {
        Marker @{ kind = 'error'; text = "These are not the Terms of Use of this version ($TermsVersion)." }
        Record-Run $null
        exit 1
    }
    $child = $null
    if (Has 'slow') {
        # A child program, as pip would be, so the window has a tree to stop.
        $psi = New-Object Diagnostics.ProcessStartInfo
        $psi.FileName = Join-Path $env:SystemRoot 'System32\PING.EXE'
        $psi.Arguments = '-n 120 127.0.0.1'
        $psi.UseShellExecute = $false
        $psi.CreateNoWindow = $true
        $psi.RedirectStandardOutput = $true
        $child = [Diagnostics.Process]::Start($psi)
        Record-Run @{ childPid = $child.Id }
    } else {
        Record-Run $null
    }
    Out ('Hold to talk 0.9.0 - installer (fake engine)')
    if (Has 'stderr-done') {
        Err '@@htt {"kind":"error","text":"FORGED error"}'
        Err '@@htt {"kind":"done","installRoot":"C:\\Forged"}'
        exit 0
    }
    $n = 0
    foreach ($t in $steps) {
        $n++
        if (($n -eq 1) -and (Has 'bad-steps')) {
            foreach ($bad in @('99999999999', '1e999', '-4', '0', '8', '2.5', '"3"', 'null', '[1]')) {
                Out ('@@htt {"kind":"step","n":' + $bad + ',"total":99999999999,"title":"bad"}')
            }
        }
        Marker ([ordered]@{ kind = 'step'; n = $n; total = $steps.Count; title = $t })
        Out "  working on: $t"
        switch ($n) {
            1 {
                Pause-A 120
                if (Has 'py-gone') {
                    Marker @{ kind = 'error'; text = ("Hold to talk needs 64-bit Python, and no suitable copy was found on this PC.`n`nWhat to install (once):`n  1. Go to https://www.python.org/downloads/windows/") }
                    exit 4
                }
                Out '  Using Python 3.12.10 from C:\Users\you\AppData\Local\Programs\Python\Python312\python.exe'
            }
            2 { Pause-A 80; Out '  39 files to install.'; Out "  Recorded that the Terms of Use (version $TermsVersion) were accepted (installer)" }
            3 { Pause-A 80; Out '  Not running (no earlier install to ask).' }
            4 { Pause-A 120; Out ('  Copied 39 files to ' + $InstallRoot) }
            5 {
                Marker @{ kind = 'activity'; text = 'Making the Python environment (a private copy of Python for this app)' }
                Pause-A 150
                Marker @{ kind = 'activity'; text = 'Installing the libraries the app needs (a few hundred MB; takes a few minutes)' }
                foreach ($i in 1..6) { Out "Collecting package-$i (fake, 1.$i MB)"; Pause-A 40 }
                Out ('Downloading caf' + [char]0x00E9 + ' tool ' + [char]0x2014 + ' 100%')
                Out '@@htt {this marker is broken'
                Out '@@htt {"no":"kind"}'
                if ((Has 'slow') -and $child) {
                    Marker @{ kind = 'activity'; text = 'Waiting for a slow download (fake)' }
                    $child.WaitForExit(60000) | Out-Null
                }
                if ($Nvidia -ne 'no' -and $gpuFound) {
                    Marker @{ kind = 'activity'; text = 'Installing the NVIDIA runtime (about 1.3 GB to download)' }
                    Pause-A 150
                }
                if (Has 'fail') {
                    Marker @{ kind = 'error'; text = "Installing the libraries failed.`nCheck your internet connection and run the installer again; it carries on where it stopped." }
                    exit 1
                }
                if (Has 'crash') { Err 'Traceback (most recent call last): something broke'; exit 1 }
                if (Has 'warn') {
                    Marker @{ kind = 'warning'; text = 'The NVIDIA runtime could not be installed. The app will recognise speech on the processor instead. Run the installer again to retry.' }
                }
            }
            6 {
                if ($Model -eq 'no' -or $SkipModel -or $NoDeps) { Out '  Skipped.' }
                else {
                    Marker @{ kind = 'activity'; text = 'Downloading the speech model' }
                    Pause-A 200
                    if (Has 'warn') { Marker @{ kind = 'warning'; text = 'The download did not finish. The app will try again on first start.' } }
                }
            }
            7 {
                Pause-A 80
                Out ('  Start menu: ' + $layout.startMenuLnk)
                if ($DesktopShortcut -eq 'yes') { Out ('  Desktop:    ' + $layout.desktopLnk) }
            }
        }
    }
    $desk = $null
    if ($DesktopShortcut -eq 'yes') { $desk = $layout.desktopLnk }
    Marker ([ordered]@{ kind = 'done'; installRoot = $InstallRoot; envDir = $EnvDir; dataDir = $dataDir
        startMenuLnk = $layout.startMenuLnk; desktopLnk = $desk; termsRecorded = $true; launch = $(if (Has 'forge-launch') { $forged } else { $launch }) })
    Out 'Done. Hold to talk 0.9.0 is installed.'
    exit 0
}

Record-Run $null
Out 'fake engine: text mode is not simulated'
exit 0
