# uninstall-gui.ps1 - the uninstaller window of Hold to talk (uninstall.bat with no
# arguments opens it). Windows PowerShell 5.1 + WPF; keep this file plain ASCII.
#
# The window never removes anything itself. It runs uninstall.ps1 (the "engine")
# as a hidden child process: first "-Check" (changes nothing, prints one facts
# line), then, after the person has chosen Remove, "-Progress -DeleteUserData
# yes|no" and shows the marker lines it prints. The contract between the two is in
# the header of uninstall.ps1. It looks like the setup window (install-gui.ps1:
# the same palette, fonts and button styles; the resources are copied, not shared).
#
#   uninstall.bat                         opens this window (through -Launch)
#   uninstall-gui.ps1 -SelfTest           checks the window's logic, shows nothing
#   uninstall-gui.ps1 -Shots <folder>     saves pictures of every page (canned facts)
#
# Test switches: -Engine <path> (another engine, e.g. the fake one in tests\fixtures),
# -AutoRun remove|remove-data|remove-keep|cancel (drives the window without a person),
# -Theme light|dark. The test overrides -InstallRoot -EnvDir -StartMenuDir
# -DesktopDir -RegKey are passed on to the engine unchanged.
# -AutoRun refuses unless all five overrides are given, the four folders are under
# the temp folder and the key is under Software\HoldToTalkTest\. -Engine is honoured
# only under the same conditions (or with -SelfTest / -Shots).
#
# What the window trusts: markers (the "@@htt " lines) only when they come from the
# engine's standard output; a line on standard error is log text. Paths shown come
# from the facts, markers only supply plain text. Deleting the settings, history and
# logs needs two steps by a person: the box (it starts unticked) and, when it is
# ticked, a confirmation ("Delete your settings and history for good?"); otherwise
# the engine gets -DeleteUserData no. Only a test switch may tick the box by
# program (Set-DataBoxForTest, which refuses outside the sandbox).
#
# When uninstall.bat ran this from its temporary copy (a folder htt-uninstall-<n> in
# the temp folder), the window deletes that copy when it ends.
#
# Exit codes: 0 removed, 1 failed, 3 closed before removing (nothing was changed).

[CmdletBinding()]
param(
    [string]$Engine,
    [switch]$SelfTest,
    [string]$Shots,
    [ValidateSet('', 'remove', 'remove-data', 'remove-keep', 'cancel')][string]$AutoRun = '',
    [ValidateSet('', 'light', 'dark')][string]$Theme = '',
    [switch]$Launch,
    [string]$ReadySignal,
    [string]$ReasonFile,
    [string]$BatDir,
    [string]$InstallRoot,
    [string]$EnvDir,
    [string]$StartMenuDir,
    [string]$DesktopDir,
    [string]$RegKey
)

$ErrorActionPreference = 'Stop'
$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
$ScriptPath = $MyInvocation.MyCommand.Path
$PsExe = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
if (-not (Test-Path -LiteralPath $PsExe)) { $PsExe = 'powershell.exe' }
$EngineGiven = [bool]$Engine
if (-not $Engine) { $Engine = Join-Path $Here 'uninstall.ps1' }
$Engine = [IO.Path]::GetFullPath($Engine)
$Inv = [Globalization.CultureInfo]::InvariantCulture

function Quote-Arg([string]$a) {
    # One argument for a Windows command line (the rules of CommandLineToArgvW).
    if ($a -eq '') { return '""' }
    if ($a -notmatch '[\s"]') { return $a }
    $a = $a -replace '(\\*)"', '$1$1\"'
    $a = $a -replace '(\\+)$', '$1$1'
    return '"' + $a + '"'
}

# -- the launcher (uninstall.bat runs this in its console) -------------------------------
# Starts the window in a process that has no console window, waits until the window
# has loaded (or failed to), and returns: 0 = the window is up (the console closes
# and the window cleans up the temporary copy), 1 = it could not be shown
# (uninstall.bat then runs the text uninstaller in the console, after the one line
# printed here).

function Invoke-Launcher {
    Write-Host 'Opening the uninstaller window...'
    $id = [Guid]::NewGuid().ToString('N')
    $signal = 'Local\HoldToTalkUninstall-' + $id
    $reason = Join-Path ([IO.Path]::GetTempPath()) ('htt-uninstall-reason-' + $id + '.txt')
    $ready = New-Object Threading.EventWaitHandle($false, [Threading.EventResetMode]::ManualReset, $signal)
    try {
        $psi = New-Object Diagnostics.ProcessStartInfo
        $psi.FileName = $PsExe
        $list = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-STA', '-File', $ScriptPath, '-ReadySignal', $signal, '-ReasonFile', $reason)
        if ($BatDir) { $list += @('-BatDir', $BatDir) }
        $psi.Arguments = ($list | ForEach-Object { Quote-Arg $_ }) -join ' '
        $psi.UseShellExecute = $false
        $psi.CreateNoWindow = $true
        # not this folder: the window deletes it when it ends
        $psi.WorkingDirectory = [IO.Path]::GetTempPath()
        try { $p = [Diagnostics.Process]::Start($psi) }
        catch { Write-Host "The uninstaller window could not be opened ($($_.Exception.Message)). Using the text uninstaller instead."; return 1 }
        $deadline = (Get-Date).AddSeconds(90)
        while ((Get-Date) -lt $deadline) {
            if ($ready.WaitOne(150)) { return 0 }
            if ($p.HasExited) {
                if ($ready.WaitOne(0)) { return 0 }
                $why = $null
                try { if (Test-Path -LiteralPath $reason) { $why = (Get-Content -LiteralPath $reason -Raw).Trim() } } catch { }
                if (-not $why) { $why = "it closed unexpectedly, code $($p.ExitCode)" }
                Write-Host "The uninstaller window could not be opened ($why). Using the text uninstaller instead."
                return 1
            }
        }
        return 0     # still loading after 90 s: leave it be
    } finally {
        try { if (Test-Path -LiteralPath $reason) { Remove-Item -LiteralPath $reason -Force -ErrorAction Stop } } catch { }
        try { $ready.Dispose() } catch { }
    }
}

if ($Launch) { exit (Invoke-Launcher) }

# -- facts that do not need the window ----------------------------------------------------

$OverrideNames = 'InstallRoot', 'EnvDir', 'StartMenuDir', 'DesktopDir', 'RegKey'
$GivenOverrides = @($OverrideNames | Where-Object { (Get-Variable -Name $_ -ValueOnly) })
$OverridesComplete = ($GivenOverrides.Count -eq 5) -and
    ($RegKey -notlike 'Software\Microsoft\Windows\CurrentVersion\Uninstall\HoldToTalk*')

function Get-LongFullPath([string]$Path) {
    # The full path with every 8.3 short name written out, even when the end
    # of it does not exist yet (the nearest folder that exists is expanded).
    $full = [IO.Path]::GetFullPath($Path)
    $tail = New-Object Collections.Generic.List[string]
    $cur = $full.TrimEnd('\')
    while ($cur -and -not (Test-Path -LiteralPath $cur)) {
        $parent = Split-Path -Parent $cur
        if (-not $parent -or $parent -eq $cur) { break }
        $tail.Insert(0, (Split-Path -Leaf $cur))
        $cur = $parent
    }
    $long = $cur
    Add-Native
    if ('HttUninstallNative' -as [type]) {
        try { $r = [HttUninstallNative]::LongPath($cur); if ($r) { $long = $r } } catch { }
    }
    foreach ($t in $tail) { $long = $long.TrimEnd('\') + '\' + $t }
    return $long
}

function Get-SandboxProblem([string[]]$Folders, [string]$Key) {
    # $null when the four folders are strictly under the temp folder and the
    # registry key is under Software\HoldToTalkTest\; else why not.
    $prefix = 'Software\HoldToTalkTest\'
    if (-not $Key -or $Key.Length -le $prefix.Length -or -not $Key.StartsWith($prefix, [StringComparison]::OrdinalIgnoreCase)) {
        return 'the registry key is not under Software\HoldToTalkTest\'
    }
    try { $temp = (Get-LongFullPath ([IO.Path]::GetTempPath())).TrimEnd('\') + '\' }
    catch { return 'the temp folder could not be resolved' }
    foreach ($f in $Folders) {
        if (-not $f) { return 'a test folder is empty' }
        try { $p = (Get-LongFullPath $f).TrimEnd('\') + '\' } catch { return 'a test folder could not be resolved' }
        if ($p.Length -le $temp.Length -or -not $p.StartsWith($temp, [StringComparison]::OrdinalIgnoreCase)) {
            return 'a test folder is not under the temp folder'
        }
    }
    # The data folder (HOLDTOTALK_HOME) and the other places htt-common.ps1's
    # Get-HttLayout reads from the environment must not lead out of the temp
    # folder either: the data folder is where the uninstaller deletes the person's
    # files, and the install window writes the record of the Terms of Use there.
    foreach ($n in @('HOLDTOTALK_HOME', 'HTT_INSTALL_ROOT', 'HTT_ENV_DIR', 'HTT_START_MENU_DIR', 'HTT_DESKTOP_DIR')) {
        $v = [Environment]::GetEnvironmentVariable($n)
        if ([string]::IsNullOrWhiteSpace($v)) { continue }
        try { $p = (Get-LongFullPath $v).TrimEnd('\') + '\' } catch { return "the environment variable $n could not be resolved" }
        if ($p.Length -le $temp.Length -or -not $p.StartsWith($temp, [StringComparison]::OrdinalIgnoreCase)) {
            return "the environment variable $n points outside the temp folder"
        }
    }
    return $null
}

function Get-PassThroughArgs {
    $a = @()
    foreach ($n in $OverrideNames) {
        $v = Get-Variable -Name $n -Scope Script -ValueOnly
        if ($v) { $a += @("-$n", [string]$v) }
    }
    return , $a
}

function Write-StartupFailure([string]$Why) {
    # Before the window is up: tell the launcher (uninstall.bat then runs the
    # text uninstaller), or the console when run by hand.
    if ($ReasonFile) {
        try { [IO.File]::WriteAllText($ReasonFile, $Why, [Text.Encoding]::ASCII) } catch { }
    } else {
        [Console]::Error.WriteLine("The uninstaller window could not be opened: $Why")
    }
}

function Remove-OwnTree([string]$Path) {
    # Removes a file or a folder tree WITHOUT following links (the same walk as
    # Remove-TreeNoFollow in htt-common.ps1, which this window does not load): a
    # junction or symbolic link inside is deleted as the link and what it points to is
    # left alone; ReadOnly is cleared on the way. Throws on failure.
    $attr = [IO.File]::GetAttributes($Path)
    if (-not ($attr -band [IO.FileAttributes]::Directory)) {
        [IO.File]::SetAttributes($Path, [IO.FileAttributes]::Normal)
        [IO.File]::Delete($Path)
        return
    }
    if (-not ($attr -band [IO.FileAttributes]::ReparsePoint)) {
        [IO.File]::SetAttributes($Path, [IO.FileAttributes]::Directory)
        foreach ($e in @([IO.Directory]::GetFileSystemEntries($Path))) { Remove-OwnTree $e }
    } else {
        try { [IO.File]::SetAttributes($Path, [IO.FileAttributes]::Directory) } catch { }
    }
    [IO.Directory]::Delete($Path, $false)
}

function Get-DelayedCleanupScript {
    # The text of the hidden PowerShell that tries again for up to 30 seconds. The folder
    # reaches it in the environment variable HTT_CLEAN_TARGET (never through cmd, never in
    # the text). It removes only a folder named htt-uninstall-<digits> directly in the temp
    # folder, refuses one that is itself a link (reparse point), and never follows a link
    # inside it. Parent and temp folder are compared as long names (8.3 short names).
    return @'
$t = $env:HTT_CLEAN_TARGET
if (-not $t) { exit 1 }
Add-Type -Namespace H -Name L -MemberDefinition '[DllImport("kernel32.dll",CharSet=CharSet.Unicode,EntryPoint="GetLongPathNameW")]public static extern int G(string a,System.Text.StringBuilder b,int c);'
function HttLong($x) { $b = New-Object Text.StringBuilder 1024; if ([H.L]::G($x, $b, 1024) -gt 0) { $b.ToString() } else { $x } }
function HttDel($p) {
    $a = [int][IO.File]::GetAttributes($p)
    if (-not ($a -band 16)) { [IO.File]::SetAttributes($p, [IO.FileAttributes]::Normal); [IO.File]::Delete($p); return }
    if (-not ($a -band 1024)) {
        [IO.File]::SetAttributes($p, [IO.FileAttributes]::Directory)
        foreach ($e in @([IO.Directory]::GetFileSystemEntries($p))) { HttDel $e }
    } else { try { [IO.File]::SetAttributes($p, [IO.FileAttributes]::Directory) } catch { } }
    [IO.Directory]::Delete($p, $false)
}
$t = HttLong $t
$tmp = (HttLong ([IO.Path]::GetTempPath())).TrimEnd([char]92)
$par = (HttLong (Split-Path -Parent $t)).TrimEnd([char]92)
$leaf = Split-Path -Leaf $t
if (($par -ine $tmp) -or ($leaf -notmatch '^htt-uninstall-[0-9]+$')) { exit 1 }
Start-Sleep -Seconds 3
$end = (Get-Date).AddSeconds(30)
while ((Get-Date) -lt $end) {
    if (-not (Test-Path -LiteralPath $t)) { exit 0 }
    $lk = $false
    try { $lk = [bool]([int][IO.File]::GetAttributes($t) -band 1024) } catch { }
    if ($lk) { exit 1 }
    try { HttDel $t } catch { }
    Start-Sleep -Seconds 1
}
exit 2
'@
}

function Remove-OwnTempCopy {
    # uninstall.bat runs this window from htt-uninstall-<n> in the temp folder
    # (a private copy, so the install folder can be deleted). When the window
    # ends, that copy goes too. Only a folder of exactly that kind is deleted, never
    # one that is itself a link, and no link inside it is followed.
    try {
        $leaf = Split-Path -Leaf $Here
        if ($leaf -notmatch '^htt-uninstall-\d+$') { return }
        $parent = Get-LongFullPath (Split-Path -Parent $Here)
        $temp = Get-LongFullPath ([IO.Path]::GetTempPath())
        if ($parent.TrimEnd('\') -ine $temp.TrimEnd('\')) { return }
        try { if ([IO.File]::GetAttributes($Here) -band [IO.FileAttributes]::ReparsePoint) { return } } catch { }
        try { Set-Location -LiteralPath $temp } catch { }
        for ($i = 1; $i -le 5; $i++) {
            if (-not (Test-Path -LiteralPath $Here)) { return }
            try { Remove-OwnTree $Here } catch { Start-Sleep -Milliseconds (200 * $i) }
        }
        if (Test-Path -LiteralPath $Here) {
            # Something still holds it (a virus scanner): a hidden PowerShell tries again.
            $script = Get-DelayedCleanupScript
            $psi = New-Object Diagnostics.ProcessStartInfo
            $psi.FileName = $PsExe
            $psi.Arguments = '-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -EncodedCommand ' +
                [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($script))
            $psi.UseShellExecute = $false
            $psi.CreateNoWindow = $true
            $psi.WorkingDirectory = $temp
            $psi.EnvironmentVariables['HTT_CLEAN_TARGET'] = $Here
            [void][Diagnostics.Process]::Start($psi)
        }
    } catch { }
}

# -- WPF and the window -----------------------------------------------------------------------

$Xaml = @'
<Window xmlns="http://schemas.microsoft.com/winfx/2006/xaml/presentation"
        xmlns:x="http://schemas.microsoft.com/winfx/2006/xaml"
        xmlns:sys="clr-namespace:System;assembly=mscorlib"
        Title="Remove Hold to talk" Width="780" Height="580" MinWidth="640" MinHeight="420"
        WindowStartupLocation="CenterScreen" UseLayoutRounding="True" SnapsToDevicePixels="True"
        FontFamily="Segoe UI Variable Text, Segoe UI" FontSize="14"
        Background="{DynamicResource Bg}" Foreground="{DynamicResource Text}">
  <Window.Resources>
    <FontFamily x:Key="Display">Segoe UI Variable Display, Segoe UI</FontFamily>
    <FontFamily x:Key="Icons">Segoe Fluent Icons, Segoe MDL2 Assets</FontFamily>
    <FontFamily x:Key="Mono">Consolas, Cascadia Mono, Courier New</FontFamily>

    <Style x:Key="FocusRing">
      <Setter Property="Control.Template">
        <Setter.Value>
          <ControlTemplate>
            <Border Margin="-3" CornerRadius="11" BorderThickness="2" BorderBrush="{DynamicResource Focus}"/>
          </ControlTemplate>
        </Setter.Value>
      </Setter>
    </Style>
    <Style x:Key="FocusRingSmall">
      <Setter Property="Control.Template">
        <Setter.Value>
          <ControlTemplate>
            <Border Margin="-4,-3" CornerRadius="6" BorderThickness="2" BorderBrush="{DynamicResource Focus}"/>
          </ControlTemplate>
        </Setter.Value>
      </Setter>
    </Style>

    <!-- text -->
    <Style x:Key="H1" TargetType="TextBlock">
      <Setter Property="FontFamily" Value="{StaticResource Display}"/>
      <Setter Property="FontSize" Value="26"/>
      <Setter Property="FontWeight" Value="SemiBold"/>
      <Setter Property="TextWrapping" Value="Wrap"/>
    </Style>
    <Style x:Key="H2" TargetType="TextBlock" BasedOn="{StaticResource H1}">
      <Setter Property="FontSize" Value="22"/>
    </Style>
    <Style x:Key="H3" TargetType="TextBlock">
      <Setter Property="FontSize" Value="15"/>
      <Setter Property="FontWeight" Value="SemiBold"/>
      <Setter Property="TextWrapping" Value="Wrap"/>
    </Style>
    <Style x:Key="Body" TargetType="TextBlock">
      <Setter Property="TextWrapping" Value="Wrap"/>
      <Setter Property="LineHeight" Value="21"/>
    </Style>
    <Style x:Key="Muted" TargetType="TextBlock">
      <Setter Property="Foreground" Value="{DynamicResource Text2}"/>
      <Setter Property="FontSize" Value="13"/>
      <Setter Property="TextWrapping" Value="Wrap"/>
      <Setter Property="LineHeight" Value="19"/>
    </Style>
    <Style x:Key="Glyph" TargetType="TextBlock">
      <Setter Property="FontFamily" Value="{StaticResource Icons}"/>
      <Setter Property="FontSize" Value="16"/>
      <Setter Property="Foreground" Value="{DynamicResource Brand}"/>
      <Setter Property="HorizontalAlignment" Value="Center"/>
      <Setter Property="VerticalAlignment" Value="Center"/>
    </Style>
    <Style x:Key="Tile" TargetType="Border">
      <Setter Property="Width" Value="34"/>
      <Setter Property="Height" Value="34"/>
      <Setter Property="CornerRadius" Value="9"/>
      <Setter Property="Background" Value="{DynamicResource BrandSoft}"/>
      <Setter Property="VerticalAlignment" Value="Top"/>
      <Setter Property="HorizontalAlignment" Value="Left"/>
    </Style>
    <Style x:Key="Card" TargetType="Border">
      <Setter Property="Background" Value="{DynamicResource Surface}"/>
      <Setter Property="BorderBrush" Value="{DynamicResource Line}"/>
      <Setter Property="BorderThickness" Value="1"/>
      <Setter Property="CornerRadius" Value="12"/>
      <Setter Property="Padding" Value="18,14"/>
    </Style>
    <Style x:Key="WarnCard" TargetType="Border" BasedOn="{StaticResource Card}">
      <Setter Property="Background" Value="{DynamicResource WarnSoft}"/>
      <Setter Property="BorderBrush" Value="{DynamicResource WarnLine}"/>
    </Style>
    <Style x:Key="BrandCard" TargetType="Border" BasedOn="{StaticResource Card}">
      <Setter Property="Background" Value="{DynamicResource BrandSoft}"/>
      <Setter Property="BorderBrush" Value="{DynamicResource BrandLine}"/>
    </Style>
    <Style x:Key="Rule" TargetType="Border">
      <Setter Property="Height" Value="1"/>
      <Setter Property="Background" Value="{DynamicResource Line}"/>
    </Style>

    <!-- buttons -->
    <Style TargetType="Button">
      <Setter Property="FocusVisualStyle" Value="{StaticResource FocusRing}"/>
      <Setter Property="Background" Value="{DynamicResource Surface}"/>
      <Setter Property="Foreground" Value="{DynamicResource Text}"/>
      <Setter Property="BorderBrush" Value="{DynamicResource Line2}"/>
      <Setter Property="BorderThickness" Value="1"/>
      <Setter Property="Padding" Value="18,0"/>
      <Setter Property="MinWidth" Value="96"/>
      <Setter Property="Height" Value="36"/>
      <Setter Property="Cursor" Value="Hand"/>
      <Setter Property="Template">
        <Setter.Value>
          <ControlTemplate TargetType="Button">
            <Border x:Name="Bd" Background="{TemplateBinding Background}" BorderBrush="{TemplateBinding BorderBrush}"
                    BorderThickness="{TemplateBinding BorderThickness}" CornerRadius="8" Padding="{TemplateBinding Padding}">
              <ContentPresenter HorizontalAlignment="Center" VerticalAlignment="Center" RecognizesAccessKey="False"/>
            </Border>
            <ControlTemplate.Triggers>
              <Trigger Property="IsMouseOver" Value="True">
                <Setter TargetName="Bd" Property="Background" Value="{DynamicResource Surface2}"/>
              </Trigger>
              <Trigger Property="IsPressed" Value="True">
                <Setter TargetName="Bd" Property="Background" Value="{DynamicResource Line}"/>
              </Trigger>
              <Trigger Property="IsEnabled" Value="False">
                <Setter TargetName="Bd" Property="Opacity" Value="0.45"/>
              </Trigger>
            </ControlTemplate.Triggers>
          </ControlTemplate>
        </Setter.Value>
      </Setter>
    </Style>
    <Style x:Key="Primary" TargetType="Button" BasedOn="{StaticResource {x:Type Button}}">
      <Setter Property="Background" Value="{DynamicResource Brand}"/>
      <Setter Property="BorderBrush" Value="{DynamicResource Brand}"/>
      <Setter Property="Foreground" Value="{DynamicResource OnBrand}"/>
      <Setter Property="FontWeight" Value="SemiBold"/>
      <Setter Property="MinWidth" Value="112"/>
      <Setter Property="Template">
        <Setter.Value>
          <ControlTemplate TargetType="Button">
            <Border x:Name="Bd" Background="{TemplateBinding Background}" BorderBrush="{TemplateBinding BorderBrush}"
                    BorderThickness="1" CornerRadius="8" Padding="{TemplateBinding Padding}">
              <ContentPresenter HorizontalAlignment="Center" VerticalAlignment="Center" RecognizesAccessKey="False"/>
            </Border>
            <ControlTemplate.Triggers>
              <Trigger Property="IsMouseOver" Value="True">
                <Setter TargetName="Bd" Property="Background" Value="{DynamicResource BrandInk}"/>
                <Setter TargetName="Bd" Property="BorderBrush" Value="{DynamicResource BrandInk}"/>
              </Trigger>
              <Trigger Property="IsPressed" Value="True">
                <Setter TargetName="Bd" Property="Opacity" Value="0.85"/>
              </Trigger>
              <Trigger Property="IsEnabled" Value="False">
                <Setter TargetName="Bd" Property="Background" Value="{DynamicResource Surface2}"/>
                <Setter TargetName="Bd" Property="BorderBrush" Value="{DynamicResource Line2}"/>
                <Setter Property="Foreground" Value="{DynamicResource Text3}"/>
                <Setter Property="Cursor" Value="Arrow"/>
              </Trigger>
            </ControlTemplate.Triggers>
          </ControlTemplate>
        </Setter.Value>
      </Setter>
    </Style>
    <Style x:Key="Link" TargetType="Button">
      <Setter Property="FocusVisualStyle" Value="{StaticResource FocusRingSmall}"/>
      <Setter Property="Foreground" Value="{DynamicResource Brand}"/>
      <Setter Property="Cursor" Value="Hand"/>
      <Setter Property="FontSize" Value="13"/>
      <Setter Property="Template">
        <Setter.Value>
          <ControlTemplate TargetType="Button">
            <TextBlock x:Name="T" Text="{TemplateBinding Content}" Background="Transparent"/>
            <ControlTemplate.Triggers>
              <Trigger Property="IsMouseOver" Value="True">
                <Setter TargetName="T" Property="TextDecorations" Value="Underline"/>
                <Setter Property="Foreground" Value="{DynamicResource BrandInk}"/>
              </Trigger>
            </ControlTemplate.Triggers>
          </ControlTemplate>
        </Setter.Value>
      </Setter>
    </Style>

    <!-- check box -->
    <Style TargetType="CheckBox">
      <Setter Property="FocusVisualStyle" Value="{StaticResource FocusRingSmall}"/>
      <Setter Property="Foreground" Value="{DynamicResource Text}"/>
      <Setter Property="Cursor" Value="Hand"/>
      <Setter Property="Template">
        <Setter.Value>
          <ControlTemplate TargetType="CheckBox">
            <Grid Background="Transparent">
              <Grid.ColumnDefinitions>
                <ColumnDefinition Width="Auto"/>
                <ColumnDefinition Width="*"/>
              </Grid.ColumnDefinitions>
              <Border x:Name="Box" Width="20" Height="20" CornerRadius="5" BorderThickness="1.5" VerticalAlignment="Top"
                      BorderBrush="{DynamicResource Control}" Background="{DynamicResource Surface}">
                <Path x:Name="Mark" Data="M 3.6,8.6 L 7,12 L 13.4,5.4" Stroke="{DynamicResource OnBrand}" StrokeThickness="2"
                      StrokeStartLineCap="Round" StrokeEndLineCap="Round" StrokeLineJoin="Round" Visibility="Collapsed"/>
              </Border>
              <ContentPresenter Grid.Column="1" Margin="10,0,0,0" VerticalAlignment="Center" RecognizesAccessKey="False"/>
            </Grid>
            <ControlTemplate.Triggers>
              <Trigger Property="IsMouseOver" Value="True">
                <Setter TargetName="Box" Property="BorderBrush" Value="{DynamicResource Brand}"/>
              </Trigger>
              <Trigger Property="IsChecked" Value="True">
                <Setter TargetName="Box" Property="Background" Value="{DynamicResource Brand}"/>
                <Setter TargetName="Box" Property="BorderBrush" Value="{DynamicResource Brand}"/>
                <Setter TargetName="Mark" Property="Visibility" Value="Visible"/>
              </Trigger>
              <Trigger Property="IsEnabled" Value="False">
                <Setter Property="Opacity" Value="0.5"/>
              </Trigger>
            </ControlTemplate.Triggers>
          </ControlTemplate>
        </Setter.Value>
      </Setter>
    </Style>

    <!-- scroll bars (the two sizes make the thumb at least 28 px long) -->
    <sys:Double x:Key="{x:Static SystemParameters.VerticalScrollBarButtonHeightKey}">56</sys:Double>
    <sys:Double x:Key="{x:Static SystemParameters.HorizontalScrollBarButtonWidthKey}">56</sys:Double>
    <Style x:Key="ScrollThumb" TargetType="Thumb">
      <Setter Property="Template">
        <Setter.Value>
          <ControlTemplate TargetType="Thumb">
            <Border x:Name="T" CornerRadius="4" Background="{DynamicResource Line2}" Margin="2"/>
            <ControlTemplate.Triggers>
              <Trigger Property="IsMouseOver" Value="True">
                <Setter TargetName="T" Property="Background" Value="{DynamicResource Control}"/>
              </Trigger>
              <Trigger Property="IsDragging" Value="True">
                <Setter TargetName="T" Property="Background" Value="{DynamicResource Text3}"/>
              </Trigger>
            </ControlTemplate.Triggers>
          </ControlTemplate>
        </Setter.Value>
      </Setter>
    </Style>
    <Style x:Key="ScrollPage" TargetType="RepeatButton">
      <Setter Property="Focusable" Value="False"/>
      <Setter Property="Template">
        <Setter.Value>
          <ControlTemplate TargetType="RepeatButton">
            <Border Background="Transparent"/>
          </ControlTemplate>
        </Setter.Value>
      </Setter>
    </Style>
    <Style TargetType="ScrollBar">
      <Setter Property="Width" Value="12"/>
      <Setter Property="MinWidth" Value="12"/>
      <Setter Property="Background" Value="Transparent"/>
      <Setter Property="Template">
        <Setter.Value>
          <ControlTemplate TargetType="ScrollBar">
            <Track x:Name="PART_Track" IsDirectionReversed="True">
              <Track.DecreaseRepeatButton><RepeatButton Style="{StaticResource ScrollPage}" Command="ScrollBar.PageUpCommand"/></Track.DecreaseRepeatButton>
              <Track.IncreaseRepeatButton><RepeatButton Style="{StaticResource ScrollPage}" Command="ScrollBar.PageDownCommand"/></Track.IncreaseRepeatButton>
              <Track.Thumb><Thumb Style="{StaticResource ScrollThumb}"/></Track.Thumb>
            </Track>
          </ControlTemplate>
        </Setter.Value>
      </Setter>
      <Style.Triggers>
        <Trigger Property="Orientation" Value="Horizontal">
          <Setter Property="Width" Value="Auto"/>
          <Setter Property="MinWidth" Value="0"/>
          <Setter Property="Height" Value="12"/>
          <Setter Property="MinHeight" Value="12"/>
          <Setter Property="Template">
            <Setter.Value>
              <ControlTemplate TargetType="ScrollBar">
                <Track x:Name="PART_Track">
                  <Track.DecreaseRepeatButton><RepeatButton Style="{StaticResource ScrollPage}" Command="ScrollBar.PageLeftCommand"/></Track.DecreaseRepeatButton>
                  <Track.IncreaseRepeatButton><RepeatButton Style="{StaticResource ScrollPage}" Command="ScrollBar.PageRightCommand"/></Track.IncreaseRepeatButton>
                  <Track.Thumb><Thumb Style="{StaticResource ScrollThumb}"/></Track.Thumb>
                </Track>
              </ControlTemplate>
            </Setter.Value>
          </Setter>
        </Trigger>
      </Style.Triggers>
    </Style>

    <!-- expander -->
    <Style x:Key="ExpanderToggle" TargetType="ToggleButton">
      <Setter Property="FocusVisualStyle" Value="{StaticResource FocusRingSmall}"/>
      <Setter Property="Foreground" Value="{DynamicResource Brand}"/>
      <Setter Property="FontWeight" Value="SemiBold"/>
      <Setter Property="FontSize" Value="13"/>
      <Setter Property="Cursor" Value="Hand"/>
      <Setter Property="Template">
        <Setter.Value>
          <ControlTemplate TargetType="ToggleButton">
            <StackPanel Orientation="Horizontal" Background="Transparent">
              <TextBlock x:Name="Chev" Text="&#xE76C;" FontFamily="{StaticResource Icons}" FontSize="10" FontWeight="Normal"
                         VerticalAlignment="Center" Margin="0,1,8,0" RenderTransformOrigin="0.5,0.5">
                <TextBlock.RenderTransform><RotateTransform Angle="0"/></TextBlock.RenderTransform>
              </TextBlock>
              <ContentPresenter VerticalAlignment="Center" RecognizesAccessKey="False"/>
            </StackPanel>
            <ControlTemplate.Triggers>
              <Trigger Property="IsChecked" Value="True">
                <Setter TargetName="Chev" Property="RenderTransform">
                  <Setter.Value><RotateTransform Angle="90"/></Setter.Value>
                </Setter>
              </Trigger>
              <Trigger Property="IsMouseOver" Value="True">
                <Setter Property="Foreground" Value="{DynamicResource BrandInk}"/>
              </Trigger>
            </ControlTemplate.Triggers>
          </ControlTemplate>
        </Setter.Value>
      </Setter>
    </Style>
    <Style TargetType="Expander">
      <Setter Property="Template">
        <Setter.Value>
          <ControlTemplate TargetType="Expander">
            <DockPanel>
              <ToggleButton DockPanel.Dock="Top" HorizontalAlignment="Left" Style="{StaticResource ExpanderToggle}"
                            Content="{TemplateBinding Header}"
                            IsChecked="{Binding IsExpanded, Mode=TwoWay, RelativeSource={RelativeSource TemplatedParent}}"/>
              <ContentPresenter x:Name="Body" Margin="0,10,0,0" Visibility="Collapsed"/>
            </DockPanel>
            <ControlTemplate.Triggers>
              <Trigger Property="IsExpanded" Value="True">
                <Setter TargetName="Body" Property="Visibility" Value="Visible"/>
              </Trigger>
            </ControlTemplate.Triggers>
          </ControlTemplate>
        </Setter.Value>
      </Setter>
    </Style>

    <!-- text boxes -->
    <Style x:Key="LogBox" TargetType="TextBox">
      <Setter Property="IsReadOnly" Value="True"/>
      <Setter Property="FocusVisualStyle" Value="{x:Null}"/>
      <Setter Property="FontFamily" Value="{StaticResource Mono}"/>
      <Setter Property="FontSize" Value="12"/>
      <Setter Property="Foreground" Value="{DynamicResource Text2}"/>
      <Setter Property="Background" Value="{DynamicResource Surface}"/>
      <Setter Property="BorderBrush" Value="{DynamicResource Line}"/>
      <Setter Property="CaretBrush" Value="{DynamicResource Text}"/>
      <Setter Property="SelectionBrush" Value="{DynamicResource Brand}"/>
      <Setter Property="Padding" Value="10,8"/>
      <Setter Property="TextWrapping" Value="Wrap"/>
      <Setter Property="VerticalScrollBarVisibility" Value="Auto"/>
      <Setter Property="Template">
        <Setter.Value>
          <ControlTemplate TargetType="TextBox">
            <Border x:Name="Bd" CornerRadius="8" BorderThickness="1" Background="{TemplateBinding Background}" BorderBrush="{TemplateBinding BorderBrush}">
              <ScrollViewer x:Name="PART_ContentHost" Margin="{TemplateBinding Padding}" Focusable="False"/>
            </Border>
            <ControlTemplate.Triggers>
              <Trigger Property="IsKeyboardFocused" Value="True">
                <Setter TargetName="Bd" Property="BorderBrush" Value="{DynamicResource Focus}"/>
                <Setter TargetName="Bd" Property="BorderThickness" Value="2"/>
              </Trigger>
            </ControlTemplate.Triggers>
          </ControlTemplate>
        </Setter.Value>
      </Setter>
    </Style>
    <Style x:Key="PathBox" TargetType="TextBox">
      <Setter Property="IsReadOnly" Value="True"/>
      <Setter Property="FocusVisualStyle" Value="{x:Null}"/>
      <Setter Property="FontSize" Value="12.5"/>
      <Setter Property="Foreground" Value="{DynamicResource Text2}"/>
      <Setter Property="CaretBrush" Value="{DynamicResource Text}"/>
      <Setter Property="SelectionBrush" Value="{DynamicResource Brand}"/>
      <Setter Property="TextWrapping" Value="Wrap"/>
      <Setter Property="Template">
        <Setter.Value>
          <ControlTemplate TargetType="TextBox">
            <Border x:Name="Bd" CornerRadius="5" BorderThickness="0" Background="{DynamicResource Surface2}" Padding="6,3">
              <ScrollViewer x:Name="PART_ContentHost" Focusable="False" Margin="-2,0"/>
            </Border>
            <ControlTemplate.Triggers>
              <Trigger Property="IsKeyboardFocused" Value="True">
                <Setter TargetName="Bd" Property="BorderThickness" Value="1.5"/>
                <Setter TargetName="Bd" Property="BorderBrush" Value="{DynamicResource Focus}"/>
              </Trigger>
            </ControlTemplate.Triggers>
          </ControlTemplate>
        </Setter.Value>
      </Setter>
    </Style>

    <!-- progress bar: determinate by step count; a moving sheen while a long step runs -->
    <Style TargetType="ProgressBar">
      <Setter Property="Height" Value="8"/>
      <Setter Property="Minimum" Value="0"/>
      <Setter Property="Maximum" Value="1"/>
      <Setter Property="Foreground" Value="{DynamicResource Brand}"/>
      <Setter Property="Background" Value="{DynamicResource Line}"/>
      <Setter Property="Template">
        <Setter.Value>
          <ControlTemplate TargetType="ProgressBar">
            <Grid ClipToBounds="True">
              <Border x:Name="PART_Track" CornerRadius="4" Background="{TemplateBinding Background}"/>
              <Border x:Name="PART_Indicator" CornerRadius="4" Background="{TemplateBinding Foreground}" HorizontalAlignment="Left"/>
              <Border x:Name="Sheen" CornerRadius="4" Background="{TemplateBinding Foreground}" Opacity="0">
                <Border.OpacityMask>
                  <LinearGradientBrush StartPoint="0,0" EndPoint="1,0">
                    <LinearGradientBrush.RelativeTransform>
                      <TranslateTransform X="-1"/>
                    </LinearGradientBrush.RelativeTransform>
                    <GradientStop Color="#00000000" Offset="0.0"/>
                    <GradientStop Color="#00000000" Offset="0.3"/>
                    <GradientStop Color="#FF000000" Offset="0.5"/>
                    <GradientStop Color="#00000000" Offset="0.7"/>
                    <GradientStop Color="#00000000" Offset="1.0"/>
                  </LinearGradientBrush>
                </Border.OpacityMask>
              </Border>
            </Grid>
            <ControlTemplate.Triggers>
              <Trigger Property="Tag" Value="busy">
                <Setter TargetName="Sheen" Property="Opacity" Value="0.45"/>
                <Trigger.EnterActions>
                  <BeginStoryboard x:Name="SheenRun">
                    <Storyboard RepeatBehavior="Forever">
                      <DoubleAnimation Storyboard.TargetName="Sheen"
                                       Storyboard.TargetProperty="(UIElement.OpacityMask).(Brush.RelativeTransform).(TranslateTransform.X)"
                                       From="-0.7" To="0.7" Duration="0:0:1.8"/>
                    </Storyboard>
                  </BeginStoryboard>
                </Trigger.EnterActions>
                <Trigger.ExitActions>
                  <StopStoryboard BeginStoryboardName="SheenRun"/>
                </Trigger.ExitActions>
              </Trigger>
            </ControlTemplate.Triggers>
          </ControlTemplate>
        </Setter.Value>
      </Setter>
    </Style>

    <!-- a small spinner (Control with this template) -->
    <ControlTemplate x:Key="Spinner" TargetType="Control">
      <Grid Width="18" Height="18">
        <Ellipse Stroke="{DynamicResource Line2}" StrokeThickness="2"/>
        <Path x:Name="Arc" Data="M 9,1 A 8,8 0 0 1 17,9" Stroke="{DynamicResource Brand}" StrokeThickness="2"
              StrokeStartLineCap="Round" StrokeEndLineCap="Round" RenderTransformOrigin="0.5,0.5">
          <Path.RenderTransform><RotateTransform Angle="0"/></Path.RenderTransform>
        </Path>
      </Grid>
      <ControlTemplate.Triggers>
        <EventTrigger RoutedEvent="FrameworkElement.Loaded">
          <BeginStoryboard>
            <Storyboard RepeatBehavior="Forever">
              <DoubleAnimation Storyboard.TargetName="Arc" Storyboard.TargetProperty="(UIElement.RenderTransform).(RotateTransform.Angle)"
                               From="0" To="360" Duration="0:0:0.9"/>
            </Storyboard>
          </BeginStoryboard>
        </EventTrigger>
      </ControlTemplate.Triggers>
    </ControlTemplate>
  </Window.Resources>

  <Grid x:Name="Root" Background="{DynamicResource Bg}">
    <Grid.ColumnDefinitions>
      <ColumnDefinition Width="208"/>
      <ColumnDefinition Width="*"/>
    </Grid.ColumnDefinitions>

    <!-- the rail: the app and the three steps -->
    <Border Background="{DynamicResource Side}" BorderBrush="{DynamicResource Line}" BorderThickness="0,0,1,0">
      <DockPanel Margin="22,24,18,20">
        <StackPanel DockPanel.Dock="Top" Orientation="Horizontal">
          <Image x:Name="RailIcon" Width="32" Height="32" RenderOptions.BitmapScalingMode="HighQuality" VerticalAlignment="Center"/>
          <StackPanel Margin="10,0,0,0" VerticalAlignment="Center">
            <TextBlock Text="Hold to talk" FontWeight="SemiBold" FontSize="15"/>
            <TextBlock x:Name="RailSub" Text="Uninstall" Foreground="{DynamicResource Text2}" FontSize="12.5"/>
          </StackPanel>
        </StackPanel>
        <Border x:Name="TestBadge" DockPanel.Dock="Bottom" HorizontalAlignment="Left" CornerRadius="6" Padding="8,3"
                Background="{DynamicResource WarnSoft}" BorderBrush="{DynamicResource WarnLine}" BorderThickness="1" Visibility="Collapsed"
                AutomationProperties.Name="Test mode: temporary places, the real app is not touched">
          <TextBlock Text="Test mode" FontSize="12" FontWeight="SemiBold" Foreground="{DynamicResource Warn}"/>
        </Border>
        <TextBlock x:Name="RailVersion" DockPanel.Dock="Bottom" Foreground="{DynamicResource Text2}" FontSize="12.5" Margin="0,0,0,10"/>
        <StackPanel x:Name="RailSteps" Margin="0,34,0,0">
{{RAIL}}
        </StackPanel>
      </DockPanel>
    </Border>

    <Grid x:Name="Main" Grid.Column="1">
      <Grid.RowDefinitions>
        <RowDefinition Height="*"/>
        <RowDefinition Height="Auto"/>
      </Grid.RowDefinitions>
      <Grid x:Name="Pages">

        <!-- 1. Remove Hold to talk? -->
        <Grid x:Name="PageAsk">
         <Grid.RowDefinitions>
           <RowDefinition Height="*"/>
           <RowDefinition Height="Auto"/>
         </Grid.RowDefinitions>
         <ScrollViewer x:Name="AskScroll" VerticalScrollBarVisibility="Auto" Focusable="False">
          <StackPanel Margin="40,24,40,14">
            <TextBlock Text="Remove Hold to talk?" Style="{StaticResource H1}" FontSize="26"/>
            <TextBlock x:Name="AskSub" Style="{StaticResource Muted}" Margin="0,4,0,12"
                       Text="This removes the app from your Windows account. You are asked before anything of yours is deleted."/>

            <StackPanel x:Name="CheckingRow" Orientation="Horizontal" Margin="2,0,0,14">
              <Control Template="{StaticResource Spinner}" Focusable="False" VerticalAlignment="Center"/>
              <TextBlock Text="Checking this PC..." Foreground="{DynamicResource Text2}" Margin="10,0,0,0" VerticalAlignment="Center"/>
            </StackPanel>
            <Border x:Name="NotFoundCard" Style="{StaticResource WarnCard}" Margin="0,0,0,14" Padding="14,10" Visibility="Collapsed">
              <Grid>
                <Grid.ColumnDefinitions><ColumnDefinition Width="28"/><ColumnDefinition Width="*"/></Grid.ColumnDefinitions>
                <TextBlock Style="{StaticResource Glyph}" Text="&#xE7BA;" Foreground="{DynamicResource Warn}" VerticalAlignment="Top" HorizontalAlignment="Left" Margin="0,2,0,0"/>
                <TextBlock Grid.Column="1" Style="{StaticResource Body}"
                           Text="Hold to talk does not seem to be installed in the usual place any more. Remove still clears away what is left."/>
              </Grid>
            </Border>

            <!-- what will be removed -->
            <TextBlock Style="{StaticResource H3}" Text="What will be removed"/>
            <Border Style="{StaticResource Card}" Padding="0" Margin="0,8,0,0">
              <StackPanel>
                <Grid Margin="18,10,18,10">
                  <Grid.ColumnDefinitions><ColumnDefinition Width="48"/><ColumnDefinition Width="*"/></Grid.ColumnDefinitions>
                  <Border Style="{StaticResource Tile}"><TextBlock Style="{StaticResource Glyph}" Text="&#xE74C;"/></Border>
                  <StackPanel Grid.Column="1">
                    <TextBlock Text="The app" FontWeight="SemiBold"/>
                    <TextBox x:Name="ProgramPath" Style="{StaticResource PathBox}" Margin="0,4,0,0" AutomationProperties.Name="Program files folder"/>
                  </StackPanel>
                </Grid>
                <Border Style="{StaticResource Rule}"/>
                <Grid Margin="18,10,18,10">
                  <Grid.ColumnDefinitions><ColumnDefinition Width="48"/><ColumnDefinition Width="*"/></Grid.ColumnDefinitions>
                  <Border Style="{StaticResource Tile}"><TextBlock Style="{StaticResource Glyph}" Text="&#xE943;"/></Border>
                  <StackPanel Grid.Column="1">
                    <TextBlock Text="Its private Python copy" FontWeight="SemiBold"/>
                    <TextBox x:Name="EnvPath" Style="{StaticResource PathBox}" Margin="0,4,0,0" AutomationProperties.Name="Python environment folder"/>
                  </StackPanel>
                </Grid>
                <Border Style="{StaticResource Rule}"/>
                <Grid Margin="18,10,18,10">
                  <Grid.ColumnDefinitions><ColumnDefinition Width="48"/><ColumnDefinition Width="*"/></Grid.ColumnDefinitions>
                  <Border Style="{StaticResource Tile}"><TextBlock Style="{StaticResource Glyph}" Text="&#xE71B;"/></Border>
                  <StackPanel Grid.Column="1">
                    <TextBlock Text="Shortcuts and entries" FontWeight="SemiBold"/>
                    <TextBlock Style="{StaticResource Muted}" Text="The Start menu and Desktop shortcuts, the start with Windows entry and the entry in Settings > Apps."/>
                  </StackPanel>
                </Grid>
              </StackPanel>
            </Border>

            <!-- what is kept -->
            <TextBlock Style="{StaticResource H3}" Text="What is kept" Margin="0,14,0,0"/>
            <Border Style="{StaticResource Card}" Padding="0" Margin="0,8,0,0">
              <StackPanel>
                <Grid Margin="18,10,18,10">
                  <Grid.ColumnDefinitions><ColumnDefinition Width="48"/><ColumnDefinition Width="*"/></Grid.ColumnDefinitions>
                  <Border Style="{StaticResource Tile}"><TextBlock Style="{StaticResource Glyph}" Text="&#xE8F1;"/></Border>
                  <StackPanel Grid.Column="1">
                    <TextBlock Text="Your settings, dictation history and logs" FontWeight="SemiBold" TextWrapping="Wrap"/>
                    <TextBlock x:Name="DataWhat" Style="{StaticResource Muted}"/>
                    <TextBox x:Name="DataPath" Style="{StaticResource PathBox}" Margin="0,4,0,0" AutomationProperties.Name="Your data folder"/>
                  </StackPanel>
                </Grid>
                <Border Style="{StaticResource Rule}"/>
                <Grid Margin="18,10,18,10">
                  <Grid.ColumnDefinitions><ColumnDefinition Width="48"/><ColumnDefinition Width="*"/></Grid.ColumnDefinitions>
                  <Border Style="{StaticResource Tile}"><TextBlock Style="{StaticResource Glyph}" Text="&#xE720;"/></Border>
                  <StackPanel Grid.Column="1">
                    <TextBlock Text="Not part of the app" FontWeight="SemiBold"/>
                    <TextBlock Style="{StaticResource Muted}" Text="The downloaded speech models, pip's download cache, Python itself and Ollama are never touched. The last page says where they are."/>
                  </StackPanel>
                </Grid>
              </StackPanel>
            </Border>

          </StackPanel>
         </ScrollViewer>
         <!-- always visible, in its own row under the scrolling part (never over it): the choice that matters most on this page -->
         <Border Grid.Row="1" x:Name="DataBoxRow" Padding="40,10,40,12" BorderThickness="0,1,0,0"
                 Background="{DynamicResource Bg}" BorderBrush="{DynamicResource Line}">
           <StackPanel>
             <TextBlock x:Name="KeptNote" Style="{StaticResource Muted}" TextWrapping="Wrap" Margin="2,0,0,8"
                        Text="Your settings, history and logs are kept unless you tick this box."/>
             <Border Padding="16,10" CornerRadius="10" BorderThickness="1"
                     Background="{DynamicResource Surface}" BorderBrush="{DynamicResource Line2}">
               <CheckBox x:Name="DataBox" AutomationProperties.Name="Also delete my settings, dictation history and logs (this cannot be undone)">
                 <TextBlock x:Name="DataBoxText" TextWrapping="Wrap" FontWeight="SemiBold"
                            Text="Also delete my settings, dictation history and logs (this cannot be undone)"/>
               </CheckBox>
             </Border>
           </StackPanel>
         </Border>
        </Grid>

        <!-- 2. Removing -->
        <Grid x:Name="PageProgress" Visibility="Collapsed">
          <Grid.RowDefinitions>
            <RowDefinition Height="Auto"/>
            <RowDefinition Height="*"/>
          </Grid.RowDefinitions>
          <StackPanel Margin="40,28,40,0">
            <TextBlock Text="Removing Hold to talk" Style="{StaticResource H2}"/>
            <TextBlock Style="{StaticResource Muted}" Margin="0,4,0,0"
                       Text="This takes a moment. Please wait until it says it is done."/>
            <DockPanel Margin="0,18,0,0">
              <TextBlock x:Name="StepCount" DockPanel.Dock="Right" Style="{StaticResource Muted}"/>
              <TextBlock x:Name="StepNow" FontWeight="SemiBold" TextTrimming="CharacterEllipsis"/>
            </DockPanel>
            <ProgressBar x:Name="Bar" Margin="0,10,0,12" AutomationProperties.Name="Removal progress"/>
          </StackPanel>
          <ScrollViewer x:Name="ProgressScroll" Grid.Row="1" VerticalScrollBarVisibility="Auto" Focusable="False">
            <StackPanel Margin="40,6,40,24">
              <Border Style="{StaticResource Card}" Padding="18,10">
                <StackPanel x:Name="StepList"/>
              </Border>
              <Expander x:Name="DetailsExp" Header="Show details" Margin="2,16,0,0">
                <TextBox x:Name="LogBox" Style="{StaticResource LogBox}" Height="170" AutomationProperties.Name="Removal details"/>
              </Expander>
            </StackPanel>
          </ScrollViewer>
        </Grid>

        <!-- 3. Result -->
        <ScrollViewer x:Name="PageResult" VerticalScrollBarVisibility="Auto" Focusable="False" Visibility="Collapsed">
          <StackPanel Margin="40,30,40,24">
            <Border x:Name="ResultBadge" Width="48" Height="48" CornerRadius="24" HorizontalAlignment="Left">
              <TextBlock x:Name="ResultGlyph" Style="{StaticResource Glyph}" FontSize="22"/>
            </Border>
            <TextBlock x:Name="ResultTitle" Style="{StaticResource H1}" Margin="0,16,0,0"/>
            <TextBlock x:Name="ResultLead" Style="{StaticResource Body}" Margin="0,6,0,0"/>

            <StackPanel x:Name="SuccessPanel">
              <StackPanel x:Name="WarnList"/>
              <Border Style="{StaticResource Card}" Margin="0,16,0,0">
                <StackPanel>
                  <TextBlock x:Name="DataResultTitle" Style="{StaticResource H3}"/>
                  <TextBlock x:Name="DataResultText" Style="{StaticResource Body}" Margin="0,6,0,0"/>
                  <TextBox x:Name="DataResultPath" Style="{StaticResource PathBox}" Margin="0,6,0,0" HorizontalAlignment="Left" AutomationProperties.Name="Your data folder"/>
                </StackPanel>
              </Border>
              <Border Style="{StaticResource Card}" Margin="0,10,0,0">
                <StackPanel>
                  <TextBlock Style="{StaticResource H3}" Text="Left on this PC on purpose"/>
                  <TextBlock Style="{StaticResource Muted}" Margin="0,2,0,0" Text="These are not part of the app. Delete them yourself if you like."/>
                  <Grid Margin="0,10,0,0">
                    <Grid.ColumnDefinitions><ColumnDefinition Width="28"/><ColumnDefinition Width="*"/></Grid.ColumnDefinitions>
                    <TextBlock Style="{StaticResource Glyph}" Text="&#xE720;" FontSize="17" VerticalAlignment="Top" HorizontalAlignment="Left" Margin="0,1,0,0"/>
                    <StackPanel Grid.Column="1">
                      <TextBlock Style="{StaticResource Body}" Text="Speech models (folders named models--...whisper..., a few hundred MB to 1.5 GB each):"/>
                      <TextBox x:Name="ModelPath" Style="{StaticResource PathBox}" Margin="0,4,0,0" AutomationProperties.Name="Speech model folder"/>
                      <TextBlock x:Name="ModelList" Style="{StaticResource Muted}" Margin="0,4,0,0"/>
                    </StackPanel>
                  </Grid>
                  <Grid Margin="0,10,0,0">
                    <Grid.ColumnDefinitions><ColumnDefinition Width="28"/><ColumnDefinition Width="*"/></Grid.ColumnDefinitions>
                    <TextBlock Style="{StaticResource Glyph}" Text="&#xE896;" FontSize="17" VerticalAlignment="Top" HorizontalAlignment="Left" Margin="0,1,0,0"/>
                    <TextBlock Grid.Column="1" Style="{StaticResource Body}"
                               Text="pip's download cache: %LOCALAPPDATA%\pip\Cache (or run: python -m pip cache purge)."/>
                  </Grid>
                  <Grid Margin="0,10,0,0">
                    <Grid.ColumnDefinitions><ColumnDefinition Width="28"/><ColumnDefinition Width="*"/></Grid.ColumnDefinitions>
                    <TextBlock Style="{StaticResource Glyph}" Text="&#xE945;" FontSize="17" VerticalAlignment="Top" HorizontalAlignment="Left" Margin="0,1,0,0"/>
                    <TextBlock Grid.Column="1" Style="{StaticResource Body}"
                               Text="Ollama and its AI models, if you installed them: Settings > Apps > Ollama. Models: run ollama list, then ollama rm &lt;name&gt; (gemma4:e2b-it-qat)."/>
                  </Grid>
                </StackPanel>
              </Border>
            </StackPanel>

            <StackPanel x:Name="FailPanel" Visibility="Collapsed">
              <TextBlock Style="{StaticResource H3}" Text="What the uninstaller said" Margin="0,18,0,8"/>
              <TextBox x:Name="ErrorBox" Style="{StaticResource LogBox}" FontFamily="Segoe UI Variable Text, Segoe UI" FontSize="13.5" Foreground="{DynamicResource Text}"
                       MaxHeight="220" AutomationProperties.Name="Error message"/>
            </StackPanel>
          </StackPanel>
        </ScrollViewer>

        <!-- an error before anything was removed -->
        <ScrollViewer x:Name="PageError" VerticalScrollBarVisibility="Auto" Focusable="False" Visibility="Collapsed">
          <StackPanel Margin="40,30,40,24">
            <Border Width="48" Height="48" CornerRadius="24" HorizontalAlignment="Left" Background="{DynamicResource BadSoft}">
              <TextBlock Style="{StaticResource Glyph}" FontSize="22" Text="&#xE783;" Foreground="{DynamicResource Bad}"/>
            </Border>
            <TextBlock x:Name="ErrTitle" Style="{StaticResource H1}" FontSize="24" Margin="0,16,0,0"/>
            <TextBlock x:Name="ErrText" Style="{StaticResource Body}" Margin="0,8,0,0"/>
            <StackPanel x:Name="ErrDetailsPanel" Margin="0,16,0,0">
              <TextBlock Style="{StaticResource H3}" Text="Details" Margin="0,0,0,8"/>
              <TextBox x:Name="ErrDetails" Style="{StaticResource LogBox}" MaxHeight="200" AutomationProperties.Name="Error details"/>
            </StackPanel>
          </StackPanel>
        </ScrollViewer>
      </Grid>

      <!-- the button row: always visible -->
      <Border Grid.Row="1" BorderBrush="{DynamicResource Line}" BorderThickness="0,1,0,0" Background="{DynamicResource Bg}" Padding="24,14">
        <DockPanel LastChildFill="False">
          <Button x:Name="CancelBtn" DockPanel.Dock="Left" Content="Cancel"/>
          <Button x:Name="CopyBtn" DockPanel.Dock="Left" Content="Copy details" Visibility="Collapsed"/>
          <Button x:Name="NextBtn" DockPanel.Dock="Right" Style="{StaticResource Primary}" Content="Remove" IsDefault="True"/>
        </DockPanel>
      </Border>
    </Grid>

    <!-- "Delete your settings and history for good?" -->
    <Grid x:Name="Overlay" Grid.ColumnSpan="2" Visibility="Collapsed">
      <Rectangle Fill="#000000" Opacity="0.38"/>
      <Border Background="{DynamicResource Surface}" BorderBrush="{DynamicResource Line2}" BorderThickness="1" CornerRadius="14"
              Width="450" Padding="26,22" HorizontalAlignment="Center" VerticalAlignment="Center">
        <Border.Effect><DropShadowEffect BlurRadius="28" ShadowDepth="6" Opacity="0.28" Color="#000000"/></Border.Effect>
        <StackPanel>
          <TextBlock x:Name="ConfirmTitle" Style="{StaticResource H3}" FontSize="18" FontFamily="{StaticResource Display}" Text="Delete your settings and history for good?"/>
          <TextBlock x:Name="ConfirmBody" Style="{StaticResource Body}" Margin="0,8,0,0"
                     Text="Your settings, dictation history and logs will be deleted from this PC. This cannot be undone."/>
          <StackPanel Orientation="Horizontal" HorizontalAlignment="Right" Margin="0,20,0,0">
            <Button x:Name="DeleteBtn" Content="Delete" Margin="0,0,10,0"/>
            <Button x:Name="KeepBtn" Content="Keep" Style="{StaticResource Primary}"/>
          </StackPanel>
        </StackPanel>
      </Border>
    </Grid>
  </Grid>
</Window>
'@

$RailLabels = 'Confirm', 'Removing', 'Finish'
$RailXaml = ''
for ($i = 1; $i -le 3; $i++) {
    $up = ''; $down = ''
    if ($i -gt 1) { $up = '<Border Width="2" Height="9" VerticalAlignment="Top" HorizontalAlignment="Center" Background="{DynamicResource Line2}"/>' }
    if ($i -lt 3) { $down = '<Border Width="2" Height="9" VerticalAlignment="Bottom" HorizontalAlignment="Center" Background="{DynamicResource Line2}"/>' }
    $RailXaml += @"
          <Grid Height="44">
            <Grid.ColumnDefinitions><ColumnDefinition Width="26"/><ColumnDefinition Width="*"/></Grid.ColumnDefinitions>
            $up $down
            <Border x:Name="RailDot$i" Width="26" Height="26" CornerRadius="13" BorderThickness="1.5" VerticalAlignment="Center">
              <TextBlock x:Name="RailNum$i" Text="$i" FontSize="12" HorizontalAlignment="Center" VerticalAlignment="Center"/>
            </Border>
            <TextBlock x:Name="RailText$i" Grid.Column="1" Text="$($RailLabels[$i - 1])" Margin="12,0,0,0" VerticalAlignment="Center" FontSize="13.5"/>
          </Grid>

"@
}
$Xaml = $Xaml.Replace('{{RAIL}}', $RailXaml)

$StepRowXaml = @'
<Grid xmlns="http://schemas.microsoft.com/winfx/2006/xaml/presentation" xmlns:x="http://schemas.microsoft.com/winfx/2006/xaml" Margin="0,6">
  <Grid.ColumnDefinitions><ColumnDefinition Width="32"/><ColumnDefinition Width="*"/></Grid.ColumnDefinitions>
  <Grid Width="20" Height="20" VerticalAlignment="Top" HorizontalAlignment="Left" Margin="0,0,0,0">
    <Ellipse x:Name="Ring" Stroke="{DynamicResource Line2}" StrokeThickness="1.5"/>
    <Ellipse x:Name="Fill" Fill="{DynamicResource Brand}" Visibility="Collapsed"/>
    <TextBlock x:Name="Tick" Text="&#xE73E;" FontFamily="Segoe Fluent Icons, Segoe MDL2 Assets" FontSize="11"
               Foreground="{DynamicResource OnBrand}" HorizontalAlignment="Center" VerticalAlignment="Center" Visibility="Collapsed"/>
    <Control x:Name="Spin" Template="{DynamicResource Spinner}" Focusable="False" Width="20" Height="20" Visibility="Collapsed"/>
  </Grid>
  <StackPanel Grid.Column="1">
    <TextBlock x:Name="Title" TextWrapping="Wrap" Margin="0,0,0,0"/>
    <TextBlock x:Name="Act" Foreground="{DynamicResource Text2}" FontSize="13" TextWrapping="Wrap" TextTrimming="CharacterEllipsis" MaxHeight="40" Margin="0,3,0,0" Visibility="Collapsed"/>
    <TextBlock x:Name="Last" Foreground="{DynamicResource Text3}" FontFamily="Consolas, Cascadia Mono, Courier New" FontSize="12"
               TextTrimming="CharacterEllipsis" Margin="0,3,0,0" Visibility="Collapsed"/>
  </StackPanel>
</Grid>
'@

$Palettes = @{
    light = [ordered]@{
        Bg = '#f5f3ef'; Side = '#efece6'; Surface = '#fffefb'; Surface2 = '#f3f0ea'; Line = '#e4e0d8'; Line2 = '#d3cec4'
        Text = '#1d1c1a'; Text2 = '#5c5952'; Text3 = '#6b675f'; Control = '#8a857c'
        Brand = '#137a70'; BrandInk = '#0e6159'; BrandSoft = '#dcefeb'; BrandLine = '#b9ddd6'; OnBrand = '#ffffff'
        Ok = '#2b7d4f'; OkSoft = '#dcefe2'; Warn = '#875408'; WarnSoft = '#f8ecd6'; WarnLine = '#ecd3a6'
        Bad = '#b8392a'; BadSoft = '#f9e0da'; Focus = '#2f6fd6'
    }
    dark = [ordered]@{
        Bg = '#171615'; Side = '#1c1b19'; Surface = '#222120'; Surface2 = '#2a2826'; Line = '#34322f'; Line2 = '#46433f'
        Text = '#f0ede7'; Text2 = '#b6b1a8'; Text3 = '#a39d93'; Control = '#8f897f'
        Brand = '#3cb5a7'; BrandInk = '#69cfc2'; BrandSoft = '#173430'; BrandLine = '#24524b'; OnBrand = '#0d1f1d'
        Ok = '#5cc28a'; OkSoft = '#1a3324'; Warn = '#e2aa4c'; WarnSoft = '#382c17'; WarnLine = '#5a4520'
        Bad = '#f07e6a'; BadSoft = '#3d221d'; Focus = '#7fa8ff'
    }
}


# -- state ----------------------------------------------------------------------------------

$script:Harness = $null          # 'selftest' / 'shots' / 'autorun' (tests only)
$script:Facts = $null
$script:Page = 'ask'
$script:Run = $null
$script:Checking = $false
$script:PendingFacts = $null
$script:CheckLog = New-Object Collections.Generic.List[string]
$script:CheckOut = New-Object Collections.Generic.List[string]
$script:FullLog = New-Object Collections.Generic.List[string]
$script:LogPending = New-Object Collections.Generic.List[string]
$script:MaxLogLines = 4000
$script:MaxSteps = 30
$script:StepRows = @()
$script:StepTotal = 0
$script:CurrentStep = 0
$script:Warnings = @()
$script:ErrorText = $null
$script:DoneInfo = $null
$script:DataConfirmed = $false
$script:DataAnswer = $null       # what the engine was given: 'yes' / 'no'
$script:Outcome = $null          # exit code once a removal was attempted
$script:ResultKind = $null       # success / failure
$script:ExitCode = $null
$script:AllowClose = $false
$script:ClosedWith = $null       # selftest: what Close-Window was asked for
$script:EngineCalls = New-Object Collections.Generic.List[object]
$script:ThemeMode = 'light'
$script:Ready = $false
$script:Copied = $null

function Test-TestMode {
    if ($script:GivenOverrides.Count -gt 0) { return $true }
    if ($script:Facts -and $script:Facts.testMode -eq $true) { return $true }
    return ($null -ne $script:Harness)
}

# -- small helpers ---------------------------------------------------------------------------

function Set-Res($Element, $Property, [string]$Key) { $Element.SetResourceReference($Property, $Key) }

function Set-Shown($Element, [bool]$Shown) {
    if ($Shown) { $Element.Visibility = 'Visible' } else { $Element.Visibility = 'Collapsed' }
}

function Limit-Text([string]$Text, [int]$Max) {
    $t = ($Text -replace '[\x00-\x08\x0B\x0C\x0E-\x1F]', '')
    if ($t.Length -gt $Max) { $t = $t.Substring(0, $Max) + ' ...' }
    return $t
}

function ConvertFrom-Marker([string]$Line) {
    # A marker line is "@@htt " and one JSON object with a "kind". Anything
    # else, including a broken marker, is $null (shown as an ordinary line).
    if ($null -eq $Line -or $Line.Length -gt 100000) { return $null }
    $l = $Line.TrimStart([char]0xFEFF)
    if (-not $l.StartsWith('@@htt ')) { return $null }
    try { $o = ConvertFrom-Json -InputObject $l.Substring(6) -ErrorAction Stop } catch { return $null }
    if ($null -eq $o -or -not ($o -is [Management.Automation.PSCustomObject])) { return $null }
    if (-not $o.PSObject.Properties['kind'] -or -not ($o.kind -is [string])) { return $null }
    return $o
}

# -- the engine as a child process ----------------------------------------------------------------
# Output is read with ReadLineAsync and collected by a DispatcherTimer on the
# window's own thread (the same way as in the setup window).

function Start-Engine([string[]]$EngineArgs, [string]$Kind) {
    $all = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $script:Engine) + $EngineArgs
    $script:EngineCalls.Add([pscustomobject]@{ Kind = $Kind; Args = $EngineArgs })
    if ($script:Harness -eq 'selftest' -or $script:Harness -eq 'shots') { return $true }
    $psi = New-Object Diagnostics.ProcessStartInfo
    $psi.FileName = $script:PsExe
    $psi.Arguments = (($all | ForEach-Object { Quote-Arg $_ }) -join ' ')
    $psi.UseShellExecute = $false
    $psi.CreateNoWindow = $true
    $psi.RedirectStandardOutput = $true
    $psi.RedirectStandardError = $true
    $psi.RedirectStandardInput = $true
    $utf8 = New-Object Text.UTF8Encoding($false)
    $psi.StandardOutputEncoding = $utf8
    $psi.StandardErrorEncoding = $utf8
    # not the folder of this script: it may be the temporary copy the window deletes
    $psi.WorkingDirectory = [IO.Path]::GetTempPath()
    Add-LineReader
    $p = [Diagnostics.Process]::Start($psi)
    try { $p.StandardInput.Close() } catch { }      # the engine never reads input
    $rd0 = New-Object HttLfReader($p.StandardOutput)
    $rd1 = New-Object HttLfReader($p.StandardError)
    $script:Run = @{
        Proc = $p; Kind = $Kind; Exited = $null
        Readers = @($rd0, $rd1)
        Tasks = @($rd0.ReadLineAsync(), $rd1.ReadLineAsync())
        Done = @($false, $false)
    }
    $script:Timer.Start()
    return $true
}

function Invoke-EngineTick {
    $r = $script:Run
    if ($r) {
        for ($i = 0; $i -lt 2; $i++) {
            $n = 0
            while (-not $r.Done[$i] -and $r.Tasks[$i].IsCompleted -and $n -lt 400) {
                $n++
                $line = $null
                try { $line = $r.Tasks[$i].Result } catch { $line = $null }
                if ($null -eq $line) { $r.Done[$i] = $true; break }
                # Reader 0 is standard output (markers count); 1 is standard error (log text only).
                Receive-RawLine $line $r.Kind (($i -eq 0) -and -not $r.Readers[$i].Untrusted)
                $r.Tasks[$i] = $r.Readers[$i].ReadLineAsync()
            }
        }
        $finished = $r.Done[0] -and $r.Done[1]
        if (-not $finished -and $r.Proc.HasExited) {
            # A grandchild can keep the pipes open: give it 3 s after the engine itself has gone.
            if (-not $r.Exited) { $r.Exited = Get-Date }
            elseif (((Get-Date) - $r.Exited).TotalSeconds -gt 3) { $finished = $true }
        }
        if ($finished) {
            $r.Proc.WaitForExit()
            $code = $r.Proc.ExitCode
            $script:Run = $null
            if ($r.Kind -eq 'progress') { Complete-Remove $code } else { Complete-Check $code }
        }
    }
    Flush-Log
    if (-not $script:Run -and -not $script:AutoTimerNeeded) { $script:Timer.Stop() }
}

function Stop-EngineTree {
    # Only used when the window is closed by a test (a person cannot close it
    # while the removal runs): the engine and what it started.
    $r = $script:Run
    if (-not $r) { return }
    try {
        $psi = New-Object Diagnostics.ProcessStartInfo
        $psi.FileName = Join-Path $env:SystemRoot 'System32\taskkill.exe'
        $psi.Arguments = "/PID $($r.Proc.Id) /T /F"
        $psi.UseShellExecute = $false
        $psi.CreateNoWindow = $true
        $psi.RedirectStandardOutput = $true
        $psi.RedirectStandardError = $true
        $k = [Diagnostics.Process]::Start($psi)
        [void]$k.WaitForExit(10000)
    } catch { }
    try { if (-not $r.Proc.HasExited) { $r.Proc.Kill() } } catch { }
}

function Receive-RawLine([string]$Line, [string]$Kind, [bool]$Trusted) {
    # One line as the reader returned it (only a line feed ends a line). A line that
    # holds a lone carriage return (a progress bar, or a program trying to forge a
    # marker with "...\r@@htt {...}") is split there and its pieces are plain log
    # text only: no marker is taken from it.
    if ($Line.IndexOf([char]13) -ge 0) {
        foreach ($piece in $Line.Split([char]13)) {
            if ($piece -ne '') { try { Receive-EngineLine $piece $Kind $false } catch { Add-Log $piece } }
        }
    } else {
        try { Receive-EngineLine $Line $Kind $Trusted } catch { Add-Log $Line }
    }
}

function Receive-EngineLine([string]$Line, [string]$Kind, [bool]$Trusted = $true) {
    # A marker is believed only on standard output; on standard error the
    # same text is just a line of the log.
    if ($Kind -eq 'check') {
        $script:CheckLog.Add($Line)
        if ($script:CheckLog.Count -gt 500) { $script:CheckLog.RemoveRange(0, $script:CheckLog.Count - 500) }
        if (-not $Trusted) { return }
        $script:CheckOut.Add($Line)
        if ($script:CheckOut.Count -gt 500) { $script:CheckOut.RemoveRange(0, $script:CheckOut.Count - 500) }
        $m = ConvertFrom-Marker $Line
        if ($m -and $m.kind -eq 'facts') { $script:PendingFacts = $m }
        return
    }
    Receive-ProgressLine $Line $Trusted
}

# -- the log (Show details) ---------------------------------------------------------------------

function Add-Log([string]$Line) {
    if ($null -eq $Line) { $Line = '' }
    if ($Line.Length -gt 2000) { $Line = $Line.Substring(0, 2000) + ' ...' }
    $script:FullLog.Add($Line)
    if ($script:FullLog.Count -gt $script:MaxLogLines) { $script:FullLog.RemoveRange(0, $script:FullLog.Count - $script:MaxLogLines) }
    $script:LogPending.Add($Line)
    if ($script:LogPending.Count -gt $script:MaxLogLines) { $script:LogPending.RemoveRange(0, $script:LogPending.Count - $script:MaxLogLines) }
}

function Get-LogText { return ($script:FullLog -join "`r`n") }

function Flush-Log {
    if ($script:LogPending.Count -eq 0) { return }
    $text = ($script:LogPending -join "`n") + "`n"
    $script:LogPending.Clear()
    $ui.LogBox.AppendText($text)
    if ($ui.LogBox.LineCount -gt 6000) {
        $all = $ui.LogBox.Text
        $ui.LogBox.Text = $all.Substring([Math]::Max(0, $all.Length - 200000))
    }
    if ($ui.DetailsExp.IsExpanded) { $ui.LogBox.ScrollToEnd() }
}

# -- pages and the button row -------------------------------------------------------------------

$PageNames = @{ ask = 'PageAsk'; progress = 'PageProgress'; result = 'PageResult'; error = 'PageError' }
$PageRail = @{ ask = 1; progress = 2; result = 3 }

function Show-Page([string]$Name) {
    foreach ($k in $PageNames.Keys) { Set-Shown $ui[$PageNames[$k]] ($k -eq $Name) }
    $script:Page = $Name
    Sync-Rail
    if ($Name -eq 'progress') { $ui.ProgressScroll.ScrollToTop() } elseif ($Name -eq 'ask') { $ui.AskScroll.ScrollToTop() } else { $ui[$PageNames[$Name]].ScrollToTop() }
    Update-Nav
    if ($script:Harness -ne 'selftest') {
        $null = $ui.Root.Dispatcher.BeginInvoke([Action] {
            try {
                if ($ui.NextBtn.IsVisible -and $ui.NextBtn.IsEnabled -and $script:Page -ne 'ask') { [void]$ui.NextBtn.Focus() }
                elseif ($ui.CancelBtn.IsVisible) { [void]$ui.CancelBtn.Focus() }
            } catch { }
        }, [Windows.Threading.DispatcherPriority]::Input)
    }
}

function Sync-Rail {
    if ($PageRail.ContainsKey($script:Page)) {
        if ($script:Page -eq 'result' -and $script:ResultKind -and $script:ResultKind -ne 'success') { Update-Rail 2 'failure' }
        else { Update-Rail $PageRail[$script:Page] }
    }
}

function Update-Rail([int]$Current, [string]$Problem = '') {
    # $Problem 'failure': step $Current did not finish and shows a "!" instead
    # of a tick; later steps stay pending.
    for ($i = 1; $i -le 3; $i++) {
        $dot = $ui["RailDot$i"]; $num = $ui["RailNum$i"]; $txt = $ui["RailText$i"]
        if ($i -eq $Current -and $Problem) {
            Set-Res $dot ([Windows.Controls.Border]::BackgroundProperty) 'BadSoft'
            Set-Res $dot ([Windows.Controls.Border]::BorderBrushProperty) 'Bad'
            $num.Text = '!'
            $num.ClearValue([Windows.Controls.TextBlock]::FontFamilyProperty)
            $num.FontSize = 13
            $num.FontWeight = 'Bold'
            Set-Res $num ([Windows.Controls.TextBlock]::ForegroundProperty) 'Bad'
            Set-Res $txt ([Windows.Controls.TextBlock]::ForegroundProperty) 'Text'
            $txt.FontWeight = 'SemiBold'
        } elseif ($i -lt $Current) {
            Set-Res $dot ([Windows.Controls.Border]::BackgroundProperty) 'Brand'
            Set-Res $dot ([Windows.Controls.Border]::BorderBrushProperty) 'Brand'
            $num.Text = [string][char]0xE73E
            Set-Res $num ([Windows.Controls.TextBlock]::FontFamilyProperty) 'Icons'
            $num.FontSize = 11
            Set-Res $num ([Windows.Controls.TextBlock]::ForegroundProperty) 'OnBrand'
            Set-Res $txt ([Windows.Controls.TextBlock]::ForegroundProperty) 'Text2'
            $txt.FontWeight = 'Normal'
        } elseif ($i -eq $Current) {
            Set-Res $dot ([Windows.Controls.Border]::BackgroundProperty) 'BrandSoft'
            Set-Res $dot ([Windows.Controls.Border]::BorderBrushProperty) 'Brand'
            $num.Text = [string]$i
            $num.ClearValue([Windows.Controls.TextBlock]::FontFamilyProperty)
            $num.FontSize = 12
            $num.FontWeight = 'SemiBold'
            Set-Res $num ([Windows.Controls.TextBlock]::ForegroundProperty) 'Brand'
            Set-Res $txt ([Windows.Controls.TextBlock]::ForegroundProperty) 'Text'
            $txt.FontWeight = 'SemiBold'
        } else {
            Set-Res $dot ([Windows.Controls.Border]::BackgroundProperty) 'Side'
            Set-Res $dot ([Windows.Controls.Border]::BorderBrushProperty) 'Line2'
            $num.Text = [string]$i
            $num.ClearValue([Windows.Controls.TextBlock]::FontFamilyProperty)
            $num.FontSize = 12
            $num.FontWeight = 'Normal'
            Set-Res $num ([Windows.Controls.TextBlock]::ForegroundProperty) 'Text3'
            Set-Res $txt ([Windows.Controls.TextBlock]::ForegroundProperty) 'Text3'
            $txt.FontWeight = 'Normal'
        }
        if ($i -eq $Current -and $Problem) { [Windows.Automation.AutomationProperties]::SetName($txt, "Step $i of 3, failed: $($RailLabels[$i - 1])") }
        elseif ($i -eq $Current) { [Windows.Automation.AutomationProperties]::SetName($txt, "Step $i of 3, current: $($RailLabels[$i - 1])") }
        else { [Windows.Automation.AutomationProperties]::SetName($txt, "Step $i of 3: $($RailLabels[$i - 1])") }
    }
}

function Test-CanRemove {
    if (-not $script:Facts -or $script:Checking -or $script:Run) { return $false }
    return ($script:Page -eq 'ask')
}

function Update-Nav {
    $b = $ui
    $b.CopyBtn.Visibility = 'Collapsed'
    $b.CancelBtn.Visibility = 'Visible'
    $b.CancelBtn.Content = 'Cancel'
    $b.NextBtn.Visibility = 'Visible'
    $b.NextBtn.IsEnabled = $true
    switch ($script:Page) {
        'ask' {
            $b.NextBtn.Content = 'Remove'
            $b.NextBtn.IsEnabled = Test-CanRemove
        }
        'progress' {
            # No Cancel and no Close while files are being removed: stopping half way would leave a broken install.
            $b.CancelBtn.Visibility = 'Collapsed'
            $b.NextBtn.Visibility = 'Collapsed'
        }
        'result' {
            $b.CancelBtn.Visibility = 'Collapsed'
            $b.NextBtn.Content = 'Close'
            if ($script:ResultKind -ne 'success') { $b.CopyBtn.Visibility = 'Visible'; $b.CopyBtn.Content = 'Copy details' }
        }
        'error' {
            $b.CancelBtn.Visibility = 'Collapsed'
            $b.NextBtn.Content = 'Close'
            if ($ui.ErrDetailsPanel.Visibility -eq 'Visible') { $b.CopyBtn.Visibility = 'Visible'; $b.CopyBtn.Content = 'Copy details' }
        }
    }
}

function Show-ErrorPage([string]$Title, [string]$Text, [string]$Details) {
    $ui.ErrTitle.Text = $Title
    $ui.ErrText.Text = $Text
    $ui.ErrDetails.Text = [string]$Details
    Set-Shown $ui.ErrDetailsPanel (-not [string]::IsNullOrWhiteSpace($Details))
    if ($null -eq $script:Outcome) { $script:Outcome = 1 }
    foreach ($k in $PageNames.Keys) { Set-Shown $ui[$PageNames[$k]] ($k -eq 'error') }
    $script:Page = 'error'
    # Both callers stop before anything was removed: step 1 (Confirm) is the one that failed.
    Update-Rail 1 'failure'
    Update-Nav
}

# -- -Check and the facts ----------------------------------------------------------------------------

function Start-Check {
    $script:Checking = $true
    $script:CheckLog.Clear()
    $script:CheckOut.Clear()
    $script:PendingFacts = $null
    Set-Shown $ui.CheckingRow $true
    Update-Nav
    try {
        [void](Start-Engine (@('-Check') + (Get-PassThroughArgs)) 'check')
    } catch {
        $script:CheckLog.Add($_.Exception.Message)
        Complete-Check -1
    }
}

function Complete-Check([int]$Code) {
    $script:Checking = $false
    Set-Shown $ui.CheckingRow $false
    $facts = $script:PendingFacts
    if ($Code -ne 0 -or $null -eq $facts) {
        $details = ($script:CheckLog | Select-Object -Last 40) -join "`r`n"
        $why = 'Nothing on this PC was changed.'
        foreach ($l in $script:CheckOut) {          # standard output only
            $m = ConvertFrom-Marker $l
            if ($m -and $m.kind -eq 'error' -and $m.text) { $why = (Limit-Text ([string]$m.text) 4000) -replace "`r?`n", "`r`n"; $why += "`r`n`r`nNothing on this PC was changed." }
        }
        if (-not (Test-Path -LiteralPath $script:Engine)) {
            $why = 'The file uninstall.ps1 is missing next to the uninstaller. Nothing on this PC was changed. You can remove Hold to talk by deleting its folder and its shortcuts by hand.'
        }
        Show-ErrorPage 'The uninstaller could not check this PC' $why $details
        return
    }
    if ($script:Harness -eq 'autorun' -and $facts.testMode -ne $true) {
        Show-ErrorPage 'Refused' 'The automatic test run only works in test mode.' $null
        return
    }
    Set-Facts $facts
    Update-Nav
}

function Set-Facts($Facts) {
    $script:Facts = $Facts
    $f = $Facts
    $ver = [string]$f.version
    if ($ver) { $ui.RailVersion.Text = "Version $ver" } else { $ui.RailVersion.Text = '' }
    Set-Shown $ui.TestBadge ($f.testMode -eq $true)
    $L = $f.layout
    $ui.ProgramPath.Text = [string]$L.installRoot
    $ui.EnvPath.Text = [string]$L.envDir
    $ui.DataPath.Text = [string]$L.dataDir
    Set-Shown $ui.NotFoundCard ($f.installed -ne $true)
    $has = ($f.dataExists -eq $true)
    Set-Shown $ui.DataBox $has
    Set-Shown $ui.DataBoxRow $has
    Set-Shown $ui.DataPath $has
    if ($has) { $ui.DataWhat.Text = 'Kept, unless you tick the box below. Where they are:' }
    else { $ui.DataWhat.Text = 'There is no data folder on this PC at the moment, so there is nothing to keep or delete.' }
    $ui.DataBox.IsChecked = $false
    $script:DataConfirmed = $false
    Update-Nav
}

# -- removing -------------------------------------------------------------------------------------------

function Get-RemoveArgs {
    # The answer about the data is 'yes' only when the box is ticked AND the confirmation was given.
    $yes = ($ui.DataBox.IsChecked -eq $true) -and $script:DataConfirmed
    $a = @('-Progress', '-DeleteUserData', $(if ($yes) { 'yes' } else { 'no' }))
    $a += Get-PassThroughArgs
    if ($script:BatDir) { $a += @('-BatDir', [string]$script:BatDir) }
    return , $a
}

function Start-Remove {
    if (-not (Test-CanRemove)) { return $false }
    if ($ui.DataBox.IsChecked -eq $true -and -not $script:DataConfirmed) { Show-DataConfirm; return $false }
    $engineArgs = Get-RemoveArgs
    $script:DataAnswer = $engineArgs[1 + [array]::IndexOf($engineArgs, '-DeleteUserData')]
    Initialize-Progress @($script:Facts.steps)
    Show-Page 'progress'
    Add-Log 'Hold to talk - removing'
    try {
        [void](Start-Engine $engineArgs 'progress')
    } catch {
        $script:ErrorText = "The uninstaller could not be started: $($_.Exception.Message)"
        Complete-Remove -1
        return $false
    }
    return $true
}

function Initialize-Progress([string[]]$Steps) {
    $ui.StepList.Children.Clear()
    $script:StepRows = @()
    $script:CurrentStep = 0
    $script:Warnings = @()
    $script:ErrorText = $null
    $script:DoneInfo = $null
    $ui.LogBox.Clear()
    $script:FullLog.Clear()
    $script:LogPending.Clear()
    $ui.Bar.Value = 0
    $ui.Bar.Tag = $null
    foreach ($s in @($Steps | Select-Object -First $script:MaxSteps)) { Add-StepRow ([string]$s) }
    $script:StepTotal = $script:StepRows.Count
    $ui.StepNow.Text = 'Starting...'
    $ui.StepCount.Text = ''
    for ($k = 0; $k -lt $script:StepRows.Count; $k++) { Set-StepState ($k + 1) 'pending' }
}

function Add-StepRow([string]$Title) {
    $row = [Windows.Markup.XamlReader]::Parse($script:StepRowXaml)
    $o = [pscustomobject]@{
        Root = $row; Title = $row.FindName('Title'); Act = $row.FindName('Act'); Last = $row.FindName('Last')
        Ring = $row.FindName('Ring'); Fill = $row.FindName('Fill'); Tick = $row.FindName('Tick'); Spin = $row.FindName('Spin')
        State = 'pending'
    }
    $o.Title.Text = $Title
    [void]$ui.StepList.Children.Add($row)
    $script:StepRows += $o
}

function Set-StepState([int]$N, [string]$State) {
    $o = $script:StepRows[$N - 1]
    $o.State = $State
    Set-Shown $o.Ring ($State -eq 'pending')
    Set-Shown $o.Fill ($State -eq 'done')
    Set-Shown $o.Tick ($State -eq 'done')
    Set-Shown $o.Spin ($State -eq 'current')
    if ($State -eq 'current') {
        Set-Res $o.Title ([Windows.Controls.TextBlock]::ForegroundProperty) 'Text'
        $o.Title.FontWeight = 'SemiBold'
    } elseif ($State -eq 'done') {
        Set-Res $o.Title ([Windows.Controls.TextBlock]::ForegroundProperty) 'Text2'
        $o.Title.FontWeight = 'Normal'
        Set-Shown $o.Act $false; Set-Shown $o.Last $false
    } else {
        Set-Res $o.Title ([Windows.Controls.TextBlock]::ForegroundProperty) 'Text3'
        $o.Title.FontWeight = 'Normal'
        Set-Shown $o.Act $false; Set-Shown $o.Last $false
    }
    [Windows.Automation.AutomationProperties]::SetName($o.Root, "$($o.Title.Text): $State")
}

function Set-Step([int]$N, [string]$Title) {
    $Total = $script:StepTotal
    if ($N -lt 1 -or $N -gt $Total -or $script:StepRows.Count -lt $Total) { return }
    for ($k = 1; $k -lt $N; $k++) { if ($script:StepRows[$k - 1].State -ne 'done') { Set-StepState $k 'done' } }
    Set-StepState $N 'current'
    $script:CurrentStep = $N
    $ui.Bar.Value = [double]($N - 1) / [double]$Total
    $ui.Bar.Tag = 'busy'
    $ui.StepNow.Text = $script:StepRows[$N - 1].Title.Text
    $ui.StepCount.Text = "Step $N of $Total"
}

function Set-StepText([string]$Which, [string]$Text) {
    if ($script:CurrentStep -lt 1) { return }
    $o = $script:StepRows[$script:CurrentStep - 1]
    $t = ($Text -replace '\s+', ' ').Trim()
    if ($t.Length -gt 240) { $t = $t.Substring(0, 240) }
    if ($t -eq '') { return }
    $o.$Which.ToolTip = $t
    if ($Which -eq 'Last' -and $t.Length -gt 58) { $t = $t.Substring(0, 32).TrimEnd() + ' ... ' + $t.Substring($t.Length - 21).TrimStart() }
    $o.$Which.Text = $t
    Set-Shown $o.$Which $true
}

function Receive-ProgressLine([string]$Line, [bool]$Trusted = $true) {
    $m = $null
    if ($Trusted) { $m = ConvertFrom-Marker $Line }     # standard error is never a marker
    if ($null -eq $m) {
        Add-Log $Line
        if ($Line.Trim()) { Set-StepText 'Last' $Line }
        return
    }
    try {
        switch ($m.kind) {
            'step' {
                # n must be a whole number from 1 to the number of step titles in the facts.
                $n = $m.n
                if (($n -is [int] -or $n -is [long]) -and $n -ge 1 -and $n -le $script:StepTotal) {
                    Add-Log ''
                    Add-Log ("[$n/$($script:StepTotal)] $($script:StepRows[[int]$n - 1].Title.Text)")
                    Set-Step ([int]$n) ''
                } else { Add-Log $Line }
            }
            'warning' {
                $t = Limit-Text ([string]$m.text) 1000
                Add-Log ('Warning: ' + $t)
                if ($t -and $script:Warnings.Count -lt 20) { $script:Warnings += $t }
            }
            'error' {
                $t = Limit-Text ([string]$m.text) 4000
                Add-Log ('Error: ' + $t)
                $script:ErrorText = $t
            }
            'done' {
                Add-Log 'Finished.'
                $script:DoneInfo = $m
                for ($k = 1; $k -le $script:StepRows.Count; $k++) { Set-StepState $k 'done' }
                $ui.Bar.Value = 1
                $ui.Bar.Tag = $null
            }
            default { Add-Log $Line }
        }
    } catch {
        Add-Log $Line          # a marker with odd values: shown, never fatal
    }
}

function Complete-Remove([int]$Code) {
    $ui.Bar.Tag = $null
    if ($Code -eq 0 -and $script:DoneInfo) { Show-Result 'success' $Code } else { Show-Result 'failure' $Code }
}

function Set-Badge([int]$Glyph, [string]$Fg, [string]$Bg) {
    $ui.ResultGlyph.Text = [string][char]$Glyph
    Set-Res $ui.ResultGlyph ([Windows.Controls.TextBlock]::ForegroundProperty) $Fg
    Set-Res $ui.ResultBadge ([Windows.Controls.Border]::BackgroundProperty) $Bg
}

function Add-WarnCard([string]$Text) {
    $card = [Windows.Markup.XamlReader]::Parse(@'
<Border xmlns="http://schemas.microsoft.com/winfx/2006/xaml/presentation" Background="{DynamicResource WarnSoft}" BorderBrush="{DynamicResource WarnLine}"
        BorderThickness="1" CornerRadius="12" Padding="16,12" Margin="0,16,0,0">
  <Grid>
    <Grid.ColumnDefinitions><ColumnDefinition Width="28"/><ColumnDefinition Width="*"/></Grid.ColumnDefinitions>
    <TextBlock Text="&#xE7BA;" FontFamily="Segoe Fluent Icons, Segoe MDL2 Assets" FontSize="16" Foreground="{DynamicResource Warn}" Margin="0,2,0,0"/>
    <TextBlock Grid.Column="1" TextWrapping="Wrap" LineHeight="21"/>
  </Grid>
</Border>
'@)
    $card.Child.Children[1].Text = $Text
    [void]$ui.WarnList.Children.Add($card)
}

function Show-Result([string]$Kind, [int]$Code) {
    $script:ResultKind = $Kind
    $f = $script:Facts
    $ui.WarnList.Children.Clear()
    if ($Kind -eq 'success') {
        $script:Outcome = 0
        $d = $script:DoneInfo
        Set-Badge 0xE73E 'Ok' 'OkSoft'
        $ui.ResultTitle.Text = 'Hold to talk has been removed'
        $ui.ResultLead.Text = 'The app, its shortcuts and its private Python copy are gone from this Windows account.'
        foreach ($w in $script:Warnings) { Add-WarnCard $w }
        $dir = [string]$f.layout.dataDir                      # from the facts, not from a marker
        $ui.DataResultPath.Text = $dir
        $deleted = ($d.dataDeleted -eq $true)
        if ($deleted) {
            $ui.DataResultTitle.Text = 'Your settings, history and logs were deleted'
            $left = @($d.leftInPlace | ForEach-Object { Limit-Text ([string]$_) 200 } | Select-Object -First 20)
            if ($left.Count) {
                $names = @($left | ForEach-Object { ([string]$_).TrimEnd([char]92, [char]47) } | Where-Object { $_ -ne '' })
                $ui.DataResultText.Text = 'These were not made by the app, so they were not deleted: ' + ($names -join ', ') + '.' + [Environment]::NewLine + 'They are in:'
                Set-Shown $ui.DataResultPath $true
            } else {
                $ui.DataResultText.Text = 'They are gone for good.'
                Set-Shown $ui.DataResultPath $false
            }
        } elseif ($script:DataAnswer -eq 'yes') {
            $ui.DataResultTitle.Text = 'Your settings, history and logs'
            $ui.DataResultText.Text = 'There were none to delete.'
            Set-Shown $ui.DataResultPath $false
        } elseif ($f.dataExists -eq $true) {
            $ui.DataResultTitle.Text = 'Your settings, history and logs were kept'
            $ui.DataResultText.Text = 'They are still here. Delete that folder yourself if you never want them back:'
            Set-Shown $ui.DataResultPath $true
        } else {
            $ui.DataResultTitle.Text = 'Your settings, history and logs'
            $ui.DataResultText.Text = 'There was no data folder on this PC.'
            Set-Shown $ui.DataResultPath $false
        }
        $cache = [string]$f.modelCache
        if ($d -and $d.modelCache) { $cache = [string]$d.modelCache }
        $ui.ModelPath.Text = Limit-Text $cache 500
        $models = @()
        if ($d -and $d.models) { $models = @($d.models) } elseif ($f.models) { $models = @($f.models) }
        $models = @($models | ForEach-Object { Limit-Text ([string]$_) 200 } | Select-Object -First 12)
        if ($models.Count) { $ui.ModelList.Text = 'Found there: ' + ($models -join ', '); Set-Shown $ui.ModelList $true }
        else { Set-Shown $ui.ModelList $false }
        Set-Shown $ui.SuccessPanel $true
        Set-Shown $ui.FailPanel $false
    } else {
        $script:Outcome = 1
        Set-Badge 0xE783 'Bad' 'BadSoft'
        $ui.ResultTitle.Text = 'Hold to talk could not be removed completely'
        $ui.ResultLead.Text = 'Part of it may still be on this PC. Close the app if it is running (its icon in the notification area, Quit) and run the uninstaller again; it carries on where it stopped.'
        $msg = $script:ErrorText
        if (-not $msg) {
            $tail = @($script:FullLog | Where-Object { $_.Trim() } | Select-Object -Last 8)
            $msg = "The uninstaller stopped unexpectedly (exit code $Code)."
            if ($tail.Count) { $msg += "`n`nThe last lines it printed:`n" + ($tail -join "`n") }
        }
        $ui.ErrorBox.Text = ($msg -replace "`r?`n", "`r`n")
        Set-Shown $ui.SuccessPanel $false
        Set-Shown $ui.FailPanel $true
    }
    Show-Page 'result'
}

# -- the confirmation and closing ---------------------------------------------------------------------------

function Show-DataConfirm {
    if ($ui.Overlay.Visibility -eq 'Visible') { return }
    $ui.Overlay.Visibility = 'Visible'
    $ui.Main.IsEnabled = $false
    if ($script:Harness -ne 'selftest') {
        $null = $ui.Root.Dispatcher.BeginInvoke([Action] { try { [void]$ui.KeepBtn.Focus() } catch { } }, [Windows.Threading.DispatcherPriority]::Input)
    }
}

function Hide-Confirm {
    $ui.Overlay.Visibility = 'Collapsed'
    $ui.Main.IsEnabled = $true
}

function Confirm-DeleteData {
    # "Delete": the box stays ticked and the answer counts.
    $script:DataConfirmed = $true
    Hide-Confirm
    Update-Nav
}

function Confirm-KeepData {
    # "Keep" (and Esc): the box is unticked again.
    $script:DataConfirmed = $false
    $ui.DataBox.IsChecked = $false
    Hide-Confirm
    Update-Nav
}

function Set-DataBoxForTest([bool]$On) {
    # The ONLY place where the program itself may tick the box that deletes the
    # person's data: the self-test, the captures and the -AutoRun test switch,
    # which refuses to run without all five test overrides in the sandbox.
    if ($script:Harness -ne 'selftest' -and $script:Harness -ne 'shots' -and
        -not ($script:Harness -eq 'autorun' -and $script:OverridesComplete)) {
        throw 'Refused: the box that deletes your data is ticked only by a person.'
    }
    $ui.DataBox.IsChecked = $On
}

function Get-CancelCode {
    if ($null -ne $script:Outcome) { return [int]$script:Outcome }
    return 3
}

function Close-Window([int]$Code) {
    $script:ExitCode = $Code
    if ($script:Harness -eq 'selftest') { $script:ClosedWith = $Code; return }
    if ($script:Run) { Stop-EngineTree; $script:Run = $null }
    $script:AllowClose = $true
    $script:Window.Close()
}

function Invoke-Cancel {
    # The Cancel button and Esc.
    if ($ui.Overlay.Visibility -eq 'Visible') { Confirm-KeepData; return }
    switch ($script:Page) {
        'progress' { return }                      # not while files are being removed
        default { Close-Window (Get-CancelCode) }
    }
}

function On-Closing($e) {
    if ($script:AllowClose) { return }
    if ($script:Page -eq 'progress' -and $script:Run) { $e.Cancel = $true; return }
    if ($script:Run) { Stop-EngineTree; $script:Run = $null }
    if ($null -eq $script:ExitCode) { $script:ExitCode = Get-CancelCode }
}

function Invoke-Next {
    switch ($script:Page) {
        'ask' { [void](Start-Remove) }
        'result' { Close-Window ([int]$script:Outcome) }
        'error' { Close-Window (Get-CancelCode) }
    }
}

function Copy-Details {
    $text = Get-LogText
    if ($script:Page -eq 'error') { $text = $ui.ErrDetails.Text }
    if ($script:Page -eq 'result' -and $script:ErrorText) { $text = "Error: $($script:ErrorText)`r`n`r`n$text" }
    if ($script:Harness) { $script:Copied = $text; $ui.CopyBtn.Content = 'Copied'; return }
    try { [Windows.Clipboard]::SetText($text); $ui.CopyBtn.Content = 'Copied' } catch { $ui.CopyBtn.Content = 'Could not copy' }
}

# -- theme -------------------------------------------------------------------------------------------------

function Get-WindowsTheme {
    try {
        $v = Get-ItemPropertyValue -Path 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Themes\Personalize' -Name AppsUseLightTheme -ErrorAction Stop
        if ($v -eq 0) { return 'dark' }
    } catch { }
    return 'light'
}

function Set-Theme([string]$Mode) {
    $p = $Palettes[$Mode]
    foreach ($k in $p.Keys) {
        $c = [Windows.Media.Color][Windows.Media.ColorConverter]::ConvertFromString($p[$k])
        $b = [Windows.Media.SolidColorBrush]::new($c)
        $b.Freeze()
        # (the base object: a PowerShell wrapper is not a valid brush for WPF)
        $script:Window.Resources[$k] = $b.PSObject.BaseObject
    }
    $script:ThemeMode = $Mode
    Set-TitleBar
}

function Set-TitleBar {
    # A dark title bar in dark mode, and (Windows 11) a caption in the window's
    # own background colour. Ignored where Windows does not allow it.
    if (-not $script:Hwnd -or -not ('HttUninstallNative' -as [type])) { return }
    try {
        $dark = 0; if ($script:ThemeMode -eq 'dark') { $dark = 1 }
        [void][HttUninstallNative]::SetInt($script:Hwnd, 20, $dark)
        [void][HttUninstallNative]::SetInt($script:Hwnd, 19, $dark)
        $p = $Palettes[$script:ThemeMode]
        foreach ($pair in @(@(35, $p.Bg), @(36, $p.Text))) {
            $c = [Windows.Media.ColorConverter]::ConvertFromString($pair[1])
            $ref = [int]$c.R + ([int]$c.G -shl 8) + ([int]$c.B -shl 16)
            [void][HttUninstallNative]::SetInt($script:Hwnd, $pair[0], $ref)
        }
    } catch { }
}

function Add-LineReader {
    # Line reading for the engine's output: only a line feed ends a line (see HttLfReader).
    if ('HttLfReader' -as [type]) { return }
    Add-Type -TypeDefinition @'
using System;
using System.IO;
using System.Text;
using System.Threading.Tasks;
// Reads lines where only a line feed ends a line (a lone carriage return does not,
// unlike StreamReader.ReadLine). A carriage return just before the line feed is dropped.
public class HttLfReader {
    readonly TextReader r;
    readonly StringBuilder sb = new StringBuilder();
    readonly char[] buf = new char[4096];
    bool eof;
    readonly int limit;
    bool carry;
    // True when the line just returned must not be believed as a marker: it was cut
    // because the buffer limit was reached without a line feed, or it is the line
    // right after such a cut (its start may be the tail of the same long line).
    public bool Untrusted { get; private set; }
    public HttLfReader(TextReader reader) : this(reader, 1000000) { }
    public HttLfReader(TextReader reader, int maxLine) { r = reader; limit = maxLine; }
    public Task<string> ReadLineAsync() { return Core(); }
    static string Trim(string s) { return (s.Length > 0 && s[s.Length - 1] == '\r') ? s.Substring(0, s.Length - 1) : s; }
    async Task<string> Core() {
        int from = 0;
        while (true) {
            for (int i = from; i < sb.Length; i++) {
                if (sb[i] == '\n') {
                    string line = sb.ToString(0, i);
                    sb.Remove(0, i + 1);
                    Untrusted = carry; carry = false;
                    return Trim(line);
                }
            }
            from = sb.Length;
            if (sb.Length > limit) { string big = sb.ToString(); sb.Clear(); Untrusted = true; carry = true; return big; }
            if (eof) {
                if (sb.Length == 0) return null;
                string last = sb.ToString(); sb.Clear(); Untrusted = carry; carry = false; return Trim(last);
            }
            int n = await r.ReadAsync(buf, 0, buf.Length);
            if (n <= 0) eof = true; else sb.Append(buf, 0, n);
        }
    }
}
'@
}

function Add-Native {
    if ('HttUninstallNative' -as [type]) { return }
    try {
        Add-Type -TypeDefinition @'
using System;
using System.Text;
using System.Runtime.InteropServices;
public static class HttUninstallNative {
    [DllImport("dwmapi.dll")]
    static extern int DwmSetWindowAttribute(IntPtr hwnd, int attr, ref int value, int size);
    public static int SetInt(IntPtr hwnd, int attr, int value) { return DwmSetWindowAttribute(hwnd, attr, ref value, 4); }
    [DllImport("user32.dll")]
    public static extern bool IsProcessDPIAware();
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, EntryPoint = "GetLongPathNameW")]
    static extern int GetLongPathName(string path, StringBuilder buf, int size);
    public static string LongPath(string path) {
        StringBuilder sb = new StringBuilder(1024);
        int n = GetLongPathName(path, sb, sb.Capacity);
        if (n > 0 && n < sb.Capacity) return sb.ToString();
        return path;
    }
}
'@
    } catch { }
}

# -- pictures for review (-Shots) -----------------------------------------------------------------------------

function Get-CannedFacts([hashtable]$Opt) {
    # Made-up facts with example paths (never this PC's).
    if (-not $Opt) { $Opt = @{} }
    $u = 'C:\Users\you\AppData'
    $f = [ordered]@{
        kind = 'facts'; appName = 'Hold to talk'; version = '0.9.1'; testMode = [bool]$Opt.TestMode
        layout = [ordered]@{ installRoot = "$u\Local\Programs\HoldToTalk"; envDir = "$u\Local\HoldToTalk\venv"; dataDir = "$u\Local\HoldToTalk\data"
            startMenuLnk = "$u\Roaming\Microsoft\Windows\Start Menu\Programs\Hold to talk.lnk"; desktopLnk = 'C:\Users\you\Desktop\Hold to talk.lnk' }
        installed = (-not $Opt.NotInstalled)
        dataExists = (-not $Opt.NoData)
        modelCache = 'C:\Users\you\.cache\huggingface\hub'
        models = @('models--Systran--faster-whisper-small', 'models--mobiuslabsgmbh--faster-whisper-large-v3-turbo')
        steps = @('Quitting the app', 'Start with Windows entry', 'Shortcuts and the Installed apps entry',
                  'Python environment and window profile', 'Your settings and history', 'Program files')
    }
    return (ConvertTo-Json -InputObject $f -Depth 6 -Compress | ConvertFrom-Json)
}

function Reset-Ui {
    Hide-Confirm
    $script:Outcome = $null; $script:ResultKind = $null; $script:ExitCode = $null; $script:ClosedWith = $null
    $script:Checking = $false; $script:DataConfirmed = $false; $script:DataAnswer = $null
    $ui.DataBox.IsChecked = $false
    $ui.CheckingRow.Visibility = 'Collapsed'
    $ui.DetailsExp.IsExpanded = $false
    $ui.Root.UpdateLayout()
}

function Get-ProgressLines([int]$UpTo) {
    $steps = @($script:Facts.steps)
    $out = @()
    for ($n = 1; $n -le $UpTo; $n++) {
        $out += '@@htt ' + (ConvertTo-Json -Compress -InputObject ([ordered]@{ kind = 'step'; n = $n; total = $steps.Count; title = $steps[$n - 1] }))
        switch ($n) {
            1 { $out += '  Asked the running app to quit.' }
            2 { $out += '  Start with Windows: entry removed (directly).' }
            3 { $out += '  Start menu shortcut: removed.'; $out += '  Desktop shortcut: removed.'; $out += '  Installed apps entry: removed.' }
            4 { $out += '  Python environment: removed.' }
            5 { $out += '  Kept: C:\Users\you\AppData\Local\HoldToTalk\data' }
            6 { $out += '  Program folder: emptied.' }
        }
    }
    return $out
}

function Save-Shot([string]$Path) {
    $ui.Root.UpdateLayout()
    $script:Window.Dispatcher.Invoke([Action] { }, [Windows.Threading.DispatcherPriority]::ContextIdle)
    $w = [int][Math]::Round($ui.Root.ActualWidth); $h = [int][Math]::Round($ui.Root.ActualHeight)
    $bmp = New-Object Windows.Media.Imaging.RenderTargetBitmap($w, $h, 96, 96, [Windows.Media.PixelFormats]::Pbgra32)
    $bmp.Render($ui.Root)
    $enc = New-Object Windows.Media.Imaging.PngBitmapEncoder
    $enc.Frames.Add([Windows.Media.Imaging.BitmapFrame]::Create($bmp))
    $fs = [IO.File]::Create($Path)
    try { $enc.Save($fs) } finally { $fs.Close() }
}

function Invoke-Shots([string]$Folder) {
    New-Item -ItemType Directory -Force -Path $Folder | Out-Null
    $win = $script:Window
    $win.WindowStartupLocation = 'Manual'
    $win.Left = -32000; $win.Top = -32000
    $win.ShowActivated = $false; $win.ShowInTaskbar = $false
    $win.Show()
    $done = '@@htt ' + (ConvertTo-Json -Compress -InputObject ([ordered]@{ kind = 'done'; dataDeleted = $false; leftInPlace = @(); selfRemoved = $true }))
    $doneDel = '@@htt ' + (ConvertTo-Json -Compress -InputObject ([ordered]@{ kind = 'done'; dataDeleted = $true; leftInPlace = @('Photos\', 'notes.docx'); selfRemoved = $true }))
    $states = @(
        @{ Name = '01-ask'; Go = { Set-Facts (Get-CannedFacts @{}); Show-Page 'ask' } },
        @{ Name = '02-ask-box-ticked'; Go = { Set-Facts (Get-CannedFacts @{}); Show-Page 'ask'; $script:DataConfirmed = $true; Set-DataBoxForTest $true } },
        @{ Name = '03-confirm-delete'; Go = { Set-Facts (Get-CannedFacts @{}); Show-Page 'ask'; $script:DataConfirmed = $false; Set-DataBoxForTest $true; Show-DataConfirm } },
        @{ Name = '04-ask-checking'; Go = { $script:Facts = $null; Show-Page 'ask'; Set-Shown $ui.CheckingRow $true; Update-Nav } },
        @{ Name = '05-ask-not-found-no-data'; Go = { Set-Facts (Get-CannedFacts @{ NotInstalled = $true; NoData = $true }); Show-Page 'ask' } },
        @{ Name = '06-removing'; Go = {
                Set-Facts (Get-CannedFacts @{}); Initialize-Progress @($script:Facts.steps); Show-Page 'progress'
                foreach ($l in (Get-ProgressLines 4)) { Receive-ProgressLine $l }
                Flush-Log } },
        @{ Name = '07-removing-details'; Go = {
                Set-Facts (Get-CannedFacts @{}); Initialize-Progress @($script:Facts.steps); Show-Page 'progress'
                foreach ($l in (Get-ProgressLines 4)) { Receive-ProgressLine $l }
                $ui.DetailsExp.IsExpanded = $true; Flush-Log; $ui.LogBox.ScrollToEnd(); $ui.ProgressScroll.UpdateLayout(); $ui.ProgressScroll.ScrollToBottom() } },
        @{ Name = '08-removed-data-kept'; Go = {
                Set-Facts (Get-CannedFacts @{}); $script:DataAnswer = 'no'; Initialize-Progress @($script:Facts.steps)
                foreach ($l in (Get-ProgressLines 6)) { Receive-ProgressLine $l }
                Receive-ProgressLine $done; Complete-Remove 0 }; Bottom = 'PageResult' },
        @{ Name = '09-removed-data-deleted'; Go = {
                Set-Facts (Get-CannedFacts @{}); $script:DataAnswer = 'yes'; Initialize-Progress @($script:Facts.steps)
                foreach ($l in (Get-ProgressLines 6)) { Receive-ProgressLine $l }
                Receive-ProgressLine '@@htt {"kind":"warning","text":"Python environment could not be removed (C:\Users\you\AppData\Local\HoldToTalk\venv). Quit the app and delete it by hand."}'
                Receive-ProgressLine $doneDel; Complete-Remove 0 }; Bottom = 'PageResult' },
        @{ Name = '10-failure'; Go = {
                Set-Facts (Get-CannedFacts @{}); Initialize-Progress @($script:Facts.steps)
                foreach ($l in (Get-ProgressLines 3)) { Receive-ProgressLine $l }
                Receive-ProgressLine '@@htt {"kind":"error","text":"The uninstaller stopped:\nAccess to the path ''C:\\Users\\you\\AppData\\Local\\HoldToTalk\\venv\\Scripts\\python.exe'' is denied."}'
                Complete-Remove 1 } },
        @{ Name = '11-error-page'; Go = { Show-ErrorPage 'The uninstaller could not check this PC' 'The file uninstall.ps1 is missing next to the uninstaller. Nothing on this PC was changed.' "line one`r`nline two" } }
    )
    $made = @()
    foreach ($sz in @(@(780, 580), @(880, 470))) {
        $win.Width = $sz[0]; $win.Height = $sz[1]
        foreach ($mode in 'light', 'dark') {
            Set-Theme $mode
            foreach ($s in $states) {
                Reset-Ui
                & $s.Go
                $name = '{0}x{1}-{2}-uninstall-{3}.png' -f $sz[0], $sz[1], $mode, $s.Name
                Save-Shot (Join-Path $Folder $name)
                $made += $name
                if ($s.Bottom) {
                    $ui.Root.UpdateLayout()
                    $ui[$s.Bottom].ScrollToBottom()
                    $bname = '{0}x{1}-{2}-uninstall-{3}.png' -f $sz[0], $sz[1], $mode, ($s.Name -replace '^(\d+)-', '${1}b-')
                    Save-Shot (Join-Path $Folder $bname)
                    $made += $bname
                }
            }
        }
    }
    [Console]::Out.WriteLine(("saved {0} pictures to {1}" -f $made.Count, $Folder))
    $script:AllowClose = $true
    $win.Close()
}

# -- the self-test -------------------------------------------------------------------------------------------------------

function Invoke-SelfTest {
    $fails = New-Object Collections.Generic.List[string]
    function Check([bool]$Cond, [string]$What) { if (-not $Cond) { $fails.Add($What) } }
    function Press($Button) {
        # Like a click: refused when the button is disabled or hidden.
        if (-not $Button.IsEnabled -or $Button.Visibility -ne 'Visible') { throw "not pressable: $($Button.Name)" }
        $Button.RaiseEvent((New-Object Windows.RoutedEventArgs([Windows.Controls.Primitives.ButtonBase]::ClickEvent)))
    }
    function Pressable($Button) { return ($Button.IsEnabled -and $Button.Visibility -eq 'Visible') }
    function DataArg($a) { $k = [array]::IndexOf($a, '-DeleteUserData'); if ($k -lt 0) { return $null }; return $a[$k + 1] }
    Set-Theme 'light'

    # before the facts arrive nothing can be removed
    Show-Page 'ask'
    Check ($null -eq $script:Facts) 'facts exist at the start'
    Check (-not $ui.NextBtn.IsEnabled) 'Remove is enabled before the facts are known'
    Check ([string]$ui.NextBtn.Content -eq 'Remove') "the button says '$($ui.NextBtn.Content)'"
    Check ((-not (Start-Remove)) -and $script:EngineCalls.Count -eq 0) 'Start-Remove ran before the facts'

    # the facts: paths, and the data box
    Set-Facts (Get-CannedFacts @{})
    Check ($ui.ProgramPath.Text -eq 'C:\Users\you\AppData\Local\Programs\HoldToTalk') 'the program folder is not shown'
    Check ($ui.EnvPath.Text -match 'venv$' -and $ui.DataPath.Text -match 'data$') 'the other folders are not shown'
    Check ($ui.DataBox.IsChecked -ne $true) 'the data box starts ticked'
    Check ($ui.DataBox.Visibility -eq 'Visible') 'the data box is not shown although there is data'
    $m = [regex]::Match((Get-Content -LiteralPath $ScriptPath -Raw), '<CheckBox x:Name="DataBox"[^>]*>')
    Check ($m.Success -and $m.Value -notmatch 'IsChecked') 'the data box has IsChecked in the XAML'
    Check ($ui.DataBoxText.Text -eq 'Also delete my settings, dictation history and logs (this cannot be undone)') 'the data box text changed'
    Check ((Pressable $ui.NextBtn) -and (Pressable $ui.CancelBtn)) 'Remove or Cancel is not usable with facts'
    Check ($ui.Overlay.Visibility -ne 'Visible') 'the confirmation is shown at the start'

    # the default answer is "no", with the other arguments of the contract
    $a = Get-RemoveArgs
    Check ($a[0] -eq '-Progress' -and (DataArg $a) -eq 'no') "the default arguments are: $($a -join ' ')"
    Check ($a -notcontains '-DeleteData' -and $a -notcontains '-Yes') 'the arguments carry a switch of the text mode'
    Press $ui.NextBtn
    Check ($script:EngineCalls.Count -eq 1 -and $script:EngineCalls[0].Kind -eq 'progress' -and (DataArg $script:EngineCalls[0].Args) -eq 'no') 'Remove with the box unticked did not start the engine with -DeleteUserData no'
    Check ($script:Page -eq 'progress' -and $script:DataAnswer -eq 'no') 'the progress page is not shown'
    Check ($ui.CancelBtn.Visibility -ne 'Visible' -and $ui.NextBtn.Visibility -ne 'Visible') 'Cancel or Close is offered while removing'

    # ticking asks "Delete your settings and history for good?"; only Delete lets the answer be yes
    Reset-Ui
    Set-Facts (Get-CannedFacts @{})
    Show-Page 'ask'
    $script:EngineCalls.Clear()
    Set-DataBoxForTest $true
    Check ($ui.Overlay.Visibility -eq 'Visible') 'ticking the box did not ask for a confirmation'
    Check ($ui.ConfirmTitle.Text -eq 'Delete your settings and history for good?') "the question reads '$($ui.ConfirmTitle.Text)'"
    Check ([string]$ui.DeleteBtn.Content -eq 'Delete' -and [string]$ui.KeepBtn.Content -eq 'Keep') 'the confirmation buttons are not Delete / Keep'
    Check ($ui.KeepBtn.Style -eq $ui.Root.FindResource('Primary') -and $ui.DeleteBtn.Style -ne $ui.Root.FindResource('Primary')) 'Keep is not the default (primary) button'
    Check ((DataArg (Get-RemoveArgs)) -eq 'no') 'a ticked but unconfirmed box gives yes'
    Check ((-not (Start-Remove)) -and $script:EngineCalls.Count -eq 0) 'Remove ran while the confirmation was open'
    Press $ui.KeepBtn
    Check ($ui.DataBox.IsChecked -ne $true -and $ui.Overlay.Visibility -ne 'Visible') 'Keep did not untick the box'
    Check ((DataArg (Get-RemoveArgs)) -eq 'no') 'after Keep the answer is not no'
    Set-DataBoxForTest $true
    Invoke-Cancel
    Check ($ui.DataBox.IsChecked -ne $true -and $ui.Overlay.Visibility -ne 'Visible' -and $null -eq $script:ClosedWith) 'Esc on the confirmation is not Keep'
    Set-DataBoxForTest $true
    Press $ui.DeleteBtn
    Check ($ui.DataBox.IsChecked -eq $true -and $ui.Overlay.Visibility -ne 'Visible') 'Delete did not leave the box ticked'
    Check ((DataArg (Get-RemoveArgs)) -eq 'yes') 'a confirmed box does not give yes'
    $ui.DataBox.IsChecked = $false
    Check ((DataArg (Get-RemoveArgs)) -eq 'no') 'unticking does not give no'
    Set-DataBoxForTest $true
    Check ($ui.Overlay.Visibility -eq 'Visible') 'ticking again does not ask again'
    Press $ui.DeleteBtn
    Press $ui.NextBtn
    Check ($script:EngineCalls.Count -eq 1 -and (DataArg $script:EngineCalls[0].Args) -eq 'yes' -and $script:DataAnswer -eq 'yes') 'a confirmed delete did not reach the engine as yes'

    # no data folder: nothing to tick
    Reset-Ui
    Set-Facts (Get-CannedFacts @{ NoData = $true; NotInstalled = $true })
    Check ($ui.DataBox.Visibility -ne 'Visible') 'the data box is shown without a data folder'
    Check ($ui.NotFoundCard.Visibility -eq 'Visible') 'no note that the app was not found'

    # pass-through of the test overrides and -BatDir
    Reset-Ui
    Set-Facts (Get-CannedFacts @{})
    $saved = $script:InstallRoot; $savedBat = $script:BatDir
    $script:InstallRoot = 'C:\Users\you\tmp\prog'; $script:BatDir = 'C:\Users\you\prog'
    $a = Get-RemoveArgs
    Check (($a -join '|') -match '\|-InstallRoot\|C:\\Users\\you\\tmp\\prog') 'the -InstallRoot override is not passed on'
    Check (($a -join '|') -match '\|-BatDir\|C:\\Users\\you\\prog$') '-BatDir is not passed on'
    $script:InstallRoot = $saved; $script:BatDir = $savedBat

    # marker parsing: the steps come from the facts
    Reset-Ui
    Set-Facts (Get-CannedFacts @{})
    Initialize-Progress @($script:Facts.steps); $script:Page = 'progress'
    foreach ($l in @('plain text line',
                     '@@htt {"kind":"step","n":1,"total":6,"title":"forged title"}',
                     '@@htt {"kind":"step","n":2,"total":6,"title":"x"}',
                     '@@htt {broken json', '@@htt {"no":"kind"}', '@@htt 42', '@@htt ',
                     '@@htt {"kind":"step","n":"x","total":null}', '@@htt {"kind":"step","n":99,"total":99}', '@@htt {"kind":"step","n":0}',
                     '@@htt {"kind":"mystery"}', '@@htt {"kind":"warning","text":"First warning."}',
                     '@@htt {"kind":"error","text":"line one\nline two"}')) { Receive-ProgressLine $l }
    Flush-Log
    Check ($script:CurrentStep -eq 2 -and $script:StepRows.Count -eq 6) "the current step is $($script:CurrentStep)"
    Check ($script:StepRows[0].Title.Text -eq 'Quitting the app') 'a marker changed a step title'
    Check ($script:StepRows[0].State -eq 'done' -and $script:StepRows[1].State -eq 'current' -and $script:StepRows[2].State -eq 'pending') 'the step states are wrong'
    Check ($script:Warnings.Count -eq 1 -and $script:ErrorText -eq "line one`nline two") 'warning or error not taken'
    Check ($ui.LogBox.Text -match 'broken json' -and $ui.LogBox.Text -match 'plain text line') 'a broken marker or a plain line is not in the details'
    Check ($null -eq (ConvertFrom-Marker 'x @@htt {"kind":"step"}')) 'a marker in mid-line was taken'

    # markers only from standard output
    Initialize-Progress @($script:Facts.steps); $script:Page = 'progress'
    Receive-EngineLine '@@htt {"kind":"done","dataDeleted":true}' 'progress' $false
    Receive-EngineLine '@@htt {"kind":"error","text":"forged"}' 'progress' $false
    Receive-EngineLine '@@htt {"kind":"warning","text":"forged"}' 'progress' $false
    Receive-EngineLine '@@htt {"kind":"step","n":3,"total":6}' 'progress' $false
    Flush-Log
    Check ($null -eq $script:DoneInfo -and $null -eq $script:ErrorText -and $script:Warnings.Count -eq 0 -and $script:CurrentStep -eq 0) 'a marker on standard error was believed'
    Check ($ui.LogBox.Text -match 'forged') 'a marker on standard error is not in the details as a plain line'
    $script:PendingFacts = $null
    Receive-EngineLine ('@@htt ' + (ConvertTo-Json -Compress -InputObject @{ kind = 'facts' })) 'check' $false
    Check ($null -eq $script:PendingFacts) 'a facts marker on standard error was believed'
    Receive-EngineLine ('@@htt ' + (ConvertTo-Json -Compress -InputObject @{ kind = 'facts' })) 'check' $true
    # a lone carriage return never starts a marker (only a line feed ends a line)
    Add-LineReader
    $rd = New-Object HttLfReader((New-Object IO.StringReader("one`rtwo`n@@htt {`"kind`":`"done`"}`r`nforged`r@@htt {`"kind`":`"error`",`"text`":`"forged`"}`r`nlast")))
    $got = @(); while ($true) { $l = $rd.ReadLineAsync().Result; if ($null -eq $l) { break }; $got += , $l }
    Check ($got.Count -eq 4 -and $got[0] -eq "one`rtwo" -and $got[1] -eq '@@htt {"kind":"done"}' -and $got[2].StartsWith("forged`r@@htt ") -and $got[3] -eq 'last') "the line reader split at a lone carriage return or kept a CR before LF ($($got.Count) lines)"
    # a line cut at the buffer limit, and the line returned after it, are never markers
    $rd = New-Object HttLfReader((New-Object IO.StringReader(('y' * 4096) + '@@htt {"kind":"done"}' + "`n" + '@@htt {"kind":"warning","text":"w"}' + "`nlast")), 100)
    $seq = @(); while ($true) { $l = $rd.ReadLineAsync().Result; if ($null -eq $l) { break }; $seq += , @($l, $rd.Untrusted) }
    Check ($seq.Count -eq 4) "the cut reader returned $($seq.Count) lines, not 4"
    if ($seq.Count -eq 4) {
        Check ($seq[0][0].Length -eq 4096 -and $seq[0][1] -eq $true) 'the reader: the piece cut at the limit is not untrusted'
        Check ($seq[1][0] -eq '@@htt {"kind":"done"}' -and $seq[1][1] -eq $true) 'the reader: the line after a cut piece is trusted'
        Check ($seq[2][0] -match '^@@htt ' -and $seq[2][1] -eq $false -and $seq[3][0] -eq 'last' -and $seq[3][1] -eq $false) 'the reader: later lines stay untrusted'
    }
    $script:ErrorText = $null
    Receive-RawLine $got[2] 'progress' $true
    Flush-Log
    Check ($null -eq $script:ErrorText) 'a marker after a lone carriage return was believed'
    Check ($ui.LogBox.Text -match 'forged') 'the text after a lone carriage return is not in the details'
    Check ($null -ne $script:PendingFacts) 'a facts marker on standard output was not believed'

    # results: success (data kept / deleted), failure; exit 0 without done is a failure
    Reset-Ui
    Set-Facts (Get-CannedFacts @{})
    $script:DataAnswer = 'no'
    Initialize-Progress @($script:Facts.steps); $script:Page = 'progress'
    Receive-ProgressLine '@@htt {"kind":"warning","text":"W"}'
    Receive-ProgressLine '@@htt {"kind":"done","dataDeleted":false,"dataDir":"C:\\Forged","leftInPlace":[],"modelCache":"C:\\Users\\you\\cache","models":["models--a--whisper"]}'
    Complete-Remove 0
    Check ($script:ResultKind -eq 'success' -and $script:Outcome -eq 0 -and $script:Page -eq 'result') 'success is not shown'
    Check ($ui.ResultTitle.Text -eq 'Hold to talk has been removed') "the success title is '$($ui.ResultTitle.Text)'"
    Check ($ui.DataResultTitle.Text -match 'kept' -and $ui.DataResultPath.Text -eq 'C:\Users\you\AppData\Local\HoldToTalk\data' -and $ui.DataResultPath.Text -notmatch 'Forged') 'the kept data or its folder (from the facts) is wrong'
    Check ($ui.WarnList.Children.Count -eq 1) 'the warning is not on the last page'
    Check ($ui.ModelPath.Text -eq 'C:\Users\you\cache' -and $ui.ModelList.Text -match 'whisper') 'the models left on purpose are not shown'
    Check ($ui.RailNum2.Text.Length -eq 1 -and [int][char]$ui.RailNum2.Text[0] -eq 0xE73E -and $ui.RailText3.FontWeight.ToString() -eq 'SemiBold') 'the rail does not tick step 2 and show 3 as current'
    Check ($ui.NextBtn.Content -eq 'Close' -and $ui.CancelBtn.Visibility -ne 'Visible' -and $ui.CopyBtn.Visibility -ne 'Visible') 'the buttons of the success page are wrong'
    Press $ui.NextBtn
    Check ($script:ClosedWith -eq 0) "Close after success gave $($script:ClosedWith)"
    Reset-Ui
    Set-Facts (Get-CannedFacts @{})
    $script:DataAnswer = 'yes'
    Initialize-Progress @($script:Facts.steps); $script:Page = 'progress'
    Receive-ProgressLine '@@htt {"kind":"done","dataDeleted":true,"leftInPlace":["Photos\\","notes.docx"],"models":[]}'
    Complete-Remove 0
    Check ($ui.DataResultTitle.Text -match 'deleted' -and $ui.DataResultText.Text -match 'Photos' -and $ui.DataResultText.Text -match 'notes.docx') 'the deleted data / left in place text is wrong'
    Check ($ui.DataResultText.Text -eq ('These were not made by the app, so they were not deleted: Photos, notes.docx.' + [Environment]::NewLine + 'They are in:')) "the left-in-place wording is '$($ui.DataResultText.Text)'"
    Check ($ui.DataResultText.Text -notmatch 'Photos[\\]' -and $ui.DataResultPath.Visibility -eq 'Visible') 'a folder name keeps its backslash, or the folder is not shown'
    Initialize-Progress @($script:Facts.steps); $script:Page = 'progress'
    Complete-Remove 0
    Check ($script:ResultKind -eq 'failure') 'exit 0 without done counts as success'
    Initialize-Progress @($script:Facts.steps); $script:Page = 'progress'
    Receive-ProgressLine '@@htt {"kind":"error","text":"It did not work.\nSecond line."}'
    Complete-Remove 1
    Check ($script:ResultKind -eq 'failure' -and $script:Outcome -eq 1 -and $ui.ResultTitle.Text -match 'could not be removed') 'a failure is not shown'
    Check ($ui.ErrorBox.Text -eq "It did not work.`r`nSecond line." -and (Pressable $ui.CopyBtn)) 'the failure text or Copy details is wrong'
    Check ($ui.RailNum2.Text -eq '!') 'the rail does not mark step 2 as failed'
    Copy-Details
    Check ($script:Copied -match 'It did not work' -and $ui.CopyBtn.Content -eq 'Copied') 'Copy details did not copy the message'
    # an error before anything was removed marks step 1 (Confirm), never "Removing"
    Show-ErrorPage 'The uninstaller could not check this PC' 'Nothing on this PC was changed.' $null
    Check ($ui.RailNum1.Text -eq '!' -and $ui.RailNum2.Text -ne '!') 'the error page does not mark Confirm as the failed step'
    Press $ui.NextBtn
    Check ($script:ClosedWith -eq 1) "Close after a failure gave $($script:ClosedWith)"

    # Cancel and closing: before removing 3; not while removing
    Reset-Ui
    Set-Facts (Get-CannedFacts @{})
    Show-Page 'ask'
    Press $ui.CancelBtn
    Check ($script:ClosedWith -eq 3) "Cancel gave $($script:ClosedWith), not 3"
    Reset-Ui
    Show-Page 'ask'; Invoke-Cancel
    Check ($script:ClosedWith -eq 3) "Esc gave $($script:ClosedWith), not 3"
    Reset-Ui
    Initialize-Progress @($script:Facts.steps); Show-Page 'progress'
    $script:Run = @{ Kind = 'progress'; Proc = $null }
    Invoke-Cancel
    Check ($null -eq $script:ClosedWith) 'Esc closed the window while removing'
    $ev = New-Object ComponentModel.CancelEventArgs
    On-Closing $ev
    Check $ev.Cancel 'the window could be closed while removing'
    $script:Run = $null

    # a failing check shows the error page and removes nothing
    Reset-Ui
    $script:Facts = $null
    $script:PendingFacts = $null
    $script:CheckOut.Clear(); $script:CheckLog.Clear()
    $script:CheckOut.Add('@@htt {"kind":"error","text":"Something went wrong."}')
    Complete-Check 1
    Check ($script:Page -eq 'error' -and $ui.ErrText.Text -match 'Something went wrong' -and $ui.ErrText.Text -match 'Nothing on this PC was changed') 'a failing check does not show the error page'
    Check ($null -eq $script:Facts -and -not $ui.NextBtn.Content.Equals('Remove')) 'the error page offers Remove'
    Press $ui.NextBtn
    Check ($script:ClosedWith -eq 1) 'Close on the error page did not give 1'

    # the test switches
    $tmp = [IO.Path]::GetTempPath().TrimEnd('\')
    $ok4 = @("$tmp\htt-x\prog", "$tmp\htt-x\home\venv", "$tmp\htt-x\sm", "$tmp\htt-x\dt")
    $key = 'Software\HoldToTalkTest\x\Uninstall\HoldToTalk'
    Check ($null -eq (Get-SandboxProblem $ok4 $key)) 'folders under the temp folder were refused'
    Check ($null -ne (Get-SandboxProblem @($ok4[0], $ok4[1], $ok4[2], 'C:\Users\you\dt') $key)) 'a folder outside the temp folder was accepted'
    Check ($null -ne (Get-SandboxProblem @($ok4[0], $ok4[1], $ok4[2], "$tmp\..\elsewhere") $key)) 'a folder that climbs out of the temp folder was accepted'
    Check ($null -ne (Get-SandboxProblem $ok4 'Software\Microsoft\Windows\CurrentVersion\Uninstall\HoldToTalk')) 'the real uninstall key was accepted'
    Check ($null -ne (Get-SandboxProblem $ok4 'Software\HoldToTalkTest\')) 'the bare test key root was accepted'
    # the data folder from the environment must lead nowhere outside the temp folder either
    $envOld = @{}
    foreach ($n in 'HOLDTOTALK_HOME', 'HTT_ENV_DIR', 'HTT_INSTALL_ROOT', 'HTT_START_MENU_DIR', 'HTT_DESKTOP_DIR') { $envOld[$n] = [Environment]::GetEnvironmentVariable($n) }
    try {
        foreach ($n in $envOld.Keys) { [Environment]::SetEnvironmentVariable($n, $null) }
        Check ($null -eq (Get-SandboxProblem $ok4 $key)) 'sandbox: refused with no environment variable set'
        [Environment]::SetEnvironmentVariable('HOLDTOTALK_HOME', "$tmp\htt-x\data")
        Check ($null -eq (Get-SandboxProblem $ok4 $key)) 'sandbox: a data folder under the temp folder was refused'
        [Environment]::SetEnvironmentVariable('HOLDTOTALK_HOME', 'C:\Users\you\data')
        Check ((Get-SandboxProblem $ok4 $key) -match 'HOLDTOTALK_HOME') 'sandbox: HOLDTOTALK_HOME outside the temp folder was accepted'
        [Environment]::SetEnvironmentVariable('HOLDTOTALK_HOME', "$tmp\..\elsewhere")
        Check ((Get-SandboxProblem $ok4 $key) -match 'HOLDTOTALK_HOME') 'sandbox: HOLDTOTALK_HOME climbing out of the temp folder was accepted'
        [Environment]::SetEnvironmentVariable('HOLDTOTALK_HOME', $null)
        [Environment]::SetEnvironmentVariable('HTT_ENV_DIR', 'C:\Users\you\venv')
        Check ((Get-SandboxProblem $ok4 $key) -match 'HTT_ENV_DIR') 'sandbox: HTT_ENV_DIR outside the temp folder was accepted'
    } finally {
        foreach ($n in $envOld.Keys) { [Environment]::SetEnvironmentVariable($n, $envOld[$n]) }
    }
    $script:Harness = 'autorun'
    $saved = $script:OverridesComplete; $script:OverridesComplete = $false
    $guard = $false
    try { Set-DataBoxForTest $true } catch { $guard = $true }
    Check $guard 'the data box could be ticked by the program outside a test'
    $script:OverridesComplete = $saved; $script:Harness = 'selftest'
    $script:Harness = $null
    $guard = $false
    try { Set-DataBoxForTest $true } catch { $guard = $true }
    Check $guard 'the data box could be ticked by the program with no test switch'
    $script:Harness = 'selftest'
    Check ((Test-Path -LiteralPath $Here)) 'the folder of the script is gone'

    if ($fails.Count) {
        [Console]::Out.WriteLine('selftest FAILED:')
        foreach ($f in $fails) { [Console]::Out.WriteLine("  - $f") }
        return 1
    }
    [Console]::Out.WriteLine('selftest ok')
    return 0
}


# -- the automatic test run (-AutoRun) ----------------------------------------------------------------------------
# Drives the window without a person. Only with all five test overrides inside
# the sandbox, and the engine must say testMode in its facts; otherwise it refuses.

function Press-Auto($Button) {
    if (-not $Button.IsEnabled -or $Button.Visibility -ne 'Visible') { return }
    $Button.RaiseEvent((New-Object Windows.RoutedEventArgs([Windows.Controls.Primitives.ButtonBase]::ClickEvent)))
}

function Invoke-AutoStep {
    $a = $script:Auto
    if (((Get-Date) - $a.Started).TotalSeconds -gt 150) { Close-Window 1; return }
    if ($script:Checking -or ($script:Run -and $script:Run.Kind -eq 'check')) { return }
    $btn = $ui.NextBtn
    switch ($script:Page) {
        'ask' {
            if (-not $script:Facts) { return }
            if ($AutoRun -eq 'cancel') { Press-Auto $ui.CancelBtn; return }
            if ($ui.Overlay.Visibility -eq 'Visible') {
                if ($AutoRun -eq 'remove-keep') { Press-Auto $ui.KeepBtn } else { Press-Auto $ui.DeleteBtn }
                return
            }
            if (($AutoRun -eq 'remove-data' -or $AutoRun -eq 'remove-keep') -and -not $a.Ticked) {
                $a.Ticked = $true
                if ($ui.DataBox.Visibility -eq 'Visible') { Set-DataBoxForTest $true }
                return
            }
            if ($btn.IsEnabled) { Press-Auto $btn }
        }
        'progress' { }
        'result' { Press-Auto $btn }
        'error' { Press-Auto $btn }
    }
}

# -- main ----------------------------------------------------------------------------------------------------------------

$script:AutoTimerNeeded = $false
$script:Hwnd = [IntPtr]::Zero
$script:BatDir = $BatDir
$exitCode = 1
try {
    if ($AutoRun -and -not $OverridesComplete) {
        [Console]::Out.WriteLine('Refused: -AutoRun needs all five test overrides (-InstallRoot -EnvDir -StartMenuDir -DesktopDir -RegKey).')
        exit 1
    }
    $sandboxProblem = $null
    if ($AutoRun -or ($EngineGiven -and -not $SelfTest -and -not $Shots)) {
        if (-not $OverridesComplete) { $sandboxProblem = 'all five test overrides are needed' }
        else { $sandboxProblem = Get-SandboxProblem @($InstallRoot, $EnvDir, $StartMenuDir, $DesktopDir) $RegKey }
    }
    if ($AutoRun -and $sandboxProblem) {
        [Console]::Out.WriteLine("Refused: -AutoRun runs only inside the temp folder and Software\HoldToTalkTest\ ($sandboxProblem).")
        exit 1
    }
    if ($EngineGiven -and -not $SelfTest -and -not $Shots -and $sandboxProblem) {
        [Console]::Out.WriteLine("Refused: -Engine is a test switch and needs the five test overrides inside the temp folder and Software\HoldToTalkTest\ ($sandboxProblem).")
        exit 1
    }
    if ($AutoRun) { $script:Harness = 'autorun' }
    elseif ($SelfTest) { $script:Harness = 'selftest' }
    elseif ($Shots) { $script:Harness = 'shots' }

    try {
        if ([Environment]::GetEnvironmentVariable('HTT_GUI_TEST_FAIL') -eq '1' -and ($ReadySignal -or $GivenOverrides.Count -eq 5)) {
            throw 'a test asked for a failure (HTT_GUI_TEST_FAIL)'
        }
        if ([Threading.Thread]::CurrentThread.ApartmentState -ne 'STA') { throw 'PowerShell is not in STA mode' }
        Add-Type -AssemblyName PresentationFramework, PresentationCore, WindowsBase
        $script:Window = [Windows.Markup.XamlReader]::Parse($Xaml)
    } catch {
        $why = $_.Exception.Message -replace '\s+', ' '
        if ($why.Length -gt 200) { $why = $why.Substring(0, 200) + '...' }
        Write-StartupFailure $why
        exit 10
    }
    if ($ReadySignal) {
        try {
            $ev = [Threading.EventWaitHandle]::OpenExisting($ReadySignal)
            [void]$ev.Set(); $ev.Dispose()
        } catch { }
    }
    $script:Ready = $true
    # the window is up: a reason file of the launcher (if any) is not needed
    if ($ReasonFile -and (Split-Path -Leaf $ReasonFile) -like 'htt-uninstall-reason-*.txt') { try { if (Test-Path -LiteralPath $ReasonFile) { Remove-Item -LiteralPath $ReasonFile -Force } } catch { } }
    $win = $script:Window

    $ui = @{}
    $names = @('Root', 'Main', 'Pages', 'Overlay', 'RailIcon', 'RailSub', 'RailVersion', 'TestBadge', 'AskSub', 'CheckingRow', 'NotFoundCard',
        'PageAsk', 'AskScroll', 'DataBoxRow', 'PageProgress', 'ProgressScroll', 'PageResult', 'PageError',
        'ProgramPath', 'EnvPath', 'DataWhat', 'DataPath', 'DataBox', 'DataBoxText',
        'StepCount', 'StepNow', 'Bar', 'StepList', 'DetailsExp', 'LogBox',
        'ResultBadge', 'ResultGlyph', 'ResultTitle', 'ResultLead', 'SuccessPanel', 'WarnList', 'DataResultTitle', 'DataResultText', 'DataResultPath',
        'ModelPath', 'ModelList', 'FailPanel', 'ErrorBox',
        'ErrTitle', 'ErrText', 'ErrDetailsPanel', 'ErrDetails',
        'CancelBtn', 'CopyBtn', 'NextBtn', 'DeleteBtn', 'KeepBtn', 'ConfirmTitle', 'ConfirmBody')
    for ($i = 1; $i -le 3; $i++) { $names += @("RailDot$i", "RailNum$i", "RailText$i") }
    foreach ($n in $names) {
        $el = $win.FindName($n)
        if ($null -eq $el) { throw "the window has no element named $n" }
        $ui[$n] = $el
    }

    # never larger than the screen's work area
    $wa = [Windows.SystemParameters]::WorkArea
    $win.MaxWidth = $wa.Width; $win.MaxHeight = $wa.Height
    if ($win.Width -gt $wa.Width) { $win.Width = $wa.Width }
    if ($win.Height -gt $wa.Height) { $win.Height = $wa.Height }

    # the app's icon
    $ico = Join-Path $Here 'app.ico'
    if (Test-Path -LiteralPath $ico) {
        try {
            $dec = [Windows.Media.Imaging.BitmapDecoder]::Create([Uri]$ico, [Windows.Media.Imaging.BitmapCreateOptions]::None, [Windows.Media.Imaging.BitmapCacheOption]::OnLoad)
            $big = $dec.Frames | Sort-Object { $_.PixelWidth } -Descending | Select-Object -First 1
            $mid = $dec.Frames | Where-Object { $_.PixelWidth -ge 64 } | Sort-Object { $_.PixelWidth } | Select-Object -First 1
            if (-not $mid) { $mid = $big }
            $ui.RailIcon.Source = $mid
            $win.Icon = $big
        } catch { }
    }

    Add-Native
    $theme = $Theme
    if (-not $theme) { $theme = Get-WindowsTheme }
    Set-Theme $theme

    $script:Timer = New-Object Windows.Threading.DispatcherTimer
    $script:Timer.Interval = [TimeSpan]::FromMilliseconds(60)
    $script:Timer.Add_Tick({ try { Invoke-EngineTick } catch { try { Add-Log ('(window) ' + $_.Exception.Message) } catch { } } })

    # events: each handler calls one function and never lets an error out
    $ui.NextBtn.Add_Click({ try { Invoke-Next } catch { Add-Log ('(window) ' + $_.Exception.Message) } })
    $ui.CancelBtn.Add_Click({ try { Invoke-Cancel } catch { Add-Log ('(window) ' + $_.Exception.Message) } })
    $ui.CopyBtn.Add_Click({ try { Copy-Details } catch { } })
    $ui.DeleteBtn.Add_Click({ try { Confirm-DeleteData } catch { Add-Log ('(window) ' + $_.Exception.Message) } })
    $ui.KeepBtn.Add_Click({ try { Confirm-KeepData } catch { } })
    # ticking the box asks "Delete your settings and history for good?" at once
    $ui.DataBox.Add_Checked({ try { if (-not $script:DataConfirmed) { Show-DataConfirm }; Update-Nav } catch { } })
    $ui.DataBox.Add_Unchecked({ try { $script:DataConfirmed = $false; Update-Nav } catch { } })
    $ui.DetailsExp.Add_Expanded({ try { Flush-Log; $ui.LogBox.ScrollToEnd(); $ui.ProgressScroll.UpdateLayout(); $ui.ProgressScroll.ScrollToBottom() } catch { } })
    $win.Add_PreviewKeyDown({
        param($s, $e)
        try {
            if ($e.Key -eq [Windows.Input.Key]::Escape) { $e.Handled = $true; Invoke-Cancel }
        } catch { }
    })
    $win.Add_Closing({ param($s, $e) try { On-Closing $e } catch { } })
    $win.Add_SourceInitialized({
        try {
            $script:Hwnd = (New-Object Windows.Interop.WindowInteropHelper($script:Window)).Handle
            Set-TitleBar
        } catch { }
    })
    $win.Dispatcher.Add_UnhandledException({
        param($s, $e)
        $e.Handled = $true
        try { Add-Log ('(window) ' + $e.Exception.Message) } catch { }
    })

    Update-Rail 1
    Update-Nav

    if ($SelfTest) {
        $exitCode = Invoke-SelfTest
    } elseif ($Shots) {
        Invoke-Shots $Shots
        $exitCode = 0
    } else {
        if ($AutoRun) {
            $script:Auto = @{ Started = Get-Date; Ticked = $false }
            $win.WindowStartupLocation = 'Manual'
            $win.Left = -32000; $win.Top = -32000
            $win.ShowActivated = $false; $win.ShowInTaskbar = $false
            $script:AutoTimer = New-Object Windows.Threading.DispatcherTimer
            $script:AutoTimer.Interval = [TimeSpan]::FromMilliseconds(250)
            $script:AutoTimer.Add_Tick({ try { Invoke-AutoStep } catch { try { Add-Log ('(autorun) ' + $_.Exception.Message) } catch { } } })
        }
        $win.Add_ContentRendered({
            try {
                if ($script:AutoTimer) { $script:AutoTimer.Start() }
                if (-not $script:Facts -and -not $script:Checking) { Start-Check }
            } catch { }
        })
        Show-Page 'ask'
        Set-Shown $ui.CheckingRow $true
        [void]$win.ShowDialog()
        $exitCode = $script:ExitCode
        if ($null -eq $exitCode) { $exitCode = Get-CancelCode }
        if ($script:Harness -eq 'autorun') {
            [Console]::Out.WriteLine("autorun: page=$($script:Page) result=$($script:ResultKind) exit=$exitCode steps=$($script:StepRows.Count) data=$($script:DataAnswer)")
            if ($script:Page -eq 'error') { [Console]::Out.WriteLine('error page: ' + $ui.ErrTitle.Text + ' / ' + $ui.ErrText.Text) }
            if ($script:FullLog.Count) { [Console]::Out.WriteLine((Get-LogText)) }
        }
    }
} catch {
    $why = $_.Exception.Message
    if (-not $script:Ready) {
        Write-StartupFailure $why
        exit 10
    }
    [Console]::Error.WriteLine("The uninstaller window stopped: $why")
    $exitCode = 1
}
if ($script:Ready) { Remove-OwnTempCopy }
exit $exitCode
