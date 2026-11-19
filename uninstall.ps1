# uninstall.ps1 - removes Hold to talk for the current user.
# Started by uninstall.bat (which runs a temporary copy of this file, so the
# install folder can be deleted). Windows PowerShell 5.1. Plain ASCII.
#
# Removes: the running app (asked to quit), the Start with Windows entry, the
# Start menu and Desktop shortcuts, the Installed apps entry, the install
# folder and the Python environment. Your settings and history are kept
# unless you say otherwise. It never touches the speech-model cache, pip's
# cache or Ollama: it prints where they are and how to remove them.
#
#   -Yes          no questions (user data is still kept)
#   -DeleteData   also delete the settings, history and logs
#   -BatDir       folder of the uninstall.bat that started this (set by it)
# Test overrides: see htt-common.ps1 (all five together, or none).
# Exit code: 0 done; 10 done and uninstall.bat should delete itself; 2 cancelled; 1 failed.
#
# For the uninstaller window (uninstall-gui.ps1 runs this script as a hidden child
# process and shows what it prints; the window never removes anything itself):
#   -Check        changes nothing; prints one "facts" line: the folders, the version,
#                 whether there is data, the titles of the steps
#   -Progress -DeleteUserData yes|no
#                 does the removal without ever reading input, the answer to "Delete
#                 your settings, dictation history and logs too?" given here (never
#                 assumed: it is required). It prints its ordinary text and, in
#                 between, marker lines: "@@htt " and one line of JSON with a "kind"
#                 (facts, step, warning, error, done). Exit code 0 done, 1 failed. When
#                 -BatDir is this program folder it also removes uninstall.bat and
#                 the folder (the batch file has finished in this mode: it only
#                 opened the window).
# (-DeleteData stays the switch of the text mode; it cannot be combined with -Progress.)

[CmdletBinding()]
param(
    [switch]$Yes,
    [switch]$DeleteData,
    [switch]$Check,
    [switch]$Progress,
    [ValidateSet('yes', 'no')][string]$DeleteUserData,
    [string]$BatDir,
    [string]$InstallRoot,
    [string]$EnvDir,
    [string]$StartMenuDir,
    [string]$DesktopDir,
    [string]$RegKey
)

$ErrorActionPreference = 'Stop'
$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
. (Join-Path $Here 'htt-common.ps1')

$StepTitles = @('Quitting the app', 'Start with Windows entry', 'Shortcuts and the Installed apps entry',
                'Python environment and window profile', 'Your settings and history', 'Program files')

function Write-Marker($Object) {
    # "@@htt " and one line of JSON (line breaks and quotes are escaped by ConvertTo-Json).
    if (-not ($Progress -or $Check)) { return }
    Write-Host ('@@htt ' + (ConvertTo-Json -InputObject $Object -Compress -Depth 6))
}

function Step([int]$N, [string]$Title) {
    Write-Host ''
    Write-Host "$N. $Title" -ForegroundColor Cyan
    Write-Marker ([ordered]@{ kind = 'step'; n = $N; total = $StepTitles.Count; title = $Title })
}

function Ask-YesNo([string]$Question, [bool]$Default) {
    if ($Yes -or $Progress) { return $Default }       # -Progress never reads input
    $hint = if ($Default) { 'Y/n' } else { 'y/N' }
    while ($true) {
        $a = (Read-Host "$Question [$hint]").Trim().ToLower()
        if ($a -eq '') { return $Default }
        if ($a -in @('y', 'yes')) { return $true }
        if ($a -in @('n', 'no')) { return $false }
    }
}

function Get-ModelCacheDir {
    $c = Get-HttEnv 'HF_HUB_CACHE'
    if ($c) { return $c }
    $h = Get-HttEnv 'HF_HOME'
    if ($h) { return (Join-Path $h 'hub') }
    return (Join-Path $env:USERPROFILE '.cache\huggingface\hub')
}

function Remove-Step([string]$Label, [string]$Path) {
    if (-not (Test-Path -LiteralPath $Path) -and -not (Test-IsLink $Path)) { Write-Host "  ${Label}: not there."; return }
    if (Test-IsLink $Path) {
        # A folder that is itself a link (junction) points somewhere else: not followed, not removed.
        Write-Warning "$Label is a link to another folder ($Path); left alone."
        Write-Marker ([ordered]@{ kind = 'warning'; text = "$Label is a link to another folder ($Path), so it was left alone. Delete the link yourself if you want it gone." })
        return
    }
    if (Remove-PathWithRetry $Path -RefuseLink) { Write-Host "  ${Label}: removed." }
    else {
        Write-Warning "$Label could not be removed ($Path). Quit the app and delete it by hand."
        Write-Marker ([ordered]@{ kind = 'warning'; text = "$Label could not be removed ($Path). Quit the app and delete it by hand." })
    }
}

function Get-ModelFolders([string]$Cache) {
    $names = @()
    if (Test-Path -LiteralPath $Cache) {
        foreach ($m in @(Get-ChildItem -LiteralPath $Cache -Directory -Filter 'models--*whisper*' -ErrorAction SilentlyContinue)) { $names += $m.Name }
    }
    return @($names)
}

# -- the switches of the window --------------------------------------------------------
$refusal = $null
if ($Check -and $Progress) { $refusal = '-Check cannot be combined with -Progress.' }
elseif ($Progress -and $DeleteData) { $refusal = '-DeleteData is the switch of the text mode; with -Progress give -DeleteUserData yes|no.' }
elseif ($Progress -and -not $DeleteUserData) { $refusal = '-Progress needs -DeleteUserData yes|no (the answer is never assumed).' }
elseif ($DeleteUserData -and -not $Progress) { $refusal = '-DeleteUserData is only for -Progress.' }
if ($refusal) {
    Write-Host 'The uninstaller stopped:' -ForegroundColor Red
    Write-Host $refusal -ForegroundColor Red
    Write-Host ('@@htt ' + (ConvertTo-Json -InputObject ([ordered]@{ kind = 'error'; text = $refusal }) -Compress))
    exit 1
}

try {
    $layout = Get-HttLayout -InstallRoot $InstallRoot -EnvDir $EnvDir -StartMenuDir $StartMenuDir `
        -DesktopDir $DesktopDir -RegKey $RegKey -Strict
    $appName = 'Hold to talk'
    $inst = $layout.InstallRoot

    if ($Check) {
        # Changes nothing: what the window shows before anything is removed.
        $cache = Get-ModelCacheDir
        $ver = Read-SourceFact $inst 'settings.py' 'APP_VERSION\s*=\s*"([^"]+)"' ''
        Write-Marker ([ordered]@{
            kind = 'facts'; appName = $appName; version = $ver; testMode = [bool]$layout.TestMode
            layout = [ordered]@{ installRoot = $layout.InstallRoot; envDir = $layout.EnvDir; dataDir = $layout.DataDir
                                 startMenuLnk = $layout.StartMenuLnk; desktopLnk = $layout.DesktopLnk }
            installed = [bool](Test-Path -LiteralPath (Join-Path $inst 'dictate.py'))
            dataExists = [bool](Test-Path -LiteralPath $layout.DataDir)
            modelCache = $cache; models = @(Get-ModelFolders $cache)
            steps = $StepTitles })
        exit 0
    }

    Write-Host ''
    Write-Host "$appName - uninstaller" -ForegroundColor Green
    if ($layout.TestMode) { Write-Host '(test mode: temporary places, the real app is not touched)' -ForegroundColor Yellow }
    Write-Host 'This removes the app, its shortcuts and its private Python copy.'
    Write-Host "Your settings, dictation history and logs are kept:  $($layout.DataDir)"
    if (-not (Ask-YesNo "Remove $appName now?" $true)) { Write-Host 'Nothing was changed.'; exit 2 }

    Step 1 'Quitting the app'
    Stop-RunningApp $layout $inst

    Step 2 'Start with Windows entry'
    $py = Join-Path $layout.EnvDir 'Scripts\python.exe'
    $dictate = Join-Path $inst 'dictate.py'
    $runKeys = Get-HttRunKeys
    # A test run touches the Start with Windows entry only in a throw-away key
    # (HTT_RUN_KEY); a real run touches the real one.
    $touchRun = (-not $layout.TestMode) -or $runKeys.Override
    $done = $false
    if ((Test-Path -LiteralPath $py) -and (Test-Path -LiteralPath $dictate)) {
        $uargs = @('-B', $dictate, '--uninstall')
        if ($layout.TestMode -and -not $runKeys.Override) {
            $uargs += @('--config', (Join-Path $layout.AppHome 'quit-probe\config.json'))
        }
        $r = Invoke-Capture $py $uargs 60
        if ($r.Code -eq 0) {
            $first = ($r.Out -split "`r?`n" | Where-Object { $_.Trim() } | Select-Object -First 1)
            Write-Host "  $first"
            $done = $true
        }
    }
    if (-not $touchRun) {
        Write-Host '  Test mode: the real Start with Windows entry is not touched.'
    } else {
        # Whatever the app said, make sure the entry is gone (it is a program
        # that would otherwise start at every sign-in, from a folder that no
        # longer exists), and say so only if it really was.
        $still = $false
        foreach ($k in @($runKeys.Run, $runKeys.Approved)) {
            $v = Get-ItemProperty -LiteralPath "HKCU:\$k" -Name 'HoldToTalk' -ErrorAction SilentlyContinue
            if ($v) {
                Remove-ItemProperty -LiteralPath "HKCU:\$k" -Name 'HoldToTalk' -ErrorAction SilentlyContinue
                $still = $true
            }
        }
        if ($still) { Write-Host '  Start with Windows: entry removed (directly).' }
        elseif (-not $done) { Write-Host '  Start with Windows: there was no entry.' }
    }

    Step 3 'Shortcuts and the Installed apps entry'
    Remove-Step 'Start menu shortcut' $layout.StartMenuLnk
    Remove-Step 'Desktop shortcut' $layout.DesktopLnk
    $key = "HKCU:\$($layout.RegKey)"
    if ($layout.RegKey -like 'Software\*' -and (Test-Path -LiteralPath $key)) {
        Remove-Item -LiteralPath $key -Recurse -Force
        Write-Host '  Installed apps entry: removed.'
    } else { Write-Host '  Installed apps entry: not there.' }

    Step 4 'Python environment and window profile'
    Remove-Step 'Python environment' $layout.EnvDir
    foreach ($d in @(Get-ChildItem -LiteralPath $layout.AppHome -Directory -ErrorAction SilentlyContinue |
                     Where-Object { $_.Name -eq 'window' -or $_.Name -like 'window-*' -or $_.Name -eq 'quit-probe' })) {
        Remove-Step "Leftover folder ($($d.Name))" $d.FullName
    }

    Step 5 'Your settings and history'
    $deleteData = [bool]$DeleteData
    $leftNames = @()
    $dataWasThere = [bool](Test-Path -LiteralPath $layout.DataDir)
    if ($Progress) { $deleteData = ($DeleteUserData -eq 'yes') }
    elseif (-not $deleteData -and (Test-Path -LiteralPath $layout.DataDir)) {
        Write-Host "  Kept so far: $($layout.DataDir)"
        $deleteData = Ask-YesNo '  Delete your settings, dictation history and logs too? This cannot be undone.' $false
    }
    if ($deleteData -and (Test-Path -LiteralPath $layout.DataDir)) {
        # By name only: HOLDTOTALK_HOME can point at any folder, so it is
        # never deleted wholesale (see Remove-AppDataFiles).
        $left = @(Remove-AppDataFiles $layout.DataDir)
        $leftNames = @($left)
        if ($left.Count) {
            Write-Host "  Settings, history and logs: removed. Left in place (not made by the app): $($left -join ', ')"
            Write-Host "  in $($layout.DataDir)"
        } else { Write-Host '  Settings, history and logs: removed.' }
    }
    elseif ($deleteData) { Write-Host '  Settings, history and logs: not there.' }
    elseif (Test-Path -LiteralPath $layout.DataDir) {
        Write-Host "  Kept: $($layout.DataDir)"
        Write-Host '  (Delete that folder yourself if you never want them back.)'
    }
    if ((Test-Path -LiteralPath $layout.AppHome) -and -not (Test-IsLink $layout.AppHome) -and -not @(Get-ChildItem -LiteralPath $layout.AppHome -Force -ErrorAction SilentlyContinue)) {
        Remove-Item -LiteralPath $layout.AppHome -Force -ErrorAction SilentlyContinue
    }

    Step 6 'Program files'
    if (Test-IsLink $inst) {
        Write-Warning "$inst is a link to another folder; left alone."
        Write-Marker ([ordered]@{ kind = 'warning'; text = "The program folder ($inst) is a link to another folder, so it was left alone." })
    } elseif (Test-Path -LiteralPath (Join-Path $inst 'dictate.py')) {
        # uninstall.bat itself is the one file that cannot be removed while it runs.
        foreach ($item in @(Get-ChildItem -LiteralPath $inst -Force)) {
            if ($item.Name -ieq 'uninstall.bat') { continue }
            if (-not (Remove-PathWithRetry $item.FullName)) { Write-Warning "Could not remove $($item.FullName)" }
        }
        Write-Host "  Program folder: emptied."
    } elseif (Test-Path -LiteralPath $inst) {
        Write-Warning "$inst does not look like an install folder (no dictate.py); left alone."
    } else { Write-Host '  Program folder: not there.' }

    Write-Host ''
    Write-Host 'Left on this PC on purpose (not part of the app; delete them if you like):' -ForegroundColor Cyan
    $cache = Get-ModelCacheDir
    Write-Host "  Speech models:  $cache"
    Write-Host '                  folders named models--...whisper...  (a few hundred MB to 1.5 GB each)'
    foreach ($m in @(Get-ModelFolders $cache)) { Write-Host "                    $m" }
    Write-Host '  pip download cache:  %LOCALAPPDATA%\pip\Cache   (or run: python -m pip cache purge)'
    Write-Host '  Ollama and its AI models, if you installed them: Settings > Apps > Ollama;'
    Write-Host '                  models:  ollama list   then   ollama rm <name>   (gemma4:e2b-it-qat)'
    Write-Host ''
    Write-Host "$appName has been removed." -ForegroundColor Green

    $self = $false
    if ($BatDir) {
        $self = [IO.Path]::GetFullPath($BatDir).TrimEnd('\') -ieq [IO.Path]::GetFullPath($inst).TrimEnd('\')
    }
    if ($Progress) {
        # The window started this and uninstall.bat has already finished (it only
        # opened the window), so the batch file and the folder can go now.
        $selfRemoved = $false
        if ($self -and -not (Test-Path -LiteralPath (Join-Path $inst 'dictate.py'))) {
            [void](Remove-PathWithRetry (Join-Path $inst 'uninstall.bat'))
            if (-not @(Get-ChildItem -LiteralPath $inst -Force -ErrorAction SilentlyContinue)) {
                Remove-Item -LiteralPath $inst -Force -ErrorAction SilentlyContinue
            }
            $selfRemoved = -not (Test-Path -LiteralPath $inst)
        }
        Write-Marker ([ordered]@{
            kind = 'done'; dataDeleted = [bool]($deleteData -and $dataWasThere); dataDir = $layout.DataDir
            leftInPlace = @($leftNames); modelCache = $cache; models = @(Get-ModelFolders $cache); selfRemoved = $selfRemoved })
        exit 0
    }
    if ($self) { exit 10 } else { exit 0 }
} catch {
    Write-Host ''
    Write-Host 'The uninstaller stopped:' -ForegroundColor Red
    Write-Host $_.Exception.Message -ForegroundColor Red
    Write-Marker ([ordered]@{ kind = 'error'; text = [string]$_.Exception.Message })
    exit 1
}
