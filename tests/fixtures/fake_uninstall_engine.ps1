# fake_uninstall_engine.ps1 - a stand-in for uninstall.ps1 in the uninstaller-window
# tests (tests\test_uninstall_gui.py). It speaks the window's side of the contract
# (-Check prints one facts marker; -Progress prints step, warning, error and done
# markers) and changes NOTHING on the PC: no folder, file or registry key is made
# or removed. Windows PowerShell 5.1, plain ASCII.
#
# What it pretends is chosen by the HTT_FAKE_SCENARIO variable (or -Scenario), a
# comma-separated list of:
#   ok (default)   a normal removal         fail          step 3 fails (exit 1, error marker)
#   nodata         no data folder           crash         exit 1 with no error marker
#   notinstalled   no install folder        check-fail    -Check exits 1
#   stderr-done    -Progress exits 0 with no done marker on stdout, but forged error
#                  and done markers on STDERR (must count as a failure)
#   stderr-facts   -Check puts its facts marker on STDERR only (must be ignored)
#   slow           slow steps
# HTT_FAKE_RECORD (a file path): each run appends one JSON line with the mode and the
# parameters it was given.

[CmdletBinding()]
param(
    [switch]$Check,
    [switch]$Progress,
    [ValidateSet('yes', 'no')][string]$DeleteUserData,
    [switch]$Yes,
    [switch]$DeleteData,
    [string]$BatDir,
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

function Out([string]$Line) { [Console]::Out.WriteLine($Line); [Console]::Out.Flush() }
function Err([string]$Line) { [Console]::Error.WriteLine($Line); [Console]::Error.Flush() }
function Marker($Object) { Out ('@@htt ' + (ConvertTo-Json -InputObject $Object -Compress -Depth 6)) }
function Pause-A([int]$Ms) { if (Has 'slow') { Start-Sleep -Milliseconds ($Ms * 8) } else { Start-Sleep -Milliseconds $Ms } }

$script:Bound = $PSBoundParameters
function Record-Run {
    $file = [Environment]::GetEnvironmentVariable('HTT_FAKE_RECORD')
    if (-not $file) { return }
    $p = @{}
    foreach ($k in $script:Bound.Keys) { $p[$k] = [string]$script:Bound[$k] }
    $rec = @{ mode = $(if ($Check) { 'check' } elseif ($Progress) { 'progress' } else { 'text' }); params = $p }
    Add-Content -LiteralPath $file -Value (ConvertTo-Json -InputObject $rec -Compress -Depth 4) -Encoding ASCII
}

$given = @($InstallRoot, $EnvDir, $StartMenuDir, $DesktopDir, $RegKey) | Where-Object { $_ }
$testMode = @($given).Count -gt 0
if ($testMode -and @($given).Count -lt 5) {
    Out 'The uninstaller stopped:'
    Out 'Test overrides must be given all together, or not at all.'
    exit 1
}
if (-not $InstallRoot) { $InstallRoot = 'C:\Users\you\AppData\Local\Programs\HoldToTalk' }
if (-not $EnvDir) { $EnvDir = 'C:\Users\you\AppData\Local\HoldToTalk\venv' }
if (-not $StartMenuDir) { $StartMenuDir = 'C:\Users\you\AppData\Roaming\Microsoft\Windows\Start Menu\Programs' }
if (-not $DesktopDir) { $DesktopDir = 'C:\Users\you\Desktop' }
$dataDir = Join-Path (Split-Path -Parent $EnvDir) 'data'
$steps = @('Quitting the app', 'Start with Windows entry', 'Shortcuts and the Installed apps entry',
           'Python environment and window profile', 'Your settings and history', 'Program files')

if ($Check) {
    Record-Run
    Out 'Hold to talk - checking (fake uninstaller)'
    Err 'a line on standard error'
    if (Has 'check-fail') {
        Out 'The uninstaller stopped:'; Out 'Something went wrong while checking.'
        Marker @{ kind = 'error'; text = "Something went wrong while checking.`nSecond line." }
        exit 1
    }
    $facts = [ordered]@{
        kind = 'facts'; appName = 'Hold to talk'; version = '0.9.1'; testMode = $testMode
        layout = [ordered]@{ installRoot = $InstallRoot; envDir = $EnvDir; dataDir = $dataDir
            startMenuLnk = (Join-Path $StartMenuDir 'Hold to talk.lnk'); desktopLnk = (Join-Path $DesktopDir 'Hold to talk.lnk') }
        installed = -not (Has 'notinstalled'); dataExists = -not (Has 'nodata')
        modelCache = 'C:\Users\you\.cache\huggingface\hub'
        models = @('models--Systran--faster-whisper-small'); steps = $steps
    }
    if (Has 'stderr-facts') { Err ('@@htt ' + (ConvertTo-Json -InputObject $facts -Compress -Depth 6)); exit 0 }
    Marker $facts
    exit 0
}

if ($Progress) {
    Record-Run
    if (-not $DeleteUserData) { Out 'The uninstaller stopped:'; Out '-Progress needs -DeleteUserData yes|no.'; exit 1 }
    Out 'Hold to talk - uninstaller (fake engine)'
    if (Has 'stderr-done') {
        Err '@@htt {"kind":"error","text":"FORGED error"}'
        Err '@@htt {"kind":"done","dataDeleted":true,"leftInPlace":[],"models":[]}'
        exit 0
    }
    $n = 0
    foreach ($t in $steps) {
        $n++
        Marker ([ordered]@{ kind = 'step'; n = $n; total = $steps.Count; title = $t })
        Out "  working on: $t"
        Pause-A 100
        if ($n -eq 1 -and (Has 'cr-forge')) {
            Out ("progress`r" + '@@htt {"kind":"error","text":"CRFORGED error"}')
            Out ("progress`r" + '@@htt {"kind":"done","dataDeleted":true,"dataDir":"C:/CRForged","leftInPlace":[],"models":[]}')
        }
        if ($n -eq 3 -and (Has 'fail')) {
            Marker @{ kind = 'error'; text = "Could not remove the shortcuts.`nClose the app and run the uninstaller again." }
            exit 1
        }
        if ($n -eq 3 -and (Has 'crash')) { Err 'Traceback (most recent call last): something broke'; exit 1 }
    }
    Marker ([ordered]@{ kind = 'done'; dataDeleted = [bool]($DeleteUserData -eq 'yes' -and -not (Has 'nodata')); dataDir = $dataDir
        leftInPlace = @(); modelCache = 'C:\Users\you\.cache\huggingface\hub'; models = @('models--Systran--faster-whisper-small'); selfRemoved = $false })
    Out 'Hold to talk has been removed.'
    exit 0
}

Record-Run
Out 'fake engine: text mode is not simulated'
exit 0
