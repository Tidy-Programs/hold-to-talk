# install-gui.ps1 - the setup window of Hold to talk (install.bat with no
# arguments opens it). Windows PowerShell 5.1 + WPF; keep this file plain ASCII.
#
# The window never installs anything itself. It runs install.ps1 (the
# "engine") as a hidden child process: first "-Check" (changes nothing, prints
# one facts line), then, after the person has ticked the Terms of Use box and
# chosen Install, "-Progress -AcceptedTerms <version> ..." and shows the
# marker lines it prints. The contract between the two is in the comments of
# install.ps1 (-Check / -Progress).
#
#   install.bat                         opens this window (through -Launch)
#   install-gui.ps1 -SelfTest           checks the window's logic, shows nothing
#   install-gui.ps1 -Shots <folder>     saves pictures of every page (canned facts)
#
# Test switches: -Engine <path> (another engine, e.g. the fake one in
# tests\fixtures), -AutoRun (drives the window without a person), -Theme
# light|dark. The test overrides -InstallRoot -EnvDir -StartMenuDir
# -DesktopDir -RegKey and -NoDeps -SkipModel -Gpu -PipArgs -UseRanges are
# passed on to the engine unchanged.
# -AutoRun refuses unless all five overrides are given, the four folders are
# under the temp folder and the key is under Software\HoldToTalkTest\.
# -Engine is honoured only under the same conditions (or with -SelfTest /
# -Shots); otherwise the window exits 1 and starts nothing.
#
# What the window trusts: markers (the "@@htt " lines) only when they come from
# the engine's standard output; a line on standard error is log text. The
# "Start Hold to talk now" command is computed here from the facts' layout, never
# taken from a marker. Step numbers come from the facts' step titles.
#
# Exit codes: 0 installed, 1 failed, 2 stopped during the install,
# 3 closed before installing (nothing was changed).

[CmdletBinding()]
param(
    [string]$Engine,
    [switch]$SelfTest,
    [string]$Shots,
    [ValidateSet('', 'install', 'install-none', 'cancel-terms', 'stop-install', 'install-python')][string]$AutoRun = '',
    [ValidateSet('', 'light', 'dark')][string]$Theme = '',
    [switch]$Launch,
    [switch]$FromSetupExe,
    [string]$ReadySignal,
    [string]$ReasonFile,
    [string]$InstallRoot,
    [string]$EnvDir,
    [string]$StartMenuDir,
    [string]$DesktopDir,
    [string]$RegKey,
    [switch]$NoDeps,
    [switch]$SkipModel,
    [ValidateSet('auto', 'yes', 'no')][string]$Gpu = 'auto',
    [string]$PipArgs,
    [switch]$UseRanges
)

$ErrorActionPreference = 'Stop'
$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
$ScriptPath = $MyInvocation.MyCommand.Path
$PsExe = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'
if (-not (Test-Path -LiteralPath $PsExe)) { $PsExe = 'powershell.exe' }
$EngineGiven = [bool]$Engine
if (-not $Engine) { $Engine = Join-Path $Here 'install.ps1' }
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

# -- the launcher (install.bat runs this in its console) ---------------------------
# Starts the window in a process that has no console window, waits until the
# window has loaded (or failed to), and returns: 0 = the window is up (the
# console closes), 1 = it could not be shown (install.bat then runs the text
# installer in the console, after the one line printed here).

function Invoke-Launcher {
    Write-Host 'Opening the setup window...'
    $id = [Guid]::NewGuid().ToString('N')
    $signal = 'Local\HoldToTalkSetup-' + $id
    $reason = Join-Path ([IO.Path]::GetTempPath()) ('htt-setup-' + $id + '.txt')
    $ready = New-Object Threading.EventWaitHandle($false, [Threading.EventResetMode]::ManualReset, $signal)
    try {
        $psi = New-Object Diagnostics.ProcessStartInfo
        $psi.FileName = $PsExe
        $psi.Arguments = (@('-NoProfile', '-ExecutionPolicy', 'Bypass', '-STA', '-File', $ScriptPath,
                            '-ReadySignal', $signal, '-ReasonFile', $reason) | ForEach-Object { Quote-Arg $_ }) -join ' '
        $psi.UseShellExecute = $false
        $psi.CreateNoWindow = $true
        $psi.WorkingDirectory = $Here
        try { $p = [Diagnostics.Process]::Start($psi) }
        catch { Write-Host "The setup window could not be opened ($($_.Exception.Message)). Using the text installer instead."; return 1 }
        $deadline = (Get-Date).AddSeconds(90)
        while ((Get-Date) -lt $deadline) {
            if ($ready.WaitOne(150)) { return 0 }
            if ($p.HasExited) {
                if ($ready.WaitOne(0)) { return 0 }
                $why = $null
                try { if (Test-Path -LiteralPath $reason) { $why = (Get-Content -LiteralPath $reason -Raw).Trim() } } catch { }
                if (-not $why) { $why = "it closed unexpectedly, code $($p.ExitCode)" }
                Write-Host "The setup window could not be opened ($why). Using the text installer instead."
                return 1
            }
        }
        return 0     # still loading after 90 s: leave it be
    } finally {
        # The reason file is for this launcher only: never left behind.
        try { if (Test-Path -LiteralPath $reason) { Remove-Item -LiteralPath $reason -Force -ErrorAction Stop } } catch { }
        try { $ready.Dispose() } catch { }
    }
}

if ($Launch) { exit (Invoke-Launcher) }

# -- facts that do not need the window ----------------------------------------------

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
    if ('HttSetupNative' -as [type]) {
        try { $r = [HttSetupNative]::LongPath($cur); if ($r) { $long = $r } } catch { }
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
    foreach ($n in ($OverrideNames + 'PipArgs')) {
        $v = Get-Variable -Name $n -Scope Script -ValueOnly
        if ($v) { $a += @("-$n", [string]$v) }
    }
    if ($script:NoDeps) { $a += '-NoDeps' }
    if ($script:SkipModel) { $a += '-SkipModel' }
    if ($script:UseRanges) { $a += '-UseRanges' }
    if ($script:Gpu -ne 'auto') { $a += @('-Gpu', $script:Gpu) }
    return , $a
}

function Write-StartupFailure([string]$Why) {
    # Before the window is up: tell the launcher (install.bat then runs the
    # text installer), or the console when run by hand.
    if ($ReasonFile) {
        try { [IO.File]::WriteAllText($ReasonFile, $Why, [Text.Encoding]::ASCII) } catch { }
    } else {
        [Console]::Error.WriteLine("The setup window could not be opened: $Why")
    }
}

function Get-DownloadAdvice([bool]$Again) {
    # What to do when a file of the download is missing or wrong. The ZIP route
    # says to extract it; the Setup.exe route has no folder to extract, so it
    # points to the project's page.
    if ($FromSetupExe) {
        return "Download it again from the project's page, or download the ZIP there and run install.bat."
    }
    if ($Again) { return 'Extract the whole ZIP again (right-click the ZIP > Extract All).' }
    return 'Extract the whole ZIP first (right-click the ZIP > Extract All), then run install.bat from the new folder.'
}

function Start-TextInstallerFallback([string]$Why) {
    # After the window was already up (rare): open the text installer in a
    # visible console window, with one line that says why. Never from the
    # Setup.exe: its files are in a folder that is deleted when it ends.
    if ($FromSetupExe) { return }
    try {
        $bat = Join-Path $Here 'install.bat'
        # install.bat echoes it: no characters that cmd would act on.
        $safe = ($Why -replace '[&|<>^%()"!]', ' ' -replace '\s+', ' ').Trim().TrimEnd('.')
        if ($safe.Length -gt 200) { $safe = $safe.Substring(0, 200) }
        [Environment]::SetEnvironmentVariable('HTT_FALLBACK_REASON', $safe, 'Process')
        $cmd = Join-Path $env:SystemRoot 'System32\cmd.exe'
        Start-Process -FilePath $cmd -ArgumentList ('/c ""' + $bat + '""') -WorkingDirectory $Here
    } catch { }
}

# -- WPF and the window ---------------------------------------------------------------

$Xaml = @'
<Window xmlns="http://schemas.microsoft.com/winfx/2006/xaml/presentation"
        xmlns:x="http://schemas.microsoft.com/winfx/2006/xaml"
        xmlns:sys="clr-namespace:System;assembly=mscorlib"
        Title="Install Hold to talk" Width="780" Height="580" MinWidth="640" MinHeight="420"
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

    <!-- the rail: the app and the five steps -->
    <Border Background="{DynamicResource Side}" BorderBrush="{DynamicResource Line}" BorderThickness="0,0,1,0">
      <DockPanel Margin="22,24,18,20">
        <StackPanel DockPanel.Dock="Top" Orientation="Horizontal">
          <Image x:Name="RailIcon" Width="32" Height="32" RenderOptions.BitmapScalingMode="HighQuality" VerticalAlignment="Center"/>
          <StackPanel Margin="10,0,0,0" VerticalAlignment="Center">
            <TextBlock Text="Hold to talk" FontWeight="SemiBold" FontSize="15"/>
            <TextBlock x:Name="RailSub" Text="Setup" Foreground="{DynamicResource Text2}" FontSize="12.5"/>
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

        <!-- 1. Welcome -->
        <ScrollViewer x:Name="PageWelcome" VerticalScrollBarVisibility="Auto" Focusable="False">
          <StackPanel Margin="40,36,40,24" MaxWidth="640" HorizontalAlignment="Left">
            <Image x:Name="WelcomeIcon" Width="64" Height="64" HorizontalAlignment="Left" RenderOptions.BitmapScalingMode="HighQuality"/>
            <TextBlock Text="Hold to talk" Style="{StaticResource H1}" FontSize="30" Margin="0,18,0,0"/>
            <TextBlock x:Name="WelcomeVersion" Style="{StaticResource Muted}" FontSize="14" Margin="0,2,0,0"/>
            <TextBlock Style="{StaticResource Body}" Margin="0,18,0,0"
                       Text="Dictate into any program: hold a key, speak, and let go. Your words are typed where your cursor is."/>
            <TextBlock Style="{StaticResource Body}" Margin="0,8,0,0"
                       Text="Speech is recognised on this PC, so nothing you say is sent anywhere."/>
            <Border x:Name="UpdateCard" Style="{StaticResource BrandCard}" Margin="0,24,0,0" Visibility="Collapsed">
              <Grid>
                <Grid.ColumnDefinitions><ColumnDefinition Width="48"/><ColumnDefinition Width="*"/></Grid.ColumnDefinitions>
                <Border Style="{StaticResource Tile}" Background="{DynamicResource Surface}" VerticalAlignment="Center">
                  <TextBlock Style="{StaticResource Glyph}" Text="&#xE895;"/>
                </Border>
                <TextBlock x:Name="UpdateText" Grid.Column="1" Style="{StaticResource Body}" VerticalAlignment="Center"/>
              </Grid>
            </Border>
            <Border x:Name="AccountCard" Style="{StaticResource Card}" Margin="0,24,0,0">
              <Grid>
                <Grid.ColumnDefinitions><ColumnDefinition Width="48"/><ColumnDefinition Width="*"/></Grid.ColumnDefinitions>
                <Border Style="{StaticResource Tile}" VerticalAlignment="Center"><TextBlock Style="{StaticResource Glyph}" Text="&#xE77B;"/></Border>
                <TextBlock x:Name="AccountText" Grid.Column="1" Style="{StaticResource Body}" VerticalAlignment="Center"
                           Text="Installs for your Windows account only. No administrator rights needed."/>
              </Grid>
            </Border>
            <StackPanel x:Name="CheckingRow" Orientation="Horizontal" Margin="2,22,0,0">
              <Control Template="{StaticResource Spinner}" Focusable="False" VerticalAlignment="Center"/>
              <TextBlock Text="Checking this PC..." Foreground="{DynamicResource Text2}" Margin="10,0,0,0" VerticalAlignment="Center"/>
            </StackPanel>
          </StackPanel>
        </ScrollViewer>

        <!-- 2. Terms of Use -->
        <ScrollViewer x:Name="PageTerms" VerticalScrollBarVisibility="Auto" Focusable="False" Visibility="Collapsed">
          <Grid MinHeight="400" Height="{Binding ViewportHeight, ElementName=PageTerms}">
            <Grid Margin="40,28,40,16">
              <Grid.RowDefinitions>
                <RowDefinition Height="Auto"/>
                <RowDefinition Height="Auto"/>
                <RowDefinition Height="*"/>
                <RowDefinition Height="Auto"/>
                <RowDefinition Height="Auto"/>
                <RowDefinition Height="Auto"/>
              </Grid.RowDefinitions>
              <TextBlock Text="Terms of Use" Style="{StaticResource H2}"/>
              <TextBlock x:Name="TermsLead" Grid.Row="1" Style="{StaticResource Muted}" Margin="0,4,0,14"/>
              <Border Grid.Row="2" CornerRadius="10" BorderThickness="1" BorderBrush="{DynamicResource Line}"
                      Background="{DynamicResource Surface}" Padding="1" MinHeight="96">
                <FlowDocumentScrollViewer x:Name="TermsView" VerticalScrollBarVisibility="Auto" HorizontalScrollBarVisibility="Disabled"
                                          IsToolBarVisible="False" SelectionBrush="{DynamicResource Brand}"
                                          FocusVisualStyle="{StaticResource FocusRing}"
                                          AutomationProperties.Name="Terms of Use, full text"/>
              </Border>
              <CheckBox x:Name="AgreeBox" Grid.Row="3" Margin="0,16,0,0" FontWeight="SemiBold"
                        AutomationProperties.Name="I have read and agree to the Terms of Use">
                <TextBlock x:Name="AgreeText" TextWrapping="Wrap"/>
              </CheckBox>
              <StackPanel Grid.Row="4" Margin="30,6,0,0">
                <TextBlock Style="{StaticResource Muted}" Text="By agreeing you accept the whole terms."/>
                <TextBlock Style="{StaticResource Muted}"
                           Text="This includes the sections on no warranty, limits of liability, assumption of risk, indemnity and disputes (sections 9 to 14)."/>
              </StackPanel>
              <WrapPanel x:Name="TermsLinks" Grid.Row="5" Margin="30,12,0,0" Orientation="Horizontal">
                <TextBlock Style="{StaticResource Muted}" FontSize="13" Text="Open in Notepad:" Margin="0,0,12,4"/>
                <Button x:Name="LinkNotepad" Style="{StaticResource Link}" Content="Terms" Margin="0,0,14,4"
                        AutomationProperties.Name="Open the Terms of Use in Notepad"/>
                <Button x:Name="LinkLicense" Style="{StaticResource Link}" Content="Licence" Margin="0,0,14,4"
                        AutomationProperties.Name="Open the licence (LICENSE) in Notepad"/>
                <Button x:Name="LinkNotices" Style="{StaticResource Link}" Content="Third-party notices" Margin="0,0,14,4"
                        AutomationProperties.Name="Open the third-party notices in Notepad"/>
                <Button x:Name="LinkSecurity" Style="{StaticResource Link}" Content="Security" Margin="0,0,14,4"
                        AutomationProperties.Name="Open the security notes (SECURITY.md) in Notepad"/>
                <TextBlock x:Name="LinkStatus" Style="{StaticResource Muted}" FontSize="12.5" Visibility="Collapsed"/>
              </WrapPanel>
            </Grid>
          </Grid>
        </ScrollViewer>

        <!-- 3. What will be installed, and where -->
        <ScrollViewer x:Name="PageChoose" VerticalScrollBarVisibility="Auto" Focusable="False" Visibility="Collapsed">
          <StackPanel Margin="40,28,40,24">
            <TextBlock x:Name="ChooseTitle" Text="What will be installed" Style="{StaticResource H2}"/>
            <TextBlock x:Name="ChooseSub" Style="{StaticResource Muted}" Margin="0,4,0,16"
                       Text="Everything goes into your own Windows account. Nothing changes until you choose Install."/>

            <!-- low disk space: at the top, so it is seen without scrolling -->
            <Border x:Name="DiskWarn" Style="{StaticResource WarnCard}" Margin="0,0,0,16" Padding="14,10" Visibility="Collapsed">
              <Grid>
                <Grid.ColumnDefinitions><ColumnDefinition Width="28"/><ColumnDefinition Width="*"/></Grid.ColumnDefinitions>
                <TextBlock Style="{StaticResource Glyph}" Text="&#xE7BA;" Foreground="{DynamicResource Warn}" VerticalAlignment="Top" HorizontalAlignment="Left" Margin="0,2,0,0"/>
                <TextBlock x:Name="DiskWarnText" Grid.Column="1" Style="{StaticResource Body}"/>
              </Grid>
            </Border>

            <StackPanel x:Name="RecheckRow" Orientation="Horizontal" Margin="2,0,0,14" Visibility="Collapsed">
              <Control Template="{StaticResource Spinner}" Focusable="False" VerticalAlignment="Center"/>
              <TextBlock Text="Checking this PC..." Foreground="{DynamicResource Text2}" Margin="10,0,0,0" VerticalAlignment="Center"/>
            </StackPanel>

            <!-- Python is missing -->
            <Border x:Name="PythonPanel" Style="{StaticResource WarnCard}" Padding="20,18" Visibility="Collapsed">
              <StackPanel>
                <StackPanel Orientation="Horizontal">
                  <TextBlock Style="{StaticResource Glyph}" Text="&#xE7BA;" Foreground="{DynamicResource Warn}" FontSize="18" Margin="0,0,10,0"/>
                  <TextBlock Style="{StaticResource H3}" Text="Python is needed first" Foreground="{DynamicResource Warn}" FontSize="16"/>
                </StackPanel>
                <TextBlock x:Name="PyLead" Style="{StaticResource Body}" Margin="0,10,0,0"
                           Text="Python is a free program that Hold to talk needs. Choose the button and Setup downloads it from python.org and installs it for your Windows account only. Nothing is downloaded until you choose the button."/>
                <WrapPanel Margin="0,14,0,0" Orientation="Horizontal">
                  <Button x:Name="PyInstallBtn" Content="Install Python for me" Style="{StaticResource Primary}" Margin="0,0,8,6" Padding="14,0" MinWidth="0"/>
                  <Button x:Name="PythonOrgBtn" Content="Open python.org" Margin="0,0,8,6" Padding="14,0" MinWidth="0"/>
                  <Button x:Name="CheckAgainBtn" Content="Check again" Margin="0,0,0,6" Padding="14,0" MinWidth="0"/>
                </WrapPanel>
                <StackPanel x:Name="PyProgressPanel" Margin="0,10,0,0" Visibility="Collapsed">
                  <DockPanel>
                    <Button x:Name="PyStopBtn" DockPanel.Dock="Right" Content="Stop waiting" Margin="12,0,0,0" Visibility="Collapsed"/>
                    <Button x:Name="PyCancelBtn" DockPanel.Dock="Right" Content="Cancel" Margin="12,0,0,0"/>
                    <TextBlock x:Name="PyStatus" Style="{StaticResource Body}" VerticalAlignment="Center"/>
                  </DockPanel>
                  <ProgressBar x:Name="PyBar" Margin="0,10,0,0" AutomationProperties.Name="Python download progress"/>
                </StackPanel>
                <TextBlock x:Name="PyError" Style="{StaticResource Body}" Foreground="{DynamicResource Bad}" Margin="0,8,0,0" Visibility="Collapsed"/>
                <TextBlock x:Name="PythonNote" Style="{StaticResource Muted}" Margin="0,8,0,0" Visibility="Collapsed"/>
                <!-- the technical facts and the manual route: collapsed, below the buttons -->
                <Expander x:Name="PyManual" Header="Details" Margin="0,10,0,0">
                  <StackPanel Margin="0,6,0,0">
                    <TextBlock x:Name="PyWhat" Style="{StaticResource Body}"/>
                    <TextBlock x:Name="PyLicence" Style="{StaticResource Muted}" Margin="0,6,0,0"/>
                    <TextBlock Text="Install it yourself instead" FontWeight="SemiBold" Margin="0,14,0,6"/>
                    <Grid>
                      <Grid.ColumnDefinitions><ColumnDefinition Width="26"/><ColumnDefinition Width="*"/></Grid.ColumnDefinitions>
                      <Grid.RowDefinitions><RowDefinition/><RowDefinition/><RowDefinition/><RowDefinition/></Grid.RowDefinitions>
                      <TextBlock Grid.Row="0" Text="1." FontWeight="SemiBold"/>
                      <TextBlock Grid.Row="0" Grid.Column="1" Style="{StaticResource Body}" Margin="0,0,0,4"
                                 Text="Choose Open python.org."/>
                      <TextBlock Grid.Row="1" Text="2." FontWeight="SemiBold"/>
                      <TextBlock Grid.Row="1" Grid.Column="1" Style="{StaticResource Body}" Margin="0,0,0,4"
                                 Text="Download the &quot;Windows installer (64-bit)&quot; for Python 3.12 (not the Microsoft Store version)."/>
                      <TextBlock Grid.Row="2" Text="3." FontWeight="SemiBold"/>
                      <TextBlock Grid.Row="2" Grid.Column="1" Style="{StaticResource Body}" Margin="0,0,0,4"
                                 Text="Run it and leave &quot;tcl/tk and IDLE&quot; and &quot;py launcher&quot; ticked (they are by default)."/>
                      <TextBlock Grid.Row="3" Text="4." FontWeight="SemiBold"/>
                      <TextBlock Grid.Row="3" Grid.Column="1" Style="{StaticResource Body}"
                                 Text="Come back here and choose Check again."/>
                    </Grid>
                    <TextBlock Text="What was looked at" FontWeight="SemiBold" Margin="0,14,0,6"/>
                    <TextBox x:Name="PythonNotes" Style="{StaticResource LogBox}" MaxHeight="120"
                             AutomationProperties.Name="What was looked at"/>
                  </StackPanel>
                </Expander>
              </StackPanel>
            </Border>

            <StackPanel x:Name="PlanPanel">
              <Border Style="{StaticResource Card}" Padding="0">
                <StackPanel>
                  <!-- program files -->
                  <Grid Margin="18,16,18,16">
                    <Grid.ColumnDefinitions><ColumnDefinition Width="48"/><ColumnDefinition Width="*"/><ColumnDefinition Width="Auto"/></Grid.ColumnDefinitions><Grid.RowDefinitions><RowDefinition Height="Auto"/><RowDefinition Height="Auto"/></Grid.RowDefinitions>
                    <Border Style="{StaticResource Tile}"><TextBlock Style="{StaticResource Glyph}" Text="&#xE74C;"/></Border>
                    <StackPanel Grid.Column="1">
                      <TextBlock Text="Program files" FontWeight="SemiBold"/>
                      <TextBlock x:Name="ProgramWhat" Style="{StaticResource Muted}"/>
                    </StackPanel>
                    <DockPanel Grid.Row="1" Grid.Column="1" Grid.ColumnSpan="2" Margin="0,8,0,0">
                      <Button x:Name="ProgramOpen" DockPanel.Dock="Right" Style="{StaticResource Link}" Content="Open folder" Margin="12,3,0,0" VerticalAlignment="Top"/>
                      <TextBox x:Name="ProgramPath" Style="{StaticResource PathBox}" AutomationProperties.Name="Program files folder"/>
                    </DockPanel>
                    <TextBlock x:Name="ProgramSize" Grid.Column="2" Style="{StaticResource Muted}" Margin="16,1,0,0" TextAlignment="Right"/>
                  </Grid>
                  <Border Style="{StaticResource Rule}"/>
                  <!-- environment -->
                  <Grid Margin="18,16,18,16">
                    <Grid.ColumnDefinitions><ColumnDefinition Width="48"/><ColumnDefinition Width="*"/><ColumnDefinition Width="Auto"/></Grid.ColumnDefinitions><Grid.RowDefinitions><RowDefinition Height="Auto"/><RowDefinition Height="Auto"/></Grid.RowDefinitions>
                    <Border Style="{StaticResource Tile}"><TextBlock Style="{StaticResource Glyph}" Text="&#xE943;"/></Border>
                    <StackPanel Grid.Column="1">
                      <TextBlock Text="Python environment and libraries" FontWeight="SemiBold" TextWrapping="Wrap"/>
                      <TextBlock x:Name="EnvWhat" Style="{StaticResource Muted}"/>
                    </StackPanel>
                    <DockPanel Grid.Row="1" Grid.Column="1" Grid.ColumnSpan="2" Margin="0,8,0,0">
                      <Button x:Name="EnvOpen" DockPanel.Dock="Right" Style="{StaticResource Link}" Content="Open folder" Margin="12,3,0,0" VerticalAlignment="Top"/>
                      <TextBox x:Name="EnvPath" Style="{StaticResource PathBox}" AutomationProperties.Name="Python environment folder"/>
                    </DockPanel>
                    <TextBlock x:Name="EnvSize" Grid.Column="2" Style="{StaticResource Muted}" Margin="16,1,0,0" TextAlignment="Right"/>
                  </Grid>
                  <!-- NVIDIA runtime -->
                  <StackPanel x:Name="NvidiaRow">
                    <Border Style="{StaticResource Rule}"/>
                    <Grid Margin="18,16,18,16">
                      <Grid.ColumnDefinitions><ColumnDefinition Width="48"/><ColumnDefinition Width="*"/><ColumnDefinition Width="Auto"/></Grid.ColumnDefinitions><Grid.RowDefinitions><RowDefinition Height="Auto"/><RowDefinition Height="Auto"/></Grid.RowDefinitions>
                      <Border Style="{StaticResource Tile}"><TextBlock Style="{StaticResource Glyph}" Text="&#xE950;"/></Border>
                      <StackPanel Grid.Column="1">
                        <CheckBox x:Name="NvidiaBox" IsChecked="True" AutomationProperties.Name="Install the NVIDIA runtime">
                          <TextBlock Text="NVIDIA runtime" FontWeight="SemiBold"/>
                        </CheckBox>
                        <TextBlock x:Name="NvidiaTitle" Text="NVIDIA runtime" FontWeight="SemiBold" Visibility="Collapsed"/>
                        <TextBlock x:Name="NvidiaWhat" Style="{StaticResource Muted}" Margin="30,4,0,0"/>
                        <TextBlock x:Name="NvidiaOff" Style="{StaticResource Muted}" Margin="30,4,0,0"
                                   Text="Without it, speech is recognised on the processor, which is slower. You can run the installer again later to add it."/>
                      </StackPanel>
                      <TextBlock x:Name="NvidiaSize" Grid.Column="2" Style="{StaticResource Muted}" Margin="16,1,0,0" TextAlignment="Right"/>
                    </Grid>
                  </StackPanel>
                  <Border Style="{StaticResource Rule}"/>
                  <!-- speech model -->
                  <Grid Margin="18,16,18,16">
                    <Grid.ColumnDefinitions><ColumnDefinition Width="48"/><ColumnDefinition Width="*"/><ColumnDefinition Width="Auto"/></Grid.ColumnDefinitions><Grid.RowDefinitions><RowDefinition Height="Auto"/><RowDefinition Height="Auto"/></Grid.RowDefinitions>
                    <Border Style="{StaticResource Tile}"><TextBlock Style="{StaticResource Glyph}" Text="&#xE720;"/></Border>
                    <StackPanel Grid.Column="1">
                      <CheckBox x:Name="ModelBox" IsChecked="True" AutomationProperties.Name="Download the speech model now">
                        <TextBlock Text="Download the speech model now" FontWeight="SemiBold" TextWrapping="Wrap"/>
                      </CheckBox>
                      <TextBlock x:Name="ModelTitle" Text="Speech model" FontWeight="SemiBold" Visibility="Collapsed"/>
                      <TextBlock x:Name="ModelWhat" Style="{StaticResource Muted}" Margin="30,4,0,0"/>
                      <TextBlock x:Name="ModelOff" Style="{StaticResource Muted}" Margin="30,4,0,0" Foreground="{DynamicResource Warn}"
                                 Text="If you leave this off, the first start downloads it and shows Getting ready for many minutes."/>
                    </StackPanel>
                    <TextBlock x:Name="ModelSize" Grid.Column="2" Style="{StaticResource Muted}" Margin="16,1,0,0" TextAlignment="Right"/>
                  </Grid>
                  <Border Style="{StaticResource Rule}"/>
                  <!-- your data -->
                  <Grid Margin="18,16,18,16">
                    <Grid.ColumnDefinitions><ColumnDefinition Width="48"/><ColumnDefinition Width="*"/><ColumnDefinition Width="Auto"/></Grid.ColumnDefinitions><Grid.RowDefinitions><RowDefinition Height="Auto"/><RowDefinition Height="Auto"/></Grid.RowDefinitions>
                    <Border Style="{StaticResource Tile}"><TextBlock Style="{StaticResource Glyph}" Text="&#xE8F1;"/></Border>
                    <StackPanel Grid.Column="1">
                      <TextBlock Text="Your data" FontWeight="SemiBold"/>
                      <TextBlock Style="{StaticResource Muted}" Text="Settings, history and logs. Kept as it is."/>
                    </StackPanel>
                    <DockPanel Grid.Row="1" Grid.Column="1" Grid.ColumnSpan="2" Margin="0,8,0,0">
                      <Button x:Name="DataOpen" DockPanel.Dock="Right" Style="{StaticResource Link}" Content="Open folder" Margin="12,3,0,0" VerticalAlignment="Top"/>
                      <TextBox x:Name="DataPath" Style="{StaticResource PathBox}" AutomationProperties.Name="Your data folder"/>
                    </DockPanel>
                    <TextBlock Grid.Column="2" Style="{StaticResource Muted}" Margin="16,1,0,0" TextAlignment="Right" Text="kept"/>
                  </Grid>
                  <Border Style="{StaticResource Rule}"/>
                  <!-- shortcuts -->
                  <Grid Margin="18,16,18,16">
                    <Grid.ColumnDefinitions><ColumnDefinition Width="48"/><ColumnDefinition Width="*"/><ColumnDefinition Width="Auto"/></Grid.ColumnDefinitions><Grid.RowDefinitions><RowDefinition Height="Auto"/><RowDefinition Height="Auto"/></Grid.RowDefinitions>
                    <Border Style="{StaticResource Tile}"><TextBlock Style="{StaticResource Glyph}" Text="&#xE71B;"/></Border>
                    <StackPanel Grid.Column="1">
                      <TextBlock Text="Shortcuts" FontWeight="SemiBold"/>
                      <TextBlock Style="{StaticResource Muted}" Text="In the Start menu (always)."/>
                      <CheckBox x:Name="DesktopBox" IsChecked="True" Margin="0,8,0,0" Content="Desktop shortcut"/>
                    </StackPanel>
                  </Grid>
                </StackPanel>
              </Border>

              <!-- totals -->
              <Grid Margin="0,14,0,0">
                <Grid.ColumnDefinitions>
                  <ColumnDefinition Width="*"/><ColumnDefinition Width="10"/>
                  <ColumnDefinition Width="*"/><ColumnDefinition Width="10"/>
                  <ColumnDefinition Width="*"/>
                </Grid.ColumnDefinitions>
                <Border Style="{StaticResource Card}" Padding="14,10">
                  <StackPanel>
                    <TextBlock Style="{StaticResource Muted}" FontSize="12.5" Text="To download"/>
                    <TextBlock x:Name="TotalDownload" FontSize="17" FontWeight="SemiBold" FontFamily="{StaticResource Display}" TextWrapping="Wrap"/>
                  </StackPanel>
                </Border>
                <Border Grid.Column="2" Style="{StaticResource Card}" Padding="14,10">
                  <StackPanel>
                    <TextBlock Style="{StaticResource Muted}" FontSize="12.5" Text="Disk space needed"/>
                    <TextBlock x:Name="TotalDisk" FontSize="17" FontWeight="SemiBold" FontFamily="{StaticResource Display}" TextWrapping="Wrap"/>
                  </StackPanel>
                </Border>
                <Border Grid.Column="4" Style="{StaticResource Card}" Padding="14,10">
                  <StackPanel>
                    <TextBlock x:Name="FreeLabel" Style="{StaticResource Muted}" FontSize="12.5"/>
                    <TextBlock x:Name="TotalFree" FontSize="17" FontWeight="SemiBold" FontFamily="{StaticResource Display}" TextWrapping="Wrap"/>
                  </StackPanel>
                </Border>
              </Grid>
            </StackPanel>

            <TextBlock x:Name="ChooseFoot" Style="{StaticResource Muted}" Margin="0,14,0,0"
                       Text="The install folder is fixed: like most desktop apps, Hold to talk installs into your own account. You can remove it later from Windows Settings &gt; Apps &gt; Installed apps."/>
            <TextBlock x:Name="ChooseNote" Style="{StaticResource Muted}" Margin="0,6,0,0" Visibility="Collapsed"/>
          </StackPanel>
        </ScrollViewer>

        <!-- 4. Installing -->
        <!-- (the title, the step line and the bar stay put; only the list and the details scroll) -->
        <Grid x:Name="PageInstall" Visibility="Collapsed">
          <Grid.RowDefinitions>
            <RowDefinition Height="Auto"/>
            <RowDefinition Height="*"/>
          </Grid.RowDefinitions>
          <StackPanel Margin="40,28,40,0">
            <TextBlock x:Name="InstallTitle" Text="Installing Hold to talk" Style="{StaticResource H2}"/>
            <TextBlock Style="{StaticResource Muted}" Margin="0,4,0,0"
                       Text="This takes a few minutes, longer when large downloads are needed. You can keep using your PC."/>
            <DockPanel Margin="0,18,0,0">
              <TextBlock x:Name="StepCount" DockPanel.Dock="Right" Style="{StaticResource Muted}"/>
              <TextBlock x:Name="StepNow" FontWeight="SemiBold" TextTrimming="CharacterEllipsis"/>
            </DockPanel>
            <ProgressBar x:Name="Bar" Margin="0,10,0,12" AutomationProperties.Name="Installation progress"/>
          </StackPanel>
          <ScrollViewer x:Name="InstallScroll" Grid.Row="1" VerticalScrollBarVisibility="Auto" Focusable="False">
            <StackPanel Margin="40,6,40,24">
              <Border Style="{StaticResource Card}" Padding="18,10">
                <StackPanel x:Name="StepList"/>
              </Border>
              <Expander x:Name="DetailsExp" Header="Show details" Margin="2,16,0,0">
                <TextBox x:Name="LogBox" Style="{StaticResource LogBox}" Height="170" AutomationProperties.Name="Installation details"/>
              </Expander>
            </StackPanel>
          </ScrollViewer>
        </Grid>

        <!-- 5. Result -->
        <ScrollViewer x:Name="PageResult" VerticalScrollBarVisibility="Auto" Focusable="False" Visibility="Collapsed">
          <StackPanel Margin="40,30,40,24">
            <Border x:Name="ResultBadge" Width="48" Height="48" CornerRadius="24" HorizontalAlignment="Left">
              <TextBlock x:Name="ResultGlyph" Style="{StaticResource Glyph}" FontSize="22"/>
            </Border>
            <TextBlock x:Name="ResultTitle" Style="{StaticResource H1}" Margin="0,16,0,0"/>
            <TextBlock x:Name="ResultLead" Style="{StaticResource Body}" Margin="0,6,0,0"/>
            <TextBox x:Name="ResultPath" Style="{StaticResource PathBox}" Margin="0,8,0,0" HorizontalAlignment="Left" AutomationProperties.Name="Install folder"/>

            <StackPanel x:Name="SuccessPanel">
              <StackPanel x:Name="WarnList"/>
              <Border Style="{StaticResource Card}" Margin="0,16,0,0">
                <StackPanel>
                  <TextBlock Style="{StaticResource H3}" Text="How to open it"/>
                  <Grid Margin="0,8,0,0">
                    <Grid.ColumnDefinitions><ColumnDefinition Width="28"/><ColumnDefinition Width="*"/></Grid.ColumnDefinitions>
                    <TextBlock Style="{StaticResource Glyph}" Text="&#xE700;" FontSize="17" VerticalAlignment="Top" HorizontalAlignment="Left" Margin="0,1,0,0"/>
                    <TextBlock Grid.Column="1" Style="{StaticResource Body}"
                               Text="Start menu: press the Windows key, type Hold to talk and press Enter."/>
                  </Grid>
                  <Grid x:Name="DesktopHow" Margin="0,6,0,0">
                    <Grid.ColumnDefinitions><ColumnDefinition Width="28"/><ColumnDefinition Width="*"/></Grid.ColumnDefinitions>
                    <TextBlock Style="{StaticResource Glyph}" Text="&#xE7F4;" FontSize="17" VerticalAlignment="Top" HorizontalAlignment="Left" Margin="0,1,0,0"/>
                    <TextBlock Grid.Column="1" Style="{StaticResource Body}" Text="Desktop: double-click the Hold to talk shortcut."/>
                  </Grid>
                  <TextBlock Style="{StaticResource Muted}" Margin="28,6,0,0"
                             Text="While it runs, its icon sits in the notification area at the bottom right of the screen."/>
                </StackPanel>
              </Border>
              <Border Style="{StaticResource Card}" Margin="0,10,0,0">
                <StackPanel>
                  <Grid>
                    <Grid.ColumnDefinitions><ColumnDefinition Width="28"/><ColumnDefinition Width="*"/></Grid.ColumnDefinitions>
                    <TextBlock Style="{StaticResource Glyph}" Text="&#xE720;" FontSize="17" VerticalAlignment="Top" HorizontalAlignment="Left" Margin="0,1,0,0"/>
                    <TextBlock Grid.Column="1" Style="{StaticResource Body}"
                               Text="If nothing is heard: in Settings &gt; Privacy &amp; security &gt; Microphone, turn on &quot;Let desktop apps access your microphone&quot;."/>
                  </Grid>
                  <Grid Margin="0,10,0,0">
                    <Grid.ColumnDefinitions><ColumnDefinition Width="28"/><ColumnDefinition Width="*"/></Grid.ColumnDefinitions>
                    <TextBlock Style="{StaticResource Glyph}" Text="&#xE945;" FontSize="17" VerticalAlignment="Top" HorizontalAlignment="Left" Margin="0,1,0,0"/>
                    <TextBlock Grid.Column="1" Style="{StaticResource Muted}" FontSize="13.5"
                               Text="Optional: AI cleanup. The app can tidy what you say (fillers, false starts) with a small AI model that runs on this PC through Ollama, about 4.3 GB: ollama pull gemma4:e2b-it-qat. The app works fine without it; Ollama is not installed for you."/>
                  </Grid>
                </StackPanel>
              </Border>
            </StackPanel>

            <StackPanel x:Name="FailPanel" Visibility="Collapsed">
              <TextBlock x:Name="FailHead" Style="{StaticResource H3}" Text="What the installer said" Margin="0,18,0,8"/>
              <TextBox x:Name="ErrorBox" Style="{StaticResource LogBox}" FontFamily="Segoe UI Variable Text, Segoe UI" FontSize="13.5" Foreground="{DynamicResource Text}"
                       MaxHeight="220" AutomationProperties.Name="Error message"/>
            </StackPanel>
          </StackPanel>
        </ScrollViewer>

        <!-- an error before anything was installed -->
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
            <Button x:Name="TextInstallerBtn" Content="Use the text installer instead" HorizontalAlignment="Left" Margin="0,16,0,0"/>
          </StackPanel>
        </ScrollViewer>
      </Grid>

      <!-- the button row: always visible -->
      <Border Grid.Row="1" BorderBrush="{DynamicResource Line}" BorderThickness="0,1,0,0" Background="{DynamicResource Bg}" Padding="24,14">
        <DockPanel LastChildFill="False">
          <Button x:Name="CancelBtn" DockPanel.Dock="Left" Content="Cancel"/>
          <Button x:Name="CopyBtn" DockPanel.Dock="Left" Content="Copy details" Visibility="Collapsed"/>
          <CheckBox x:Name="StartNow" DockPanel.Dock="Left" VerticalAlignment="Center" IsChecked="True" Visibility="Collapsed"
                    Content="Start Hold to talk now"/>
          <Button x:Name="NextBtn" DockPanel.Dock="Right" Style="{StaticResource Primary}" Content="Next" IsDefault="True"/>
          <Button x:Name="BackBtn" DockPanel.Dock="Right" Content="Back" Margin="0,0,10,0"/>
        </DockPanel>
      </Border>
    </Grid>

    <!-- "Stop installing?" -->
    <Grid x:Name="Overlay" Grid.ColumnSpan="2" Visibility="Collapsed">
      <Rectangle Fill="#000000" Opacity="0.38"/>
      <Border Background="{DynamicResource Surface}" BorderBrush="{DynamicResource Line2}" BorderThickness="1" CornerRadius="14"
              Width="430" Padding="26,22" HorizontalAlignment="Center" VerticalAlignment="Center">
        <Border.Effect><DropShadowEffect BlurRadius="28" ShadowDepth="6" Opacity="0.28" Color="#000000"/></Border.Effect>
        <StackPanel>
          <TextBlock x:Name="StopTitle" Style="{StaticResource H3}" FontSize="18" FontFamily="{StaticResource Display}" Text="Stop installing?"/>
          <TextBlock x:Name="StopBody" Style="{StaticResource Body}" Margin="0,8,0,0"
                     Text="What is already done stays on this PC, and nothing is broken. Running the installer again carries on where it stopped."/>
          <StackPanel Orientation="Horizontal" HorizontalAlignment="Right" Margin="0,20,0,0">
            <Button x:Name="StopBtn" Content="Stop installing" Margin="0,0,10,0"/>
            <Button x:Name="KeepBtn" Content="Keep installing" Style="{StaticResource Primary}"/>
          </StackPanel>
        </StackPanel>
      </Border>
    </Grid>
  </Grid>
</Window>
'@

$RailLabels = 'Welcome', 'Terms of Use', 'Choices', 'Installing', 'Finish'
$RailXaml = ''
for ($i = 1; $i -le 5; $i++) {
    $up = ''; $down = ''
    if ($i -gt 1) { $up = '<Border Width="2" Height="9" VerticalAlignment="Top" HorizontalAlignment="Center" Background="{DynamicResource Line2}"/>' }
    if ($i -lt 5) { $down = '<Border Width="2" Height="9" VerticalAlignment="Bottom" HorizontalAlignment="Center" Background="{DynamicResource Line2}"/>' }
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

# -- state ---------------------------------------------------------------------------

$script:Harness = $null          # 'selftest' / 'shots' / 'autorun' (tests only)
$script:Facts = $null
$script:TermsOk = $false
$script:TermsVersion = $null
$script:TermsProblem = $null
$script:TermsPath = Join-Path $Here 'TERMS.md'
$script:Page = 'welcome'
$script:Mode = 'install'        # install / update / repair: decided from the facts each time they are loaded
$script:Run = $null
$script:Checking = $false
$script:CheckLog = New-Object Collections.Generic.List[string]     # everything -Check printed
$script:CheckOut = New-Object Collections.Generic.List[string]     # ...only its standard output
$script:FullLog = New-Object Collections.Generic.List[string]      # the kept log: the last $MaxLogLines lines
$script:LogPending = New-Object Collections.Generic.List[string]
$script:MaxLogLines = 4000
$script:MaxSteps = 30
$script:StepRows = @()
$script:StepTotal = 0
$script:CurrentStep = 0
$script:StepStarted = $null
$script:Warnings = @()
$script:ErrorText = $null
$script:DoneInfo = $null
$script:StopRequested = $false
$script:Outcome = $null          # exit code once an install was attempted
$script:ResultKind = $null       # success / failure / stopped
$script:ExitCode = $null
$script:AllowClose = $false
$script:ClosedWith = $null       # selftest: what Close-Window was asked for
$script:EngineCalls = New-Object Collections.Generic.List[object]
$script:ModelDefaultName = $null
$script:ThemeMode = 'light'
$script:Ready = $false

function Test-TestMode {
    if ($script:GivenOverrides.Count -gt 0) { return $true }
    if ($script:Facts -and $script:Facts.testMode -eq $true) { return $true }
    return ($null -ne $script:Harness)
}

# -- small helpers -------------------------------------------------------------------

function Set-Res($Element, $Property, [string]$Key) { $Element.SetResourceReference($Property, $Key) }

function Format-MB([double]$MB) {
    if ($MB -ge 995) { return [string]::Format($Inv, '{0:0.0} GB', $MB / 1000) }
    if ($MB -ge 10) { return [string]::Format($Inv, '{0:0} MB', $MB) }
    if ($MB -ge 1) { return [string]::Format($Inv, '{0:0.0} MB', $MB) }
    return 'under 1 MB'
}

function Format-Bytes([double]$Bytes) {
    if ($Bytes -ge 1e9) { return [string]::Format($Inv, '{0:0} GB', $Bytes / 1e9) }
    return Format-MB ($Bytes / 1e6)
}

function Get-Num($Value, [double]$Default = 0) {
    try { if ($null -ne $Value -and "$Value" -ne '') { return [double]$Value } } catch { }
    return $Default
}

function Set-Shown($Element, [bool]$Shown) {
    if ($Shown) { $Element.Visibility = 'Visible' } else { $Element.Visibility = 'Collapsed' }
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

# -- the engine as a child process -----------------------------------------------------
# Output is read with ReadLineAsync and collected by a DispatcherTimer on the
# window's own thread: no .NET event fires on another thread, so no script
# block ever runs without a runspace, and the window never waits on a read.

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
    $psi.WorkingDirectory = Split-Path -Parent $script:Engine
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
            # A grandchild can keep the pipes open: give it 3 s after the
            # engine itself has gone.
            if (-not $r.Exited) { $r.Exited = Get-Date }
            elseif (((Get-Date) - $r.Exited).TotalSeconds -gt 3) { $finished = $true }
        }
        if ($finished) {
            $r.Proc.WaitForExit()
            $code = $r.Proc.ExitCode
            $script:Run = $null
            if ($r.Kind -eq 'progress') { Complete-Install $code } else { Complete-Check $code }
        } elseif ($r.Kind -eq 'progress') {
            Update-Busy
        }
    }
    Flush-Log
    if ($script:Py) { Step-PythonRun }
    if (-not $script:Run -and -not $script:Py -and -not $script:AutoTimerNeeded) { $script:Timer.Stop() }
}

function Get-EngineDescendants([int]$RootId, $Since) {
    # The processes below the engine, recorded as (id, start time): found by
    # parent id, recursively, and only those created after the engine started.
    $found = New-Object Collections.Generic.List[object]
    if ($null -eq $Since) { return }
    try { $all = @(Get-CimInstance -ClassName Win32_Process -ErrorAction Stop) } catch { return }
    $ids = @{ $RootId = $true }
    $limit = $Since.AddSeconds(-1)
    $added = $true
    while ($added) {
        $added = $false
        foreach ($p in $all) {
            $pid2 = [int]$p.ProcessId
            if ($ids.ContainsKey($pid2) -or -not $ids.ContainsKey([int]$p.ParentProcessId)) { continue }
            if ($null -eq $p.CreationDate -or $p.CreationDate -lt $limit) { continue }
            $ids[$pid2] = $true
            $found.Add([pscustomobject]@{ Id = $pid2; Started = $p.CreationDate })
            $added = $true
        }
    }
    return $found.ToArray()
}

function Stop-EngineTree {
    # The engine and everything it started (pip, Python, a download): taskkill
    # for the tree, then whatever of the recorded tree is still there.
    $r = $script:Run
    if (-not $r) { return }
    $recorded = @()
    try {
        $since = $null
        try { $since = $r.Proc.StartTime } catch { }
        $recorded = @(Get-EngineDescendants $r.Proc.Id $since)
    } catch { }
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
    if ($recorded.Count -gt 0) {
        # Give the tree about a second to go; then end what is left of it. A
        # process counts only if it is the recorded one (same id, same start).
        $left = @()
        for ($t = 0; $t -lt 10; $t++) {
            Start-Sleep -Milliseconds 100
            $left = @($recorded | Where-Object { Test-RecordedAlive $_ })
            if ($left.Count -eq 0) { break }
        }
        Start-Sleep -Milliseconds 100
        foreach ($d in @($recorded | Where-Object { Test-RecordedAlive $_ })) {
            try { [Diagnostics.Process]::GetProcessById([int]$d.Id).Kill() } catch { }
        }
    }
}

function Test-RecordedAlive($Recorded) {
    try {
        $p = [Diagnostics.Process]::GetProcessById([int]$Recorded.Id)
        if ($p.HasExited) { return $false }
        return ([Math]::Abs(($p.StartTime - $Recorded.Started).TotalSeconds) -lt 2)
    } catch { return $false }
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

# -- the log (Show details) -------------------------------------------------------------

function Add-Log([string]$Line) {
    # The kept log (Show details, Copy details) is the last $MaxLogLines lines,
    # each at most 2000 characters: the oldest are dropped.
    if ($null -eq $Line) { $Line = '' }
    if ($Line.Length -gt 2000) { $Line = $Line.Substring(0, 2000) + ' ...' }
    $script:FullLog.Add($Line)
    if ($script:FullLog.Count -gt $script:MaxLogLines) {
        $script:FullLog.RemoveRange(0, $script:FullLog.Count - $script:MaxLogLines)
    }
    $script:LogPending.Add($Line)
    if ($script:LogPending.Count -gt $script:MaxLogLines) {
        $script:LogPending.RemoveRange(0, $script:LogPending.Count - $script:MaxLogLines)
    }
}

function Get-LogText {
    return ($script:FullLog -join "`r`n")
}

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

# -- pages and the button row ---------------------------------------------------------------

$PageNames = @{ welcome = 'PageWelcome'; terms = 'PageTerms'; choose = 'PageChoose'; install = 'PageInstall'; result = 'PageResult'; error = 'PageError' }
$PageRail = @{ welcome = 1; terms = 2; choose = 3; install = 4; result = 5 }

function Show-Page([string]$Name) {
    if ($Name -eq 'terms' -and -not $script:TermsOk) {
        Show-ErrorPage 'The Terms of Use could not be read' $script:TermsProblem $null
        return
    }
    foreach ($k in $PageNames.Keys) { Set-Shown $ui[$PageNames[$k]] ($k -eq $Name) }
    $script:Page = $Name
    Sync-Rail
    if ($Name -eq 'install') { $ui.InstallScroll.ScrollToTop() } else { $ui[$PageNames[$Name]].ScrollToTop() }
    Update-Nav
    if ($script:Harness -ne 'selftest') {
        $null = $ui.Root.Dispatcher.BeginInvoke([Action] {
            try {
                if ($script:Page -eq 'terms') { [void]$ui.TermsView.Focus() }
                elseif ($ui.NextBtn.IsVisible -and $ui.NextBtn.IsEnabled) { [void]$ui.NextBtn.Focus() }
                elseif ($ui.CancelBtn.IsVisible) { [void]$ui.CancelBtn.Focus() }
            } catch { }
        }, [Windows.Threading.DispatcherPriority]::Input)
    }
}

function Sync-Rail {
    if ($PageRail.ContainsKey($script:Page)) {
        if ($script:Page -eq 'result' -and $script:ResultKind -and $script:ResultKind -ne 'success') {
            # Failed or stopped: step 4 shows it, step 5 was never reached.
            Update-Rail 4 $script:ResultKind
        } else { Update-Rail $PageRail[$script:Page] }
    }
}

function Update-Rail([int]$Current, [string]$Problem = '') {
    # $Problem 'failure' or 'stopped': step $Current did not finish and shows
    # a "!" (Bad / Warn colour) instead of a tick; later steps stay pending.
    for ($i = 1; $i -le 5; $i++) {
        $dot = $ui["RailDot$i"]; $num = $ui["RailNum$i"]; $txt = $ui["RailText$i"]
        if ($i -eq $Current -and $Problem) {
            $ink = 'Bad'; $soft = 'BadSoft'
            if ($Problem -eq 'stopped') { $ink = 'Warn'; $soft = 'WarnSoft' }
            Set-Res $dot ([Windows.Controls.Border]::BackgroundProperty) $soft
            Set-Res $dot ([Windows.Controls.Border]::BorderBrushProperty) $ink
            $num.Text = '!'
            $num.ClearValue([Windows.Controls.TextBlock]::FontFamilyProperty)
            $num.FontSize = 13
            $num.FontWeight = 'Bold'
            Set-Res $num ([Windows.Controls.TextBlock]::ForegroundProperty) $ink
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
        if ($i -eq $Current -and $Problem -eq 'stopped') { [Windows.Automation.AutomationProperties]::SetName($txt, "Step $i of 5, stopped: $($RailLabels[$i - 1])") }
        elseif ($i -eq $Current -and $Problem) { [Windows.Automation.AutomationProperties]::SetName($txt, "Step $i of 5, failed: $($RailLabels[$i - 1])") }
        elseif ($i -eq $Current) { [Windows.Automation.AutomationProperties]::SetName($txt, "Step $i of 5, current: $($RailLabels[$i - 1])") }
        else { [Windows.Automation.AutomationProperties]::SetName($txt, "Step $i of 5: $($RailLabels[$i - 1])") }
    }
}

function Test-CanInstall {
    if (-not $script:Facts -or $script:Checking -or $script:Run) { return $false }
    if (-not $script:TermsOk) { return $false }
    if ($script:Facts.python.ok -ne $true) { return $false }
    return ($ui.AgreeBox.IsChecked -eq $true)
}

function Update-Nav {
    $b = $ui
    $b.CopyBtn.Visibility = 'Collapsed'
    # "Start Hold to talk now" sits in the button row, on the success page only
    # (never in test mode).
    Set-Shown $b.StartNow (($script:Page -eq 'result') -and ($script:ResultKind -eq 'success') -and -not (Test-TestMode))
    $b.CancelBtn.Visibility = 'Visible'
    $b.CancelBtn.Content = 'Cancel'
    $b.BackBtn.Content = 'Back'
    $b.NextBtn.Visibility = 'Visible'
    $b.NextBtn.IsEnabled = $true
    switch ($script:Page) {
        'welcome' {
            $b.BackBtn.Visibility = 'Collapsed'
            $b.NextBtn.Content = 'Next'
            $b.NextBtn.IsEnabled = ($null -ne $script:Facts) -and -not $script:Checking
        }
        'terms' {
            $b.BackBtn.Visibility = 'Visible'
            $b.NextBtn.Content = 'I agree'
            $b.NextBtn.IsEnabled = $script:TermsOk -and ($b.AgreeBox.IsChecked -eq $true)
        }
        'choose' {
            $b.BackBtn.Visibility = 'Visible'
            $b.BackBtn.IsEnabled = -not $script:Checking -and -not $script:Py
            $b.NextBtn.Content = (Get-ModeWords).Verb
            $b.NextBtn.IsEnabled = Test-CanInstall
        }
        'install' {
            $b.BackBtn.Visibility = 'Collapsed'
            $b.NextBtn.Visibility = 'Collapsed'
            $b.CancelBtn.IsEnabled = -not $script:StopRequested
        }
        'result' {
            $b.CancelBtn.Visibility = 'Collapsed'
            $b.NextBtn.Content = 'Close'
            if ($script:ResultKind -eq 'success') {
                $b.BackBtn.Visibility = 'Collapsed'
            } else {
                $b.CopyBtn.Visibility = 'Visible'
                $b.CopyBtn.Content = 'Copy details'
                $b.BackBtn.Visibility = 'Visible'
                $b.BackBtn.Content = 'Try again'
            }
        }
        'error' {
            $b.CancelBtn.Visibility = 'Collapsed'
            $b.BackBtn.Visibility = 'Collapsed'
            $b.NextBtn.Content = 'Close'
            if ($ui.ErrDetailsPanel.Visibility -eq 'Visible') { $b.CopyBtn.Visibility = 'Visible'; $b.CopyBtn.Content = 'Copy details' }
        }
    }
    if ($script:Page -ne 'install') { $b.CancelBtn.IsEnabled = $true }
    if ($script:Page -ne 'choose') { $b.BackBtn.IsEnabled = $true }
}

function Show-ErrorPage([string]$Title, [string]$Text, [string]$Details) {
    $ui.ErrTitle.Text = $Title
    $ui.ErrText.Text = $Text
    $ui.ErrDetails.Text = [string]$Details
    Set-Shown $ui.ErrDetailsPanel (-not [string]::IsNullOrWhiteSpace($Details))
    Set-Shown $ui.TextInstallerBtn (-not $FromSetupExe -and -not (Test-TestMode) -and (Test-Path -LiteralPath (Join-Path $Here 'install.bat')))
    if ($null -eq $script:Outcome) { $script:Outcome = 1 }
    foreach ($k in $PageNames.Keys) { Set-Shown $ui[$PageNames[$k]] ($k -eq 'error') }
    $script:Page = 'error'
    Update-Nav
}

# -- -Check and the facts --------------------------------------------------------------------

function Start-Check([bool]$Again) {
    $script:Checking = $true
    $script:CheckAgain = $Again
    $script:CheckLog.Clear()
    $script:CheckOut.Clear()
    $script:PendingFacts = $null
    Set-Shown $ui.CheckingRow (-not $Again)
    Set-Shown $ui.RecheckRow $Again
    $ui.CheckAgainBtn.IsEnabled = $false
    $ui.PyInstallBtn.IsEnabled = $false
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
    Set-Shown $ui.RecheckRow $false
    Update-PythonUi
    $facts = $script:PendingFacts
    if ($Code -ne 0 -or $null -eq $facts) {
        $details = ($script:CheckLog | Select-Object -Last 40) -join "`r`n"
        $why = 'Nothing on this PC was changed.'
        foreach ($l in $script:CheckOut) {          # standard output only
            $m = ConvertFrom-Marker $l
            if ($m -and $m.kind -eq 'error' -and $m.text) { $why = ([string]$m.text -replace "`r?`n", "`r`n") + "`r`n`r`nNothing on this PC was changed." }
        }
        if (-not (Test-Path -LiteralPath $script:Engine)) {
            $why = 'The file install.ps1 is missing next to the installer. ' + (Get-DownloadAdvice $false) + ' Nothing on this PC was changed.'
        }
        Show-ErrorPage 'The installer could not check this PC' $why $details
        return
    }
    if ($script:Harness -eq 'autorun' -and $facts.testMode -ne $true) {
        Show-ErrorPage 'Refused' 'The automatic test run only works in test mode.' $null
        return
    }
    Set-Facts $facts
    if ($script:CheckAgain) {
        $ui.PythonNote.Text = 'Checked again just now.'
        Set-Shown $ui.PythonNote ($facts.python.ok -ne $true)
    }
    Update-Nav
}

# -- install, update or repair ------------------------------------------------------------------
# One notion of mode, decided from the facts every time they are (re)loaded:
# install = nothing is installed yet; update = another version is installed;
# repair = this very version is installed. Install mode keeps the original wording.

function Get-Mode($Facts) {
    if (-not $Facts -or -not $Facts.installedVersion) { return 'install' }
    if ([string]$Facts.installedVersion -eq [string]$Facts.version) { return 'repair' }
    return 'update'
}

function Get-ModeWords {
    # Verb: the button and the window title; Ger: the rail step and page title;
    # Past: "is installed"; Noun / NounCap: "the installation" / "Installation";
    # Tool: what "carries on where it stopped".
    switch ($script:Mode) {
        'update' { return @{ Verb = 'Update'; Ger = 'Updating'; Past = 'updated'; Noun = 'update'; NounCap = 'Update'; Tool = 'update'; Rail = 'Update'; FailHead = 'What the updater said' } }
        'repair' { return @{ Verb = 'Repair'; Ger = 'Repairing'; Past = 'repaired'; Noun = 'repair'; NounCap = 'Repair'; Tool = 'repair'; Rail = 'Repair'; FailHead = 'What the updater said' } }
        default { return @{ Verb = 'Install'; Ger = 'Installing'; Past = 'installed'; Noun = 'installation'; NounCap = 'Installation'; Tool = 'installer'; Rail = 'Setup'; FailHead = 'What the installer said' } }
    }
}

function Update-ModeText {
    $f = $script:Facts
    $script:Mode = Get-Mode $f
    $w = Get-ModeWords
    $script:Window.Title = "$($w.Verb) Hold to talk"
    $ui.RailSub.Text = $w.Rail
    $script:RailLabels[3] = $w.Ger
    $ui.RailText4.Text = $w.Ger
    $ui.ChooseTitle.Text = "What will be $($w.Past)"
    $ui.ChooseSub.Text = "Everything goes into your own Windows account. Nothing changes until you choose $($w.Verb)."
    $ui.InstallTitle.Text = "$($w.Ger) Hold to talk"
    [Windows.Automation.AutomationProperties]::SetName($ui.Bar, "$($w.NounCap) progress")
    [Windows.Automation.AutomationProperties]::SetName($ui.LogBox, "$($w.NounCap) details")
    [Windows.Automation.AutomationProperties]::SetName($ui.ResultPath, $(if ($script:Mode -eq 'install') { 'Install folder' } else { 'App folder' }))
    $ui.FailHead.Text = $w.FailHead
    $ui.StopTitle.Text = "Stop $($w.Ger.ToLower())?"
    $ui.StopBody.Text = "What is already done stays on this PC, and nothing is broken. Running the $($w.Tool) again carries on where it stopped."
    $ui.StopBtn.Content = "Stop $($w.Ger.ToLower())"
    $ui.KeepBtn.Content = "Keep $($w.Ger.ToLower())"
    if ($script:Mode -eq 'install') {
        $ui.UpdateCard.Visibility = 'Collapsed'
        $ui.AccountText.Text = 'Installs for your Windows account only. No administrator rights needed.'
        $ui.ChooseFoot.Text = 'The install folder is fixed: like most desktop apps, Hold to talk installs into your own account. You can remove it later from Windows Settings > Apps > Installed apps.'
    } else {
        if ($script:Mode -eq 'repair') {
            $ui.UpdateText.Text = "Version $($f.version) is already installed. This installs it again to repair it; your settings and history are kept."
        } else {
            $ui.UpdateText.Text = "This updates Hold to talk from version $($f.installedVersion) to $($f.version). It does not install a second copy: your settings and history are kept."
        }
        $ui.UpdateCard.Visibility = 'Visible'
        $ui.AccountText.Text = 'Applies to your Windows account only. No administrator rights needed.'
        $ui.ChooseFoot.Text = 'The folder is fixed: the app stays where it is installed, in your own account. You can remove it later from Windows Settings > Apps > Installed apps.'
    }
    $ui.AccountCard.Margin = $(if ($script:Mode -eq 'install') { '0,24,0,0' } else { '0,12,0,0' })
    Sync-Rail
    Update-Nav
}

function Set-Facts($Facts) {
    $script:Facts = $Facts
    $f = $Facts
    $ui.WelcomeVersion.Text = "Version $($f.version)"
    $ui.RailVersion.Text = "Version $($f.version)"
    Update-ModeText
    Set-Shown $ui.TestBadge ($f.testMode -eq $true)
    Read-Terms
    # page 3
    $L = $f.layout
    $ui.ProgramPath.Text = [string]$L.installRoot
    $ui.EnvPath.Text = [string]$L.envDir
    $ui.DataPath.Text = [string]$L.dataDir
    foreach ($pair in @(@('ProgramOpen', $L.installRoot), @('EnvOpen', $L.envDir), @('DataOpen', $L.dataDir))) {
        $exists = $false
        if ($script:Harness -ne 'shots') { try { $exists = [bool]$pair[1] -and (Test-Path -LiteralPath $pair[1] -PathType Container) } catch { } }
        Set-Shown $ui[$pair[0]] $exists
    }
    $count = [int](Get-Num $f.files.count)
    $ui.ProgramWhat.Text = "The app itself: $count files."
    $ui.ProgramSize.Text = Format-Bytes (Get-Num $f.files.sizeBytes)
    $gpu = ($f.gpu.found -eq $true)
    Set-Shown $ui.NvidiaRow $gpu
    $ui.NvidiaBox.IsChecked = $gpu
    # Already installed: no box (the engine never removes the runtime, so
    # unticking would do nothing).
    $have = $gpu -and ($f.gpu.runtimeInstalled -eq $true)
    Set-Shown $ui.NvidiaBox (-not $have)
    Set-Shown $ui.NvidiaTitle $have
    if ($have) {
        $ui.NvidiaWhat.Margin = '0,2,0,0'
        $ui.NvidiaWhat.Text = 'Already installed. It lets your NVIDIA graphics card recognise speech, which is much faster.'
    } else {
        $ui.NvidiaWhat.Margin = '30,4,0,0'
        $ui.NvidiaWhat.Text = "Lets your NVIDIA graphics card recognise speech, which is much faster. About $(Format-MB (Get-Num $f.estimates.nvidiaDownloadMB 1300)) to download, about $(Format-MB (Get-Num $f.estimates.nvidiaDiskMB 2000)) on disk. Installing it means accepting NVIDIA's licence for those files."
    }
    $script:ModelDefaultName = if ($gpu) { [string]$f.model.name } else { [string]$f.model.cpuName }
    $ui.ModelBox.IsChecked = $true
    $ui.DesktopBox.IsChecked = $true
    $ui.FreeLabel.Text = "Free on $($f.disk.drive)"
    # Python
    $pyOk = ($f.python.ok -eq $true)
    Set-Shown $ui.PythonPanel (-not $pyOk)
    Set-Shown $ui.PlanPanel $pyOk
    if (-not $pyOk) {
        Set-PythonTexts
        $notes = @($f.python.notes | ForEach-Object { [string]$_ })
        $ui.PythonNotes.Text = ($notes -join "`r`n")
    }
    Update-Plan
}

function Get-Choice {
    $f = $script:Facts
    $gpu = ($f.gpu.found -eq $true)
    $nv = $gpu -and ($ui.NvidiaBox.IsChecked -eq $true)
    $modelName = if ($nv) { [string]$f.model.name } else { [string]$f.model.cpuName }
    $modelMB = if ($nv) { Get-Num $f.model.downloadMB 1500 } else { Get-Num $f.model.cpuDownloadMB 500 }
    $present = ($f.model.present -eq $true) -and ($modelName -eq $script:ModelDefaultName)
    $recorded = ($f.gpu.runtimeInstalled -eq $true)
    $libsCurrent = ($f.env.depsCurrent -eq $true) -and ((-not $gpu) -or ($nv -eq $recorded))
    [pscustomobject]@{
        Nvidia = $nv; ModelName = $modelName; ModelMB = $modelMB; ModelPresent = $present
        Model = ($present -or ($ui.ModelBox.IsChecked -eq $true))
        Desktop = ($ui.DesktopBox.IsChecked -eq $true); LibsCurrent = $libsCurrent; Gpu = $gpu
        NvidiaNew = $nv -and -not $recorded
    }
}

function Update-Plan {
    $f = $script:Facts
    if (-not $f) { return }
    $c = Get-Choice
    $est = $f.estimates
    $libsDl = Get-Num $est.libsDownloadMB; $libsDisk = Get-Num $est.libsDiskMB
    $dl = 0.0; $disk = (Get-Num $f.files.sizeBytes) / 1e6; $unknown = $false
    if ($c.LibsCurrent) {
        $ui.EnvWhat.Text = 'A private copy of Python for this app. Already installed, up to date.'
        $ui.EnvSize.Text = 'up to date'
    } else {
        $ui.EnvWhat.Text = 'A private copy of Python for this app, with the libraries it needs.'
        if ($libsDl -gt 0) {
            $ui.EnvSize.Text = "$(Format-MB $libsDl) download`n$(Format-MB $libsDisk) on disk"
            $dl += $libsDl; $disk += $libsDisk
        } else {
            $ui.EnvSize.Text = 'a few hundred MB'
            $unknown = $true
        }
    }
    if ($c.Gpu) {
        Set-Shown $ui.NvidiaOff (-not $c.Nvidia)
        if ($c.NvidiaNew) {
            $ndl = Get-Num $est.nvidiaDownloadMB 1300; $ndisk = Get-Num $est.nvidiaDiskMB 2000
            $ui.NvidiaSize.Text = "$(Format-MB $ndl) download`n$(Format-MB $ndisk) on disk"
            $dl += $ndl; $disk += $ndisk
        } elseif ($c.Nvidia) { $ui.NvidiaSize.Text = 'installed' }
        else { $ui.NvidiaSize.Text = 'not installed' }
    }
    if ($c.ModelPresent) {
        Set-Shown $ui.ModelBox $false; Set-Shown $ui.ModelTitle $true; Set-Shown $ui.ModelOff $false
        $ui.ModelWhat.Margin = '0,2,0,0'
        $ui.ModelWhat.Text = "$($c.ModelName) is already on this PC."
        $ui.ModelSize.Text = 'on this PC'
    } else {
        Set-Shown $ui.ModelBox $true; Set-Shown $ui.ModelTitle $false
        $ui.ModelWhat.Margin = '30,4,0,0'
        $ui.ModelWhat.Text = "The model that turns your speech into text: $($c.ModelName), about $(Format-MB $c.ModelMB) to download."
        Set-Shown $ui.ModelOff (-not $c.Model)
        if ($c.Model) { $ui.ModelSize.Text = "$(Format-MB $c.ModelMB) download"; $dl += $c.ModelMB; $disk += $c.ModelMB }
        else { $ui.ModelSize.Text = 'later' }
    }
    $plus = ''
    if ($unknown) { $plus = ' + libraries' }
    if ($dl -lt 0.5 -and -not $unknown) { $ui.TotalDownload.Text = 'Nothing' } else { $ui.TotalDownload.Text = "About $(Format-MB $dl)$plus" }
    $ui.TotalDisk.Text = "About $(Format-MB $disk)$plus"
    $free = Get-Num $f.disk.freeBytes -1
    if ($free -ge 0) { $ui.TotalFree.Text = Format-Bytes $free } else { $ui.TotalFree.Text = 'not known' }
    $need = $disk * 1e6 * 1.1
    if ($free -ge 0 -and $free -lt $need -and $f.python.ok -eq $true) {
        $ui.DiskWarnText.Text = "There may not be enough free space on $($f.disk.drive) for these choices: about $(Format-MB $disk) is needed and $(Format-Bytes $free) is free. You can still try; free some space first if you can."
        $ui.DiskWarn.Visibility = 'Visible'
    } else { $ui.DiskWarn.Visibility = 'Collapsed' }
    Update-Nav
}

function Get-InstallArgs {
    $c = Get-Choice
    $a = @('-Progress')
    # The acceptance is passed only when the box is ticked at this moment.
    if ($ui.AgreeBox.IsChecked -eq $true -and $script:TermsOk) { $a += @('-AcceptedTerms', [string]$script:TermsVersion) }
    $a += @('-Nvidia', $(if ($c.Nvidia) { 'yes' } else { 'no' }))
    $a += @('-Model', $(if ($c.Model) { 'yes' } else { 'no' }))
    $a += @('-DesktopShortcut', $(if ($c.Desktop) { 'yes' } else { 'no' }))
    $a += Get-PassThroughArgs
    return , $a
}

# -- the Terms of Use ------------------------------------------------------------------------

function Read-Terms {
    # A tick given to another version of the terms (or to none) does not count.
    $want = [string]$script:Facts.termsVersion
    if (-not $script:TermsOk -or $script:TermsVersion -ne $want) { $ui.AgreeBox.IsChecked = $false }
    $script:TermsOk = $false
    $script:TermsProblem = $null
    if (-not (Test-Path -LiteralPath $script:TermsPath)) {
        $script:TermsProblem = 'The file TERMS.md is missing next to the installer, so the terms cannot be shown and nothing can be installed. ' + (Get-DownloadAdvice $false) + ' Nothing on this PC was changed.'
        return
    }
    try { $lines = [IO.File]::ReadAllLines($script:TermsPath, [Text.Encoding]::UTF8) }
    catch { $script:TermsProblem = "TERMS.md could not be read ($($_.Exception.Message)). Nothing on this PC was changed."; return }
    $first = ''
    if ($lines.Count) { $first = $lines[0].TrimStart([char]0xFEFF) }
    $m = [regex]::Match($first, '^Version (\S+) ')
    if (-not $want -or -not $m.Success -or $m.Groups[1].Value -ne $want) {
        $script:TermsProblem = 'TERMS.md is not the version of the Terms of Use that belongs to this program, so nothing can be installed. ' + (Get-DownloadAdvice $true) + ' Nothing on this PC was changed.'
        return
    }
    $ui.TermsView.Document = New-TermsDocument $lines
    $script:TermsVersion = $want
    $ui.AgreeText.Text = "I have read and agree to the Terms of Use (version $want)"
    [Windows.Automation.AutomationProperties]::SetName($ui.AgreeBox, "I have read and agree to the Terms of Use (version $want)")
    $ui.TermsLead.Text = "Please read the terms before you install. Version $want."
    $script:TermsOk = $true
}

function Add-MdInlines($Target, [string]$Text) {
    # **bold** as bold; everything else exactly as written.
    $parts = $Text -split '\*\*'
    for ($j = 0; $j -lt $parts.Count; $j++) {
        if ($parts[$j] -eq '') { continue }
        $run = New-Object Windows.Documents.Run($parts[$j])
        if ($j % 2 -eq 1) { $Target.Inlines.Add((New-Object Windows.Documents.Bold($run))) }
        else { $Target.Inlines.Add($run) }
    }
}

function New-TermsDocument([string[]]$Lines) {
    $doc = New-Object Windows.Documents.FlowDocument
    $doc.FontFamily = New-Object Windows.Media.FontFamily 'Segoe UI Variable Text, Segoe UI'
    $doc.FontSize = 13.5
    $doc.LineHeight = 20
    $doc.TextAlignment = 'Left'
    $doc.PagePadding = New-Object Windows.Thickness(22, 16, 22, 18)
    $doc.IsOptimalParagraphEnabled = $false
    $doc.IsHyphenationEnabled = $false
    $doc.Background = [Windows.Media.Brushes]::Transparent
    Set-Res $doc ([Windows.Documents.FlowDocument]::ForegroundProperty) 'Text'
    $display = New-Object Windows.Media.FontFamily 'Segoe UI Variable Display, Segoe UI'
    $list = $null
    for ($i = 0; $i -lt $Lines.Count; $i++) {
        $l = $Lines[$i].TrimStart([char]0xFEFF).TrimEnd()
        if ($i -eq 0) {
            $p = New-Object Windows.Documents.Paragraph
            Add-MdInlines $p $l
            $p.FontSize = 12.5
            Set-Res $p ([Windows.Documents.Paragraph]::ForegroundProperty) 'Text2'
            $p.Margin = New-Object Windows.Thickness(0, 0, 0, 8)
            $doc.Blocks.Add($p); continue
        }
        if ($l.Trim() -eq '') { $list = $null; continue }
        if ($l.Trim() -eq '---') {
            $p = New-Object Windows.Documents.Paragraph
            $p.Margin = New-Object Windows.Thickness(0, 6, 0, 14)
            $p.BorderThickness = New-Object Windows.Thickness(0, 1, 0, 0)
            Set-Res $p ([Windows.Documents.Paragraph]::BorderBrushProperty) 'Line2'
            $p.FontSize = 2
            $doc.Blocks.Add($p); $list = $null; continue
        }
        if ($l.StartsWith('# ')) {
            $p = New-Object Windows.Documents.Paragraph
            Add-MdInlines $p $l.Substring(2)
            $p.FontFamily = $display; $p.FontSize = 20; $p.FontWeight = 'SemiBold'
            $p.Margin = New-Object Windows.Thickness(0, 2, 0, 10)
            $doc.Blocks.Add($p); $list = $null; continue
        }
        if ($l.StartsWith('## ')) {
            $p = New-Object Windows.Documents.Paragraph
            Add-MdInlines $p $l.Substring(3)
            $p.FontFamily = $display; $p.FontSize = 15.5; $p.FontWeight = 'SemiBold'
            $p.Margin = New-Object Windows.Thickness(0, 10, 0, 6)
            $doc.Blocks.Add($p); $list = $null; continue
        }
        if ($l.StartsWith('- ')) {
            if (-not $list) {
                $list = New-Object Windows.Documents.List
                $list.MarkerStyle = 'Disc'
                $list.Padding = New-Object Windows.Thickness(20, 0, 0, 0)
                $list.Margin = New-Object Windows.Thickness(0, 0, 0, 10)
                $doc.Blocks.Add($list)
            }
            $p = New-Object Windows.Documents.Paragraph
            Add-MdInlines $p $l.Substring(2)
            $p.Margin = New-Object Windows.Thickness(0, 0, 0, 6)
            $list.ListItems.Add((New-Object Windows.Documents.ListItem($p)))
            continue
        }
        $list = $null
        $p = New-Object Windows.Documents.Paragraph
        Add-MdInlines $p $l
        $p.Margin = New-Object Windows.Thickness(0, 0, 0, 10)
        $doc.Blocks.Add($p)
    }
    return $doc
}

function Set-TermsBoxForTest([bool]$On) {
    # The ONLY place where the program itself may tick the Terms of Use box:
    # the self-test, the captures and the -AutoRun test switch, which refuses
    # to run without all five test overrides. A real install is always
    # ticked by a person.
    if ($script:Harness -ne 'selftest' -and $script:Harness -ne 'shots' -and
        -not ($script:Harness -eq 'autorun' -and $script:OverridesComplete)) {
        throw 'Refused: the Terms of Use box is ticked only by a person.'
    }
    $ui.AgreeBox.IsChecked = $On
}

$script:DocDir = $null

function Get-DocumentPath([string]$Name) {
    # The file to hand to Notepad. From the Setup.exe the window runs in a
    # folder that Setup deletes when it ends, and Notepad must not hold it: a
    # copy is opened instead, in a private folder of its own (htt-doc-<guid>
    # in the temp folder) that Remove-DocumentCopies deletes on exit.
    $path = Join-Path $Here $Name
    if (-not $FromSetupExe) { return $path }
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { return $path }
    if (-not $script:DocDir) { $script:DocDir = New-PrivateTempDir 'htt-doc-' }
    $copy = Join-Path $script:DocDir $Name
    Copy-Item -LiteralPath $path -Destination $copy -Force
    return $copy
}

function Remove-DocumentCopies {
    $d = $script:DocDir
    $script:DocDir = $null
    if ($d) { Remove-PrivateTempDir $d 'htt-doc-' }
}

function Open-Document([string]$Name) {
    $path = Join-Path $Here $Name
    if (-not (Test-Path -LiteralPath $path)) {
        $ui.LinkStatus.Text = "$Name is missing next to the installer."
    } elseif (Test-TestMode) {
        $ui.LinkStatus.Text = "Test mode: Notepad is not opened ($Name)."
    } else {
        try {
            $path = Get-DocumentPath $Name
            Start-Process -FilePath (Join-Path $env:SystemRoot 'System32\notepad.exe') -ArgumentList ('"' + $path + '"')
            $ui.LinkStatus.Text = "$Name opened in Notepad."
        } catch { $ui.LinkStatus.Text = "Notepad could not be opened. The file is $path" }
    }
    $ui.LinkStatus.Visibility = 'Visible'
}

function Open-Folder([string]$Path) {
    # Only an existing folder is opened; anything else does nothing.
    $isFolder = $false
    try { $isFolder = [bool]$Path -and (Test-Path -LiteralPath $Path -PathType Container) } catch { }
    if (-not $isFolder) { return }
    if (Test-TestMode) { $ui.ChooseNote.Text = 'Test mode: nothing is opened.'; $ui.ChooseNote.Visibility = 'Visible'; return }
    try { Start-Process -FilePath (Join-Path $env:SystemRoot 'explorer.exe') -ArgumentList ('"' + $Path + '"') } catch { }
}

# -- installing ------------------------------------------------------------------------------------

function Start-Install {
    if (-not (Test-CanInstall)) { return $false }
    $engineArgs = Get-InstallArgs
    if ($engineArgs -notcontains '-AcceptedTerms') { return $false }
    Initialize-Progress @($script:Facts.steps)
    Show-Page 'install'
    Add-Log ('Hold to talk ' + $script:Facts.version + ' - ' + (Get-ModeWords).Ger.ToLower())
    try {
        [void](Start-Engine $engineArgs 'progress')
    } catch {
        $script:ErrorText = "The installer could not be started: $($_.Exception.Message)"
        Complete-Install -1
        return $false
    }
    return $true
}

function Initialize-Progress([string[]]$Steps) {
    $ui.StepList.Children.Clear()
    $script:StepRows = @()
    $script:CurrentStep = 0
    $script:StepStarted = $null
    $script:Warnings = @()
    $script:ErrorText = $null
    $script:DoneInfo = $null
    $script:StopRequested = $false
    $script:LastLine = $null
    $ui.LogBox.Clear()
    $script:FullLog.Clear()
    $script:LogPending.Clear()
    $ui.Bar.Value = 0
    $ui.Bar.Tag = $null
    # The step list is the facts' step titles (at most $MaxSteps); a marker
    # can never add to it.
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
    $label = "$($o.Title.Text): $State"
    [Windows.Automation.AutomationProperties]::SetName($o.Root, $label)
}

function Set-Step([int]$N, [string]$Title) {
    # $N is 1..$script:StepTotal (the caller checks); the total is the number
    # of step titles from the facts.
    $Total = $script:StepTotal
    if ($N -lt 1 -or $N -gt $Total -or $script:StepRows.Count -lt $Total) { return }
    for ($k = 1; $k -lt $N; $k++) { if ($script:StepRows[$k - 1].State -ne 'done') { Set-StepState $k 'done' } }
    if ($Title) {
        if ($Title.Length -gt 200) { $Title = $Title.Substring(0, 200) }
        $script:StepRows[$N - 1].Title.Text = $Title
    }
    Set-StepState $N 'current'
    $script:CurrentStep = $N
    $script:StepStarted = Get-Date
    $script:ActivitySeen = $false
    $ui.Bar.Value = [double]($N - 1) / [double]$Total
    $ui.Bar.Tag = $null
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
    if ($Which -eq 'Last' -and $t.Length -gt 58) {
        # one line: trimmed in the middle, so both the start and the end stay readable
        $t = $t.Substring(0, 32).TrimEnd() + ' ... ' + $t.Substring($t.Length - 21).TrimStart()
    }
    $o.$Which.Text = $t
    Set-Shown $o.$Which $true
}

function Update-Busy {
    # The sheen means "working", never an amount: it runs while a step has
    # an activity or has taken more than two seconds.
    if ($script:CurrentStep -lt 1 -or $script:DoneInfo) { $ui.Bar.Tag = $null; return }
    $long = $script:ActivitySeen -or ($script:StepStarted -and ((Get-Date) - $script:StepStarted).TotalSeconds -gt 2)
    if ($long) { $ui.Bar.Tag = 'busy' } else { $ui.Bar.Tag = $null }
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
                # n must be a whole number from 1 to the number of step titles
                # in the facts; anything else is shown as an ordinary line.
                $n = $m.n
                if (($n -is [int] -or $n -is [long]) -and $n -ge 1 -and $n -le $script:StepTotal) {
                    $title = [string]$m.title
                    Add-Log ''
                    Add-Log ("[$n/$($script:StepTotal)] $title")
                    Set-Step ([int]$n) $title
                } else {
                    Add-Log $Line
                }
            }
            'activity' {
                $t = [string]$m.text
                Add-Log ('  ' + $t)
                $script:ActivitySeen = $true
                Set-StepText 'Act' $t
                Update-Busy
            }
            'warning' {
                $t = [string]$m.text
                Add-Log ('Warning: ' + $t)
                if ($t -and $script:Warnings.Count -lt 20) {
                    if ($t.Length -gt 1000) { $t = $t.Substring(0, 1000) }
                    $script:Warnings += $t
                }
            }
            'error' {
                $t = [string]$m.text
                if ($t.Length -gt 4000) { $t = $t.Substring(0, 4000) }
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

function Get-RunAgainName {
    # What a person runs to try again: Setup itself on the Setup.exe route (install.bat is
    # not there: its folder is Setup's private temporary folder), install.bat otherwise.
    if ($FromSetupExe) { return 'Setup' }
    return 'install.bat'
}

function Add-SetupExeHint([string]$Text) {
    # The engine (install.ps1) words its warnings and errors for the ZIP route ("run
    # install.bat again"). On the Setup.exe route one line says what that means here.
    if ($FromSetupExe -and $Text -match 'install\.bat') { return ($Text + "`n(If you used the Setup file: run Setup again.)") }
    return $Text
}

function Complete-Install([int]$Code) {
    Hide-Confirm
    $ui.Bar.Tag = $null
    if ($script:StopRequested) {
        Show-Result 'stopped' $Code
    } elseif ($Code -eq 0 -and $script:DoneInfo) {
        Show-Result 'success' $Code
    } else {
        Show-Result 'failure' $Code
    }
}

function Set-Badge([int]$Glyph, [string]$Fg, [string]$Bg) {
    $ui.ResultGlyph.Text = [string][char]$Glyph
    Set-Res $ui.ResultGlyph ([Windows.Controls.TextBlock]::ForegroundProperty) $Fg
    Set-Res $ui.ResultBadge ([Windows.Controls.Border]::BackgroundProperty) $Bg
}

function Show-Result([string]$Kind, [int]$Code) {
    $script:ResultKind = $Kind
    $f = $script:Facts
    $w = Get-ModeWords
    $ui.WarnList.Children.Clear()
    $ui.ResultPath.Visibility = 'Collapsed'
    if ($Kind -eq 'success') {
        $script:Outcome = 0
        $d = $script:DoneInfo
        Set-Badge 0xE73E 'Ok' 'OkSoft'
        $ui.ResultTitle.Text = "Hold to talk is $($w.Past)"
        if ($script:Mode -eq 'update') { $ui.ResultLead.Text = "Version $($f.version) is now installed in this folder:" }
        else { $ui.ResultLead.Text = "Version $($f.version) is installed in this folder:" }
        $ui.ResultPath.Text = [string]$f.layout.installRoot       # from the facts, not from a marker
        $ui.ResultPath.Visibility = 'Visible'
        Set-Shown $ui.DesktopHow ([bool]$d.desktopLnk)
        foreach ($w in $script:Warnings) {
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
            $card.Child.Children[1].Text = (Add-SetupExeHint $w)
            [void]$ui.WarnList.Children.Add($card)
        }
        $ui.StartNow.IsChecked = -not (Test-TestMode)      # (shown in the button row by Update-Nav)
        Set-Shown $ui.SuccessPanel $true
        Set-Shown $ui.FailPanel $false
    } elseif ($Kind -eq 'stopped') {
        $script:Outcome = 2
        Set-Badge 0xE7BA 'Warn' 'WarnSoft'
        $ui.ResultTitle.Text = "$($w.NounCap) stopped"
        $ui.ResultLead.Text = "You stopped the $($w.Noun). What was already done stays on this PC, and nothing is broken. Try again (the same as running $(Get-RunAgainName) again) carries on where it stopped."
        Set-Shown $ui.SuccessPanel $false
        Set-Shown $ui.FailPanel $false
    } else {
        $script:Outcome = 1
        Set-Badge 0xE783 'Bad' 'BadSoft'
        $ui.ResultTitle.Text = "Hold to talk could not be $($w.Past)"
        $ui.ResultLead.Text = "Nothing is broken. Fix the problem described below, then choose Try again (the same as running $(Get-RunAgainName) again): the $($w.Tool) carries on where it stopped."
        $msg = $script:ErrorText
        if (-not $msg) {
            $tail = @($script:FullLog | Where-Object { $_.Trim() } | Select-Object -Last 8)
            $msg = "The installer stopped unexpectedly (exit code $Code)."
            if ($tail.Count) { $msg += "`n`nThe last lines it printed:`n" + ($tail -join "`n") }
        }
        $msg = Add-SetupExeHint $msg
        $ui.ErrorBox.Text = ($msg -replace "`r?`n", "`r`n")
        Set-Shown $ui.SuccessPanel $false
        Set-Shown $ui.FailPanel $true
    }
    Show-Page 'result'
}

# -- "Install Python for me" ---------------------------------------------------------------------
# Downloads ONE pinned file (the official 64-bit Windows installer of Python
# from python.org), checks it, runs it for this account only, and then the
# normal "Check again" takes over. Nothing is downloaded before the button is
# chosen. In test mode (the five overrides, or any harness) nothing is ever
# downloaded or run: the button calls Invoke-PythonInstallStub. The real steps
# can run in test mode only through $script:PyOpen / $script:PyRun, which only
# the self-test sets (a local file and a fake program, never the network).
#
# Phases, advanced by the window's timer (the window never waits on the network):
#   connect -> download -> verify -> installing -> done / failed
# The installer file is in a folder made fresh for this run (htt-py-<guid> in
# the temp folder, this account only) and the folder is deleted in every case.

$script:PyPin = [pscustomobject]@{
    Version = '3.12.10'
    Url     = 'https://www.python.org/ftp/python/3.12.10/python-3.12.10-amd64.exe'
    HostName = 'www.python.org'
    Bytes   = [int64]26964224
    Sha256  = '67b5635e80ea51072b87941312d00ec8927c4db9ba18938f7ad2d27b328b95fb'
    Signer  = 'Python Software Foundation'
    # per user, no administrator rights, python.exe not put on PATH, no file associations and no
    # shortcuts (option names: docs.python.org/3.12/using/windows.html, "Installing Without UI")
    Args    = @('/quiet', 'InstallAllUsers=0', 'PrependPath=0', 'Include_launcher=1', 'InstallLauncherAllUsers=0',
                'Include_tcltk=1', 'Include_test=0', 'SimpleInstall=1', 'AssociateFiles=0', 'Shortcuts=0')
}
$script:Py = $null                 # the run in progress (a hashtable), or $null
$script:PyOpen = $null             # tests only: scriptblock returning @{ Stream; Length } (no network)
$script:PyRun = $null              # tests only: scriptblock (path, args) returning a process-like object
$script:PyGetSignature = $null     # tests only: scriptblock (path) returning @{ Status; Subject }
$script:PyInstallCalls = New-Object Collections.Generic.List[string]
$script:PyLastError = $null
$script:PyTimeoutSeconds = 900     # after this long "Stop waiting" is offered

function Get-FileSha256([string]$Path) {
    $sha = [Security.Cryptography.SHA256]::Create()
    $fs = New-Object IO.FileStream($Path, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::Read)
    try { $b = $sha.ComputeHash($fs) } finally { $fs.Dispose(); $sha.Dispose() }
    return (($b | ForEach-Object { $_.ToString('x2') }) -join '')
}

function Get-StreamSha256([IO.Stream]$Stream) {
    # The hash of the whole stream, read from its start (the file stays open).
    $sha = [Security.Cryptography.SHA256]::Create()
    try { [void]$Stream.Seek(0, [IO.SeekOrigin]::Begin); $b = $sha.ComputeHash($Stream) } finally { $sha.Dispose() }
    return (($b | ForEach-Object { $_.ToString('x2') }) -join '')
}

function Get-InstallerSignature([string]$Path) {
    $s = Get-AuthenticodeSignature -LiteralPath $Path
    $subject = ''
    if ($s.SignerCertificate) { $subject = [string]$s.SignerCertificate.Subject }
    return @{ Status = [string]$s.Status; Subject = $subject }
}

function Test-PythonInstaller([string]$Path, $Pin, [IO.FileStream]$Lock = $null) {
    # $null when the file is the pinned one AND carries a valid signature of the
    # Python Software Foundation; otherwise the plain reason. Both are needed.
    # $Lock is the file opened for reading with FileShare.Read (nobody can write
    # to it, replace it or rename it while it is open): the size and the hash
    # are taken from that open handle, so what is checked is what is run. Without
    # one (the self-test only) the file is opened here for the length of the check.
    $own = $null
    if (-not $Lock) {
        if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) { return 'The downloaded file is missing.' }
        try { $own = New-Object IO.FileStream($Path, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::Read) }
        catch { return 'The downloaded file could not be opened, so it was not used.' }
        $Lock = $own
    }
    try {
        if ($Lock.Length -ne $Pin.Bytes) {
            return 'The downloaded file is not the expected size, so it was not used.'
        }
        $got = Get-StreamSha256 $Lock
        return (Test-PythonSignature $Path $Pin $got)
    } finally { if ($own) { $own.Dispose() } }
}

function Test-PythonSignature([string]$Path, $Pin, [string]$got) {
    if ($got -ne ([string]$Pin.Sha256).ToLowerInvariant()) {
        return 'The downloaded file is not the one that was expected (its checksum does not match), so it was not used.'
    }
    try {
        if ($script:PyGetSignature) { $sig = & $script:PyGetSignature $Path } else { $sig = Get-InstallerSignature $Path }
    } catch { return 'The signature of the downloaded file could not be checked, so it was not used.' }
    if ($sig.Status -ne 'Valid') {
        return "The downloaded file has no valid signature (Windows says: $($sig.Status)), so it was not used."
    }
    if (([string]$sig.Subject).IndexOf([string]$Pin.Signer, [StringComparison]::Ordinal) -lt 0) {
        return "The downloaded file is not signed by the $($Pin.Signer), so it was not used."
    }
    return $null
}

function New-PrivateTempDir([string]$Prefix) {
    # A folder made new in the temp folder (<prefix><32 hex>), readable by this
    # account only. If it cannot be made private it is removed again and this
    # throws: nothing is put in a folder others might read or change.
    $dir = Join-Path ([IO.Path]::GetTempPath()) ($Prefix + [Guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $dir -ErrorAction Stop | Out-Null     # fails if it exists
    try {
        $acl = New-Object Security.AccessControl.DirectorySecurity
        $acl.SetAccessRuleProtection($true, $false)
        $me = [Security.Principal.WindowsIdentity]::GetCurrent().User
        $acl.AddAccessRule((New-Object Security.AccessControl.FileSystemAccessRule(
            $me, 'FullControl', 'ContainerInherit,ObjectInherit', 'None', 'Allow')))
        if ($script:Harness -eq 'selftest' -and $script:FailAcl) { throw 'test: the ACL could not be set' }
        Set-Acl -LiteralPath $dir -AclObject $acl
    } catch {
        try { Remove-Item -LiteralPath $dir -Recurse -Force -ErrorAction Stop } catch { }
        throw 'A private folder could not be made on this PC (its access rights could not be set), so nothing was done.'
    }
    return $dir
}

function Remove-PrivateTempDir([string]$Dir, [string]$Prefix) {
    # Only a folder this window made: <prefix><32 hex> directly in the temp folder.
    # Best effort: a file still held open by a program is left for Windows' own clean-up.
    if (-not $Dir) { return }
    $leaf = Split-Path -Leaf $Dir
    $parent = (Split-Path -Parent $Dir).TrimEnd('\')
    if ($leaf -notmatch ('^' + [regex]::Escape($Prefix) + '[0-9a-f]{32}$') -or $parent -ine ([IO.Path]::GetTempPath().TrimEnd('\'))) { return }
    for ($i = 1; $i -le 5; $i++) {
        if (-not (Test-Path -LiteralPath $Dir)) { return }
        try { Remove-Item -LiteralPath $Dir -Recurse -Force -ErrorAction Stop } catch { Start-Sleep -Milliseconds (200 * $i) }
    }
}

function New-PythonWorkDir { return (New-PrivateTempDir 'htt-py-') }

function Remove-PythonWorkDir([string]$Dir) { Remove-PrivateTempDir $Dir 'htt-py-' }

function Format-PythonSize {
    return (Format-Bytes ([double]$script:PyPin.Bytes))
}

function Set-PythonTexts {
    $pin = $script:PyPin
    $ui.PyLead.Text = "Python is a free program that Hold to talk needs. Choose the button and Setup downloads it from python.org (about $(Format-PythonSize)) and installs it for your Windows account only. Nothing is downloaded until you choose the button."
    $ui.PyWhat.Text = "The installer is the official Python $($pin.Version) installer (64-bit, about $(Format-PythonSize)) from python.org. It is checked against a fixed fingerprint and the Python Software Foundation's signature before it runs; if either check fails, nothing is run. Python $($pin.Version) is installed for this Windows account only, with the `"py`" launcher. It needs no administrator rights, does not add python.exe to your PATH, and sets up no file associations and no Start menu shortcuts; the `"py`" launcher may add its own folder to your PATH. On a PC with an ARM processor, still choose the 64-bit (x64) installer: it runs there."
    $ui.PyLicence.Text = 'Python is not part of Hold to talk. It comes under its own licence, the PSF License (python.org/psf/license).'
}

function Update-PythonUi {
    $p = $script:Py
    $busy = ($null -ne $p)
    $ui.PyInstallBtn.IsEnabled = -not $busy -and -not $script:Checking
    $ui.PythonOrgBtn.IsEnabled = -not $busy
    $ui.CheckAgainBtn.IsEnabled = -not $busy -and -not $script:Checking
    Set-Shown $ui.PyProgressPanel $busy
    Set-Shown $ui.PyCancelBtn ($busy -and ($p.Phase -eq 'connect' -or $p.Phase -eq 'download'))
    Set-Shown $ui.PyStopBtn ($busy -and $p.Phase -eq 'installing' -and [bool]$p.OfferStop)
    if ($script:PyLastError) { $ui.PyError.Text = $script:PyLastError; Set-Shown $ui.PyError $true } else { Set-Shown $ui.PyError $false }
    if ($script:Page -eq 'choose') { Update-Nav }
}

function Set-PythonStatus([string]$Text, $Fraction) {
    $ui.PyStatus.Text = $Text
    if ($null -eq $Fraction) { $ui.PyBar.Tag = 'busy'; $ui.PyBar.Value = 0 }
    else { $ui.PyBar.Tag = $null; $ui.PyBar.Value = [double]$Fraction }
}

function Invoke-PythonInstallStub {
    # Test mode: nothing is downloaded or run. The call is recorded for the
    # tests, and the fake engine then reports that Python was found.
    $script:PyInstallCalls.Add('install-python')
    [Environment]::SetEnvironmentVariable('HTT_FAKE_PYTHON_INSTALLED', '1', 'Process')
    $script:PyLastError = $null
    $ui.PythonNote.Text = 'Test mode: nothing is downloaded or installed.'
    Set-Shown $ui.PythonNote $true
    Update-PythonUi
    Start-Check $true
}

function Start-PythonInstall {
    # The button. Test mode goes to the stub; the real run needs a person.
    if ($script:Py -or $script:Run -or $script:Checking) { return }
    $script:PyLastError = $null
    Set-Shown $ui.PythonNote $false
    if ((Test-TestMode) -and -not $script:PyOpen) { Invoke-PythonInstallStub; return }
    Start-PythonDownload
}

function Start-PythonDownload {
    if ((Test-TestMode) -and -not $script:PyOpen) { throw 'Refused: nothing is downloaded in test mode.' }
    $pin = $script:PyPin
    $p = @{ Phase = 'connect'; Dir = $null; File = $null; Out = $null; Stream = $null; Got = [int64]0
            ReadTask = $null; Buf = (New-Object byte[] 65536); Cancel = $false; Started = Get-Date; LastData = Get-Date
            Http = $null; Task = $null; Proc = $null; Lock = $null; InstallStarted = $null; OfferStop = $false }
    $script:Py = $p
    try {
        if (-not $script:PyOpen) {
            $uri = [Uri]$pin.Url
            if ($uri.Scheme -ne 'https' -or $uri.Host -ne $pin.HostName) { throw 'The download address is not python.org over HTTPS.' }
            $proto = [Net.SecurityProtocolType]::Tls12
            try { $proto = $proto -bor [Net.SecurityProtocolType]'Tls13' } catch { }
            [Net.ServicePointManager]::SecurityProtocol = $proto
        }
        $p.Dir = New-PythonWorkDir
        $p.File = Join-Path $p.Dir ('python-' + $pin.Version + '-amd64.exe')
        if (-not $script:PyOpen) {
            Add-Type -AssemblyName System.Net.Http
            $h = New-Object Net.Http.HttpClientHandler
            $h.AllowAutoRedirect = $false                # a redirect is refused, not followed
            $c = New-Object Net.Http.HttpClient($h)
            $c.Timeout = [TimeSpan]::FromMinutes(30)
            $req = New-Object Net.Http.HttpRequestMessage([Net.Http.HttpMethod]::Get, $pin.Url)
            $p.Http = $c
            $p.Task = $c.SendAsync($req, [Net.Http.HttpCompletionOption]::ResponseHeadersRead)
        }
        Set-PythonStatus 'Connecting to python.org...' $null
        Update-PythonUi
        $script:Timer.Start()
    } catch {
        Stop-PythonRun ('Python could not be downloaded: ' + $_.Exception.Message)
    }
}

function Stop-PythonRun([string]$Problem) {
    # Ends the run (any phase but a running installer): streams closed, the
    # folder with the installer or its partial file deleted, the message shown.
    $p = $script:Py
    if ($p) {
        $p.ReadTask = $null
        try { if ($p.Lock) { $p.Lock.Dispose() } } catch { }
        try { if ($p.Out) { $p.Out.Dispose() } } catch { }
        try { if ($p.Stream) { $p.Stream.Dispose() } } catch { }
        try { if ($p.Http) { $p.Http.Dispose() } } catch { }
        Remove-PythonWorkDir $p.Dir
    }
    $script:Py = $null
    $script:PyLastError = $Problem
    $ui.PyBar.Tag = $null
    Update-PythonUi
}

function Stop-PythonDownload {
    # The Cancel button: only while connecting or downloading.
    $p = $script:Py
    if (-not $p -or ($p.Phase -ne 'connect' -and $p.Phase -ne 'download')) { return $false }
    $p.Cancel = $true
    Stop-PythonRun $null
    $ui.PythonNote.Text = 'The download was cancelled. Nothing was installed.'
    Set-Shown $ui.PythonNote $true
    return $true
}

function Stop-PythonInstallWait {
    # "Stop waiting": ends only the installer process this window started (the
    # Process object it holds, never one found by name), cleans up and shows the
    # manual route. Only after the timeout, or when the person closes the window.
    $p = $script:Py
    if (-not $p -or $p.Phase -ne 'installing') { return $false }
    try { if ($p.Proc -and -not $p.Proc.HasExited) { $p.Proc.Kill(); [void]$p.Proc.WaitForExit(5000) } } catch { }
    Stop-PythonRun 'Stopped waiting for the Python installer. It may have left Python half installed. You can try again, or install Python yourself (see below).'
    $ui.PyManual.IsExpanded = $true
    return $true
}

function Show-PythonStopConfirm {
    # The same question from the "Stop waiting" button and from closing the window.
    if (-not $script:Py -or $script:Py.Phase -ne 'installing') { return }
    if ($ui.Overlay.Visibility -eq 'Visible') { return }
    $script:ConfirmKind = 'python'
    $script:SavedConfirm = @($ui.StopTitle.Text, $ui.StopBody.Text, $ui.StopBtn.Content, $ui.KeepBtn.Content)
    $ui.StopTitle.Text = 'Stop waiting for Python?'
    $ui.StopBody.Text = 'Hold to talk will end the Python installer it started and stop waiting. Python may be left half installed. You can try again, or install Python yourself.'
    $ui.StopBtn.Content = 'Stop waiting'
    $ui.KeepBtn.Content = 'Keep waiting'
    Show-Confirm
}

function Step-PythonRun {
    $p = $script:Py
    if (-not $p) { return }
    try {
        switch ($p.Phase) {
            'connect' {
                if (((Get-Date) - $p.Started).TotalSeconds -gt 45) { throw 'python.org did not answer in time.' }
                $src = $null
                if ($script:PyOpen) { $src = & $script:PyOpen $script:PyPin.Url }
                else {
                    $t = $p.Task
                    if (-not $t.IsCompleted) { return }
                    if ($t.IsFaulted -or $t.IsCanceled) { throw 'The connection to python.org failed. Check your internet connection.' }
                    $r = $t.Result
                    if ([int]$r.StatusCode -ne 200) { throw "python.org answered with code $([int]$r.StatusCode)." }
                    $len = $r.Content.Headers.ContentLength
                    $src = @{ Stream = $r.Content.ReadAsStreamAsync().Result; Length = [int64]$len }
                }
                if ($src.Length -ne $script:PyPin.Bytes) { throw 'The file on the server is not the expected size.' }
                $p.Stream = $src.Stream
                $p.Out = New-Object IO.FileStream($p.File, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
                $p.Phase = 'download'; $p.LastData = Get-Date
                Update-PythonUi
            }
            'download' {
                for ($i = 0; $i -lt 8; $i++) {
                    if (-not $p.ReadTask) { $p.ReadTask = $p.Stream.ReadAsync($p.Buf, 0, $p.Buf.Length) }
                    if (-not $p.ReadTask.IsCompleted) {
                        if (((Get-Date) - $p.LastData).TotalSeconds -gt 60) { throw 'The download stopped (nothing arrived for a minute).' }
                        break
                    }
                    if ($p.ReadTask.IsFaulted -or $p.ReadTask.IsCanceled) { throw 'The download was interrupted.' }
                    $n = $p.ReadTask.Result
                    $p.ReadTask = $null
                    $p.LastData = Get-Date
                    if ($n -le 0) { $p.Phase = 'verify'; break }
                    $p.Out.Write($p.Buf, 0, $n)
                    $p.Got += $n
                    if ($p.Got -gt $script:PyPin.Bytes) { throw 'More data arrived than expected, so the download was stopped.' }
                }
                if ($p.Phase -eq 'download') {
                    $frac = [double]$p.Got / [double]$script:PyPin.Bytes
                    Set-PythonStatus ("Downloading Python... {0} of {1}" -f (Format-Bytes $p.Got), (Format-PythonSize)) $frac
                } else {
                    $p.Out.Dispose(); $p.Out = $null
                    $p.Stream.Dispose(); $p.Stream = $null
                    # Held open (read, nobody may write) from here until the installer has started,
                    # so the file cannot be replaced or renamed between the check and the run.
                    $p.Lock = New-Object IO.FileStream($p.File, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::Read)
                    Set-PythonStatus 'Checking the downloaded file...' $null
                    Update-PythonUi
                }
            }
            'verify' {
                # (one tick after the status was drawn: hashing and the signature check take a moment)
                if (-not $p.Lock) { $p.Lock = New-Object IO.FileStream($p.File, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::Read) }
                $problem = Test-PythonInstaller $p.File $script:PyPin $p.Lock
                if ($problem) { throw $problem }
                Set-PythonStatus 'Installing Python for this Windows account (this takes a minute or two)...' $null
                $argsLine = (@($script:PyPin.Args) -join ' ')
                try {
                    if ($script:PyRun) { $p.Proc = & $script:PyRun $p.File $argsLine $p.Lock }
                    else {
                        if (Test-TestMode) { throw 'Refused: nothing is run in test mode.' }
                        $psi = New-Object Diagnostics.ProcessStartInfo
                        $psi.FileName = $p.File
                        $psi.Arguments = $argsLine
                        $psi.UseShellExecute = $false
                        $psi.CreateNoWindow = $true
                        $psi.WorkingDirectory = $p.Dir
                        $p.Proc = [Diagnostics.Process]::Start($psi)
                    }
                } finally {
                    # only now, with the process started, may the file be released
                    try { $p.Lock.Dispose() } catch { }
                    $p.Lock = $null
                }
                $p.Phase = 'installing'
                $p.InstallStarted = Get-Date
                Update-PythonUi
            }
            'installing' {
                if (-not $p.Proc.HasExited) {
                    if (-not $p.OfferStop -and ((Get-Date) - $p.InstallStarted).TotalSeconds -ge $script:PyTimeoutSeconds) {
                        $p.OfferStop = $true
                        Set-PythonStatus 'The Python installer is taking a long time (more than 15 minutes). You can keep waiting, or stop waiting.' $null
                        Update-PythonUi
                    }
                    return
                }
                $code = [int]$p.Proc.ExitCode
                # 3010 is Windows Installer's "done, a restart may be needed later": a success
                if ($code -ne 0 -and $code -ne 3010) { throw "The Python installer stopped with code $code. Nothing in Hold to talk was changed. You can try again, or install Python yourself." }
                $p.RestartHint = ($code -eq 3010)
                Complete-PythonInstall
            }
        }
    } catch {
        Stop-PythonRun $_.Exception.Message
    }
}

function Complete-PythonInstall {
    $p = $script:Py
    Remove-PythonWorkDir $p.Dir
    $script:Py = $null
    $script:PyLastError = $null
    Update-ProcessAfterPython
    $ui.PyBar.Tag = $null
    if ($p.RestartHint) { $ui.PythonNote.Text = 'Python was installed. Windows may want a restart later; you do not need one now. Checking this PC again...' }
    else { $ui.PythonNote.Text = 'Python was installed. Checking this PC again...' }
    Set-Shown $ui.PythonNote $true
    Update-PythonUi
    Start-Check $true
}

function Update-ProcessAfterPython {
    # The installer changed the user's PATH (the py launcher), but this window
    # and the engine it starts keep the PATH they were started with: read it
    # again, and name the new python.exe, so "Check again" can find it.
    if ($script:Harness) { return }
    try {
        $m = [Environment]::GetEnvironmentVariable('Path', 'Machine')
        $u = [Environment]::GetEnvironmentVariable('Path', 'User')
        [Environment]::SetEnvironmentVariable('Path', (($m, $u | Where-Object { $_ }) -join ';'), 'Process')
    } catch { }
    try {
        $exe = Join-Path $env:LOCALAPPDATA 'Programs\Python\Python312\python.exe'
        if (Test-Path -LiteralPath $exe -PathType Leaf) { [Environment]::SetEnvironmentVariable('HTT_PYTHON', $exe, 'Process') }
    } catch { }
}

# -- the confirmation and closing ---------------------------------------------------------------------

$script:FailAcl = $false
$script:ConfirmKind = $null
$script:SavedConfirm = $null
$script:ClosePending = $false

function Show-Confirm {
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
    if ($script:ConfirmKind -eq 'python' -and $script:SavedConfirm) {
        $ui.StopTitle.Text = $script:SavedConfirm[0]; $ui.StopBody.Text = $script:SavedConfirm[1]
        $ui.StopBtn.Content = $script:SavedConfirm[2]; $ui.KeepBtn.Content = $script:SavedConfirm[3]
    }
    $script:ConfirmKind = $null
    $script:ClosePending = $false
}

function Confirm-Stop {
    if ($script:ConfirmKind -eq 'python') {
        $closing = $script:ClosePending
        Hide-Confirm
        [void](Stop-PythonInstallWait)
        if ($closing) { Close-Window (Get-CancelCode) }
        return
    }
    Hide-Confirm
    if (-not $script:Run -or $script:Run.Kind -ne 'progress') { return }
    $script:StopRequested = $true
    $ui.StepNow.Text = 'Stopping...'
    Update-Nav
    Stop-EngineTree
}

function Get-CancelCode {
    if ($null -ne $script:Outcome) { return [int]$script:Outcome }
    return 3
}

function Close-Window([int]$Code) {
    $script:ExitCode = $Code
    if ($script:Harness -eq 'selftest') { $script:ClosedWith = $Code; return }
    if ($script:Run) { Stop-EngineTree; $script:Run = $null }
    if ($script:Py) { [void](Stop-PythonDownload) }
    if ($script:Py -and $script:Py.Phase -eq 'installing') { [void](Stop-PythonInstallWait) }
    $script:AllowClose = $true
    $script:Window.Close()
}

function Invoke-Cancel {
    # The Cancel button and Esc.
    if ($ui.Overlay.Visibility -eq 'Visible') { Hide-Confirm; return }
    if ($script:Py) {
        # Esc ends a download; a running Python installer is only ended after the question
        if ($script:Py.Phase -eq 'installing') { if ($script:Py.OfferStop) { Show-PythonStopConfirm }; return }
        [void](Stop-PythonDownload); return
    }
    switch ($script:Page) {
        'install' {
            if ($script:Run -and -not $script:StopRequested) { Show-Confirm }
        }
        default { Close-Window (Get-CancelCode) }
    }
}

function On-Closing($e) {
    if ($script:AllowClose) { return }
    if ($script:Page -eq 'install' -and $script:Run) {
        $e.Cancel = $true
        if (-not $script:StopRequested) { Show-Confirm }
        return
    }
    if ($script:Py) {
        if (-not (Stop-PythonDownload)) {
            # The Python installer is running: closing asks the same question as "Stop waiting".
            $e.Cancel = $true
            if ($script:Py.Phase -eq 'installing') { $script:ClosePending = $true; Show-PythonStopConfirm }
            return
        }
    }
    if ($script:Run) { Stop-EngineTree; $script:Run = $null }
    if ($null -eq $script:ExitCode) { $script:ExitCode = Get-CancelCode }
}

function Invoke-Next {
    switch ($script:Page) {
        'welcome' { if ($script:Facts) { Show-Page 'terms' } }
        'terms' { if ($ui.AgreeBox.IsChecked -eq $true -and $script:TermsOk) { Show-Page 'choose' } }
        'choose' { [void](Start-Install) }
        'result' {
            if ($script:ResultKind -eq 'success' -and $ui.StartNow.IsVisible -and $ui.StartNow.IsChecked -eq $true) { Start-App }
            Close-Window ([int]$script:Outcome)
        }
        'error' { Close-Window (Get-CancelCode) }
    }
}

function Invoke-Back {
    switch ($script:Page) {
        'terms' { Show-Page 'welcome' }
        'choose' { Show-Page 'terms' }
        'result' {
            if ($script:ResultKind -ne 'success') {
                Show-Page 'choose'
                Start-Check $true
            }
        }
    }
}

function Get-StartCommand {
    # What "Start Hold to talk now" runs, worked out here from the layout in the
    # facts. No marker (not "done", not "facts") can change it.
    $L = $null
    if ($script:Facts) { $L = $script:Facts.layout }
    if (-not $L -or -not $L.envDir -or -not $L.installRoot) { return $null }
    $root = [string]$L.installRoot
    [pscustomobject]@{
        Exe     = (Join-Path ([string]$L.envDir) 'Scripts\pythonw.exe')
        Args    = ('"' + (Join-Path $root 'dictate.py') + '" --open')
        WorkDir = $root
    }
}

function Start-App {
    if (Test-TestMode) { return }
    $c = Get-StartCommand
    if (-not $c) { return }
    if (-not (Test-Path -LiteralPath $c.Exe -PathType Leaf)) { return }
    try { Start-Process -FilePath $c.Exe -ArgumentList $c.Args -WorkingDirectory $c.WorkDir } catch { }
}

function Copy-Details {
    $text = Get-LogText
    if ($script:Page -eq 'error') { $text = $ui.ErrDetails.Text }
    if ($script:Page -eq 'result' -and $script:ErrorText) { $text = "Error: $($script:ErrorText)`r`n`r`n$text" }
    if ($script:Harness) { $script:Copied = $text; $ui.CopyBtn.Content = 'Copied'; return }
    try { [Windows.Clipboard]::SetText($text); $ui.CopyBtn.Content = 'Copied' } catch { $ui.CopyBtn.Content = 'Could not copy' }
}

function Open-TextInstaller {
    if ($FromSetupExe) { return }      # the Setup.exe's folder goes away when it ends
    Start-TextInstallerFallback 'The setup window could not continue'
    Close-Window (Get-CancelCode)
}

# -- theme ---------------------------------------------------------------------------------------------

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
    # A dark title bar in dark mode, and (Windows 11) a caption in the
    # window's own background colour. Ignored where Windows does not allow it.
    if (-not $script:Hwnd -or -not ('HttSetupNative' -as [type])) { return }
    try {
        $dark = 0; if ($script:ThemeMode -eq 'dark') { $dark = 1 }
        [void][HttSetupNative]::SetInt($script:Hwnd, 20, $dark)
        [void][HttSetupNative]::SetInt($script:Hwnd, 19, $dark)
        $p = $Palettes[$script:ThemeMode]
        foreach ($pair in @(@(35, $p.Bg), @(36, $p.Text))) {
            $c = [Windows.Media.ColorConverter]::ConvertFromString($pair[1])
            $ref = [int]$c.R + ([int]$c.G -shl 8) + ([int]$c.B -shl 16)
            [void][HttSetupNative]::SetInt($script:Hwnd, $pair[0], $ref)
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
    if ('HttSetupNative' -as [type]) { return }
    try {
        Add-Type -TypeDefinition @'
using System;
using System.Text;
using System.Runtime.InteropServices;
public static class HttSetupNative {
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

# -- pictures for review (-Shots) --------------------------------------------------------------------------

function Get-SourceTermsVersion {
    try {
        $t = Get-Content -LiteralPath (Join-Path $Here 'settings.py') -Raw -Encoding UTF8
        $m = [regex]::Match($t, 'TERMS_VERSION\s*=\s*"([^"]+)"')
        if ($m.Success) { return $m.Groups[1].Value }
    } catch { }
    return '1.0'
}

function Get-CannedFacts([hashtable]$Opt) {
    # Made-up facts with example paths (never this PC's).
    if (-not $Opt) { $Opt = @{} }
    $u = 'C:\Users\you\AppData'
    $py = [ordered]@{ ok = $true; version = '3.12.10'; exe = "$u\Local\Programs\Python\Python312\python.exe"; via = 'py -3.12' }
    if ($Opt.NoPython) {
        $py = [ordered]@{ ok = $false; notes = @('  py -3.12: not found', '  py -3.13: not found', '  py -3.11: not found', '  py -3.10: not found',
            "  python: $u\Local\Microsoft\WindowsApps\python.exe is the Microsoft Store shortcut, not Python") }
    }
    $tv = Get-SourceTermsVersion
    if ($Opt.TermsVersion) { $tv = $Opt.TermsVersion }
    $free = [int64]182 * 1000000000
    if ($Opt.LowDisk) { $free = [int64]700 * 1000000 }     # less than the ~900 MB the update needs
    $f = [ordered]@{
        kind = 'facts'; appName = 'Hold to talk'; version = '0.9.1'; termsVersion = $tv; testMode = [bool]$Opt.TestMode
        layout = [ordered]@{ installRoot = "$u\Local\Programs\HoldToTalk"; envDir = "$u\Local\HoldToTalk\venv"; dataDir = "$u\Local\HoldToTalk\data"
            startMenuLnk = "$u\Roaming\Microsoft\Windows\Start Menu\Programs\Hold to talk.lnk"; desktopLnk = 'C:\Users\you\Desktop\Hold to talk.lnk' }
        installedVersion = $Opt.Installed
        python = $py
        gpu = [ordered]@{ found = [bool]$Opt.Gpu; runtimeInstalled = [bool]$Opt.GpuReady }
        env = [ordered]@{ exists = [bool]$Opt.Installed; depsCurrent = $false }
        model = [ordered]@{ name = 'large-v3-turbo'; cpuName = 'small'; downloadMB = 1500; cpuDownloadMB = 500; present = $(if ($Opt.ModelPresent) { $true } else { $null }) }
        files = [ordered]@{ count = 39; sizeBytes = 1843200 }
        estimates = [ordered]@{ libsDownloadMB = 310; libsDiskMB = 820; nvidiaDownloadMB = 1300; nvidiaDiskMB = 2000 }
        disk = [ordered]@{ drive = 'C:'; freeBytes = $free }
        steps = @('Looking for Python', 'Checking the files', 'Making sure the app is not running', 'Copying the program files',
                  'Python environment and libraries', 'Speech model', 'Shortcuts and the Installed apps entry')
    }
    return (ConvertTo-Json -InputObject $f -Depth 6 -Compress | ConvertFrom-Json)
}

function Reset-Ui {
    Hide-Confirm
    $script:Outcome = $null; $script:ResultKind = $null; $script:ExitCode = $null; $script:ClosedWith = $null
    $script:Checking = $false
    $script:Py = $null
    $ui.AgreeBox.IsChecked = $false
    $script:TermsOk = $false; $script:TermsVersion = $null
    $ui.CheckingRow.Visibility = 'Collapsed'; $ui.RecheckRow.Visibility = 'Collapsed'
    $ui.LinkStatus.Visibility = 'Collapsed'; $ui.ChooseNote.Visibility = 'Collapsed'; $ui.PythonNote.Visibility = 'Collapsed'
    $ui.DetailsExp.IsExpanded = $false; $ui.PyManual.IsExpanded = $false
    $script:PyLastError = $null; Set-Shown $ui.PyError $false; Set-Shown $ui.PyProgressPanel $false
    $ui.Root.UpdateLayout()
}

function Get-ProgressLines([int]$UpTo) {
    $steps = @($script:Facts.steps)
    $out = @()
    for ($n = 1; $n -le $UpTo; $n++) {
        $out += '@@htt ' + (ConvertTo-Json -Compress -InputObject ([ordered]@{ kind = 'step'; n = $n; total = $steps.Count; title = $steps[$n - 1] }))
        switch ($n) {
            1 { $out += '  Using Python 3.12.10 from C:\Users\you\AppData\Local\Programs\Python\Python312\python.exe' }
            2 { $out += '  39 files to install.'; $out += '  Recorded that the Terms of Use (version 1.0) were accepted (installer)' }
            3 { $out += '  Not running (no earlier install to ask).' }
            4 { $out += '  Copied 39 files to C:\Users\you\AppData\Local\Programs\HoldToTalk' }
            5 {
                $out += '@@htt {"kind":"activity","text":"Making the Python environment (a private copy of Python for this app)"}'
                $out += '@@htt {"kind":"activity","text":"Installing the libraries the app needs (a few hundred MB; takes a few minutes)"}'
                $out += 'Collecting faster-whisper==1.1.1 (from -r requirements.lock.txt (line 12))'
                $out += '  Downloading faster_whisper-1.1.1-py3-none-any.whl (1.1 MB)'
                $out += 'Collecting ctranslate2==4.5.0'
                $out += '  Downloading ctranslate2-4.5.0-cp312-cp312-win_amd64.whl (19.4 MB)'
            }
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
    $states = @(
        @{ Name = '01-welcome'; Go = { Set-Facts (Get-CannedFacts @{ Gpu = $true }); Show-Page 'welcome' } },
        @{ Name = '02-welcome-update'; Go = { Set-Facts (Get-CannedFacts @{ Gpu = $true; Installed = '0.8.2'; TestMode = $true }); Show-Page 'welcome' } },
        @{ Name = '03-terms-unticked'; Go = { Set-Facts (Get-CannedFacts @{ Gpu = $true }); Show-Page 'terms' } },
        @{ Name = '04-terms-ticked'; Go = { Set-Facts (Get-CannedFacts @{ Gpu = $true }); Show-Page 'terms'; Set-TermsBoxForTest $true; $ui.TermsView.Document.Blocks.LastBlock.BringIntoView() } },
        @{ Name = '05-terms-error'; Go = { Set-Facts (Get-CannedFacts @{ TermsVersion = '0.1' }); Show-Page 'welcome'; Show-Page 'terms' } },
        @{ Name = '06-choices-nvidia'; Go = { Set-Facts (Get-CannedFacts @{ Gpu = $true }); Set-TermsBoxForTest $true; Show-Page 'choose' }; Bottom = 'PageChoose' },
        @{ Name = '07-choices-no-nvidia-model-later'; Go = { Set-Facts (Get-CannedFacts @{ }); Set-TermsBoxForTest $true; $ui.ModelBox.IsChecked = $false; $ui.DesktopBox.IsChecked = $false; Update-Plan; Show-Page 'choose' }; Bottom = 'PageChoose' },
        @{ Name = '08-choices-update-low-disk'; Go = { Set-Facts (Get-CannedFacts @{ Gpu = $true; GpuReady = $true; ModelPresent = $true; Installed = '0.8.2'; LowDisk = $true }); Set-TermsBoxForTest $true; Show-Page 'choose' }; Bottom = 'PageChoose' },
        @{ Name = '09-choices-python-missing'; Go = { Set-Facts (Get-CannedFacts @{ NoPython = $true }); Set-TermsBoxForTest $true; Show-Page 'choose'}; Bottom = 'PageChoose' },
        @{ Name = '09c-python-downloading'; Go = { Set-Facts (Get-CannedFacts @{ NoPython = $true }); Set-TermsBoxForTest $true; Show-Page 'choose'
                $script:Py = @{ Phase = 'download' }; Set-PythonStatus 'Downloading Python... 11 MB of 27 MB' 0.42; Update-PythonUi }; Bottom = 'PageChoose' },
        @{ Name = '09c2-python-stop-waiting'; Go = { Set-Facts (Get-CannedFacts @{ NoPython = $true }); Set-TermsBoxForTest $true; Show-Page 'choose'
                $script:Py = @{ Phase = 'installing'; OfferStop = $true }; Set-PythonStatus 'The Python installer is taking a long time (more than 15 minutes). You can keep waiting, or stop waiting.' $null; Update-PythonUi }; Bottom = 'PageChoose' },
        @{ Name = '09d-python-error'; Go = { Set-Facts (Get-CannedFacts @{ NoPython = $true }); Set-TermsBoxForTest $true; Show-Page 'choose'
                $script:PyLastError = 'The downloaded file is not the one that was expected (its checksum does not match), so it was not used.'; Update-PythonUi }; Bottom = 'PageChoose' },
        @{ Name = '09e-python-manual'; Go = { Set-Facts (Get-CannedFacts @{ NoPython = $true }); Set-TermsBoxForTest $true; Show-Page 'choose'
                $ui.PyManual.IsExpanded = $true }; Bottom = 'PageChoose' },
        @{ Name = '10-installing'; Go = {
                Set-Facts (Get-CannedFacts @{ Gpu = $true }); Initialize-Progress @($script:Facts.steps); Show-Page 'install'
                foreach ($l in (Get-ProgressLines 5)) { Receive-ProgressLine $l }
                $ui.Bar.Tag = 'busy'; Flush-Log } },
        @{ Name = '10b-installing-details'; Go = {
                Set-Facts (Get-CannedFacts @{ Gpu = $true }); Initialize-Progress @($script:Facts.steps); Show-Page 'install'
                foreach ($l in (Get-ProgressLines 5)) { Receive-ProgressLine $l }
                $ui.Bar.Tag = 'busy'; $ui.DetailsExp.IsExpanded = $true; Flush-Log; $ui.LogBox.ScrollToEnd(); $ui.InstallScroll.UpdateLayout(); $ui.InstallScroll.ScrollToBottom(); $ui.LogBox.ScrollToEnd() } },
        @{ Name = '10u-updating'; Go = {
                Set-Facts (Get-CannedFacts @{ Gpu = $true; Installed = '0.8.2' }); Initialize-Progress @($script:Facts.steps); Show-Page 'install'
                foreach ($l in (Get-ProgressLines 5)) { Receive-ProgressLine $l }
                $ui.Bar.Tag = 'busy'; Flush-Log } },
        @{ Name = '11-stop-confirm'; Go = {
                Set-Facts (Get-CannedFacts @{ Gpu = $true }); Initialize-Progress @($script:Facts.steps); Show-Page 'install'
                foreach ($l in (Get-ProgressLines 5)) { Receive-ProgressLine $l }
                Flush-Log; Show-Confirm } },
        @{ Name = '12-success-warning'; Go = {
                Set-Facts (Get-CannedFacts @{ Gpu = $true }); Initialize-Progress @($script:Facts.steps)
                foreach ($l in (Get-ProgressLines 7)) { Receive-ProgressLine $l }
                Receive-ProgressLine '@@htt {"kind":"warning","text":"The NVIDIA runtime could not be installed. The app will recognise speech on the processor instead. Run install.bat again to retry."}'
                Receive-ProgressLine ('@@htt ' + (ConvertTo-Json -Compress -InputObject ([ordered]@{ kind = 'done'; installRoot = $script:Facts.layout.installRoot; desktopLnk = $script:Facts.layout.desktopLnk })))
                Complete-Install 0
                # as a person sees it (the test mode hides the box)
                $ui.StartNow.Visibility = 'Visible'; $ui.StartNow.IsChecked = $true }; Bottom = 'PageResult' },
        @{ Name = '12u-updated'; Go = {
                Set-Facts (Get-CannedFacts @{ Gpu = $true; Installed = '0.8.2' }); Initialize-Progress @($script:Facts.steps)
                foreach ($l in (Get-ProgressLines 7)) { Receive-ProgressLine $l }
                Receive-ProgressLine ('@@htt ' + (ConvertTo-Json -Compress -InputObject ([ordered]@{ kind = 'done'; installRoot = $script:Facts.layout.installRoot; desktopLnk = $script:Facts.layout.desktopLnk })))
                Complete-Install 0
                $ui.StartNow.Visibility = 'Visible'; $ui.StartNow.IsChecked = $true }; Bottom = 'PageResult' },
        @{ Name = '13-failure'; Go = {
                Set-Facts (Get-CannedFacts @{ Gpu = $true }); Initialize-Progress @($script:Facts.steps)
                foreach ($l in (Get-ProgressLines 5)) { Receive-ProgressLine $l }
                Receive-ProgressLine '@@htt {"kind":"error","text":"Installing the libraries failed. Check your internet connection and run install.bat again; it carries on where it stopped.\n\npip said: connection timed out (pypi.org)."}'
                Complete-Install 1 } },
        @{ Name = '14-stopped'; Go = {
                Set-Facts (Get-CannedFacts @{ Gpu = $true }); Initialize-Progress @($script:Facts.steps)
                foreach ($l in (Get-ProgressLines 5)) { Receive-ProgressLine $l }
                $script:StopRequested = $true; Complete-Install 1 } }
    )
    $sizes = @(@(780, 580), @(880, 470))
    $made = @()
    foreach ($sz in $sizes) {
        $win.Width = $sz[0]; $win.Height = $sz[1]
        foreach ($mode in 'light', 'dark') {
            Set-Theme $mode
            foreach ($s in $states) {
                Reset-Ui
                & $s.Go
                $name = '{0}x{1}-{2}-{3}.png' -f $sz[0], $sz[1], $mode, $s.Name
                Save-Shot (Join-Path $Folder $name)
                $made += $name
                if ($s.Bottom) {
                    # the same page scrolled to its end ("06b-..."), so all of it can be reviewed
                    $ui.Root.UpdateLayout()
                    $ui[$s.Bottom].ScrollToBottom()
                    $bname = '{0}x{1}-{2}-{3}.png' -f $sz[0], $sz[1], $mode, ($s.Name -replace '^(\d+)(u?)-', '${1}b$2-')
                    Save-Shot (Join-Path $Folder $bname)
                    $made += $bname
                }
            }
        }
    }
    $aware = 'unknown'
    if ('HttSetupNative' -as [type]) { $aware = [HttSetupNative]::IsProcessDPIAware() }
    [Console]::Out.WriteLine("dpi-aware: $aware")
    [Console]::Out.WriteLine(("saved {0} pictures to {1}" -f $made.Count, $Folder))
    $script:AllowClose = $true
    $win.Close()
}

# -- the self-test ------------------------------------------------------------------------------------------

function Invoke-SelfTest {
    $fails = New-Object Collections.Generic.List[string]
    function Check([bool]$Cond, [string]$What) { if (-not $Cond) { $fails.Add($What) } }
    function Press($Button) {
        # Like a click: refused when the button is disabled or hidden.
        if (-not $Button.IsEnabled -or $Button.Visibility -ne 'Visible') { throw "not pressable: $($Button.Name)" }
        $Button.RaiseEvent((New-Object Windows.RoutedEventArgs([Windows.Controls.Primitives.ButtonBase]::ClickEvent)))
    }
    function Pressable($Button) { return ($Button.IsEnabled -and $Button.Visibility -eq 'Visible') }
    Set-Theme 'light'

    # facts and the terms
    Set-Facts (Get-CannedFacts @{ Gpu = $true })
    Check ($script:TermsOk) 'TERMS.md was not accepted as the right version'
    Check ($ui.AgreeBox.IsChecked -ne $true) 'the terms box starts ticked'
    $blocks = @($ui.TermsView.Document.Blocks)
    Check ($blocks.Count -gt 30) "the terms have too few blocks ($($blocks.Count))"
    Check (@($blocks | Where-Object { $_ -is [Windows.Documents.List] }).Count -ge 1) 'the summary bullets are not a list'
    $docText = (New-Object Windows.Documents.TextRange($ui.TermsView.Document.ContentStart, $ui.TermsView.Document.ContentEnd)).Text
    Check ($docText -match 'THE APP IS PROVIDED "AS IS"') 'section 9 is not shown as written'
    Check ($docText -notmatch '\*\*') 'bold marks are left in the text'
    Check ($docText -match '9\. NO WARRANTY') 'the heading of section 9 is missing'

    # welcome -> terms
    Show-Page 'welcome'
    Check (Pressable $ui.NextBtn) 'Next is not usable on the welcome page'
    Press $ui.NextBtn
    Check ($script:Page -eq 'terms') 'Next did not open the terms'
    Check ([string]$ui.NextBtn.Content -eq 'I agree') "the button is labelled '$($ui.NextBtn.Content)', not 'I agree'"
    Check (-not $ui.NextBtn.IsEnabled) '"I agree" is enabled before the box is ticked'
    $refused = $false
    try { Press $ui.NextBtn } catch { $refused = $true }
    Check $refused '"I agree" could be pressed while disabled'
    $peerRefused = $false
    try { (New-Object Windows.Automation.Peers.ButtonAutomationPeer($ui.NextBtn)).GetPattern('Invoke').Invoke() } catch { $peerRefused = $true }
    Check $peerRefused '"I agree" could be invoked while disabled'
    Check ($script:Page -eq 'terms') 'the page moved on without agreeing'
    Set-TermsBoxForTest $true
    Check ($ui.NextBtn.IsEnabled) '"I agree" is not enabled after ticking'
    Set-TermsBoxForTest $false
    Check (-not $ui.NextBtn.IsEnabled) '"I agree" stays enabled after unticking'
    Set-TermsBoxForTest $true
    Press $ui.NextBtn
    Check ($script:Page -eq 'choose') '"I agree" did not open the choices'
    Check ([string]$ui.NextBtn.Content -eq 'Install') 'the choices page has no Install button'
    Check ($ui.NextBtn.IsEnabled) 'Install is not enabled with the box ticked'

    # the engine arguments
    $a = Get-InstallArgs
    $iv = [array]::IndexOf($a, '-AcceptedTerms')
    Check ($iv -ge 0 -and $a[$iv + 1] -eq $script:TermsVersion) "-AcceptedTerms is missing: $($a -join ' ')"
    Check ($a[0] -eq '-Progress') '-Progress is not first'
    foreach ($pair in @(@('-Nvidia', 'yes'), @('-Model', 'yes'), @('-DesktopShortcut', 'yes'))) {
        $k = [array]::IndexOf($a, $pair[0]); Check ($k -ge 0 -and $a[$k + 1] -eq $pair[1]) "$($pair[0]) is not $($pair[1]): $($a -join ' ')"
    }
    Check ($ui.ModelWhat.Text -match 'large-v3-turbo') 'the NVIDIA model is not named'
    $ui.NvidiaBox.IsChecked = $false; $ui.ModelBox.IsChecked = $false; $ui.DesktopBox.IsChecked = $false
    $a = Get-InstallArgs
    foreach ($pair in @(@('-Nvidia', 'no'), @('-Model', 'no'), @('-DesktopShortcut', 'no'))) {
        $k = [array]::IndexOf($a, $pair[0]); Check ($k -ge 0 -and $a[$k + 1] -eq $pair[1]) "$($pair[0]) is not $($pair[1]): $($a -join ' ')"
    }
    Check ($ui.ModelWhat.Text -match 'small') 'without NVIDIA the processor model is not named'
    Check ($ui.ModelOff.Visibility -eq 'Visible') 'the "first start downloads it" note is not shown'
    Check ($ui.NvidiaOff.Visibility -eq 'Visible') 'the "processor, which is slower" note is not shown'
    $ui.NvidiaBox.IsChecked = $true; $ui.ModelBox.IsChecked = $true; $ui.DesktopBox.IsChecked = $true

    # Back, untick: Install is disabled again and no acceptance is passed
    Press $ui.BackBtn
    Check ($script:Page -eq 'terms') 'Back did not return to the terms'
    Check ($ui.NextBtn.IsEnabled) '"I agree" is not enabled on return although ticked'
    Set-TermsBoxForTest $false
    $script:Page = 'choose'; Update-Nav
    Check (-not $ui.NextBtn.IsEnabled) 'Install is enabled with the box unticked'
    Check ((Get-InstallArgs) -notcontains '-AcceptedTerms') '-AcceptedTerms is passed with the box unticked'
    $before = $script:EngineCalls.Count
    Check (-not (Start-Install)) 'Start-Install ran with the box unticked'
    Check ($script:EngineCalls.Count -eq $before) 'the engine was started with the box unticked'
    Show-Page 'terms'; Set-TermsBoxForTest $true; Press $ui.NextBtn

    # pass-through of the test overrides
    $saved = $script:InstallRoot
    $script:InstallRoot = 'C:\Users\you\tmp\prog'; $script:NoDeps = [switch]$true
    $a = Get-InstallArgs
    Check (($a -join '|') -match '\|-InstallRoot\|C:\\Users\\you\\tmp\\prog') 'the -InstallRoot override is not passed on'
    Check ($a -contains '-NoDeps') '-NoDeps is not passed on'
    $script:InstallRoot = $saved; $script:NoDeps = [switch]$false

    # Python missing
    Set-Facts (Get-CannedFacts @{ NoPython = $true })
    Set-TermsBoxForTest $true
    Show-Page 'choose'
    Check ($ui.PythonPanel.Visibility -eq 'Visible') 'the Python panel is not shown'
    Check (-not $ui.NextBtn.IsEnabled) 'Install is enabled without Python'
    Check ($ui.PythonNotes.Text -match 'Microsoft Store') 'the notes of what was looked at are missing'
    Check (Pressable $ui.CheckAgainBtn) 'Check again is not usable'
    $before = $script:EngineCalls.Count
    Press $ui.CheckAgainBtn
    Check ($script:EngineCalls.Count -eq $before + 1 -and $script:EngineCalls[$script:EngineCalls.Count - 1].Args[0] -eq '-Check') 'Check again did not run -Check'
    $script:PendingFacts = Get-CannedFacts @{ Gpu = $false }
    Complete-Check 0
    Check ($ui.PythonPanel.Visibility -ne 'Visible') 'the Python panel stays after Python was found'
    Check ($ui.NextBtn.IsEnabled) 'Install is not enabled after Check again found Python (the box was ticked)'
    # a check that reports another version of the terms takes the tick away
    $script:PendingFacts = Get-CannedFacts @{ TermsVersion = '7.7' }
    Complete-Check 0
    Check ($ui.AgreeBox.IsChecked -ne $true) 'a tick given to other terms was kept'
    Check (-not $ui.NextBtn.IsEnabled) 'Install is enabled after the terms changed'

    # marker parsing
    Set-Facts (Get-CannedFacts @{ Gpu = $true })
    Initialize-Progress @($script:Facts.steps)
    $script:Page = 'install'
    $lines = @(
        'plain text line',
        '@@htt {"kind":"step","n":1,"total":7,"title":"Looking for Python"}',
        '@@htt {"kind":"step","n":2,"total":7,"title":"Checking the files"}',
        '@@htt {"kind":"activity","text":"Installing the libraries the app needs"}',
        '@@htt {broken json',
        '@@htt {"no":"kind"}',
        '@@htt 42',
        '@@htt ',
        '@@htt {"kind":"step","n":"x","total":null}',
        '@@htt {"kind":"mystery"}',
        '@@htt {"kind":"warning","text":"First warning."}',
        '@@htt {"kind":"error","text":"line one\nline two"}'
    )
    foreach ($l in $lines) { Receive-ProgressLine $l }
    Flush-Log
    Check ($script:CurrentStep -eq 2) "the current step is $($script:CurrentStep), not 2"
    Check ($script:StepRows[0].State -eq 'done' -and $script:StepRows[1].State -eq 'current' -and $script:StepRows[2].State -eq 'pending') 'the step states are wrong'
    Check ($script:StepRows[1].Act.Text -match 'libraries') 'the activity is not shown under the step'
    Check ($script:Warnings.Count -eq 1) "warnings: $($script:Warnings.Count)"
    Check ($script:ErrorText -eq "line one`nline two") 'the error text lost its line break'
    Check ($ui.LogBox.Text -match 'broken json') 'a broken marker is not in the details'
    Check ($ui.LogBox.Text -match 'plain text line') 'a plain line is not in the details'
    Check ([math]::Abs($ui.Bar.Value - (1.0 / 7)) -lt 0.001) "the bar is at $($ui.Bar.Value)"
    Check ($null -eq (ConvertFrom-Marker '@@htt ["kind"]')) 'a JSON array was taken as a marker'
    Check ($null -eq (ConvertFrom-Marker '@@htt {"kind":5}')) 'a number kind was taken as a marker'
    Check ($null -eq (ConvertFrom-Marker 'x @@htt {"kind":"step"}')) 'a marker in mid-line was taken'
    Complete-Install 1
    Check ($script:Page -eq 'result' -and $script:ResultKind -eq 'failure' -and $script:Outcome -eq 1) 'a failure does not show the failure page'
    Check ($ui.ErrorBox.Text -eq "line one`r`nline two") 'the error box lost the line break'
    Check ((Pressable $ui.BackBtn) -and [string]$ui.BackBtn.Content -eq 'Try again') 'there is no Try again'
    Check (Pressable $ui.CopyBtn) 'there is no Copy details'

    # success, with a warning
    Initialize-Progress @($script:Facts.steps); $script:Page = 'install'
    foreach ($l in @('@@htt {"kind":"step","n":7,"total":7,"title":"Shortcuts"}', '@@htt {"kind":"warning","text":"W"}',
                     '@@htt {"kind":"done","installRoot":"C:\\Users\\you\\p","desktopLnk":null,"launch":{"exe":"x"}}')) { Receive-ProgressLine $l }
    Complete-Install 0
    Check ($script:ResultKind -eq 'success' -and $script:Outcome -eq 0) 'success is not shown'
    Check ($ui.WarnList.Children.Count -eq 1) 'the warning is not on the finish page'
    Check ($ui.DesktopHow.Visibility -ne 'Visible') 'the desktop line shows although no shortcut was made'
    Check ($ui.StartNow.Visibility -ne 'Visible') '"Start Hold to talk now" shows in test mode'
    Check ($script:StepRows[0].State -eq 'done' -and $script:StepRows[6].State -eq 'done') 'done does not tick every step'
    Press $ui.NextBtn
    Check ($script:ClosedWith -eq 0) "Close after success gave $($script:ClosedWith)"

    # exit 0 without a done marker is a failure; stop gives 2
    Initialize-Progress @($script:Facts.steps); $script:Page = 'install'
    Complete-Install 0
    Check ($script:ResultKind -eq 'failure') 'exit 0 without done counts as success'
    Initialize-Progress @($script:Facts.steps); $script:Page = 'install'
    $script:StopRequested = $true
    Complete-Install 1
    Check ($script:ResultKind -eq 'stopped' -and $script:Outcome -eq 2) 'a stop is not shown as stopped'

    # Esc while installing asks; cancel at the terms gives 3
    Reset-Ui
    Set-Facts (Get-CannedFacts @{ Gpu = $true })
    Initialize-Progress @($script:Facts.steps)
    Show-Page 'install'
    $script:Run = @{ Kind = 'progress'; Proc = $null }
    Invoke-Cancel
    Check ($ui.Overlay.Visibility -eq 'Visible') 'Cancel while installing does not ask "Stop installing?"'
    Check ($null -eq $script:ClosedWith) 'Cancel while installing closed the window'
    Invoke-Cancel
    Check ($ui.Overlay.Visibility -ne 'Visible') 'Esc does not dismiss the question'
    $script:Run = $null
    Reset-Ui
    Set-Facts (Get-CannedFacts @{ Gpu = $true })
    Show-Page 'terms'
    Press $ui.CancelBtn
    Check ($script:ClosedWith -eq 3) "Cancel at the terms gave $($script:ClosedWith), not 3"
    Reset-Ui
    Show-Page 'choose'
    Invoke-Cancel
    Check ($script:ClosedWith -eq 3) "Esc on the choices page gave $($script:ClosedWith), not 3"

    # a TERMS.md of another version: no install possible
    Reset-Ui
    Set-Facts (Get-CannedFacts @{ TermsVersion = '0.1' })
    Check (-not $script:TermsOk) 'terms of another version were accepted'
    Show-Page 'welcome'; Press $ui.NextBtn
    Check ($script:Page -eq 'error') 'terms of another version do not show the error page'
    Check (-not (Start-Install)) 'Start-Install ran with the wrong terms'

    # markers are trusted only from standard output
    Reset-Ui
    Set-Facts (Get-CannedFacts @{ Gpu = $true })
    Initialize-Progress @($script:Facts.steps); $script:Page = 'install'
    Receive-EngineLine '@@htt {"kind":"done"}' 'progress' $false
    Receive-EngineLine '@@htt {"kind":"error","text":"forged"}' 'progress' $false
    Receive-EngineLine '@@htt {"kind":"warning","text":"forged"}' 'progress' $false
    Receive-EngineLine '@@htt {"kind":"step","n":3,"total":7,"title":"forged"}' 'progress' $false
    Flush-Log
    Check ($null -eq $script:DoneInfo) 'a done marker on stderr was believed'
    Check ($null -eq $script:ErrorText) 'an error marker on stderr was believed'
    Check ($script:Warnings.Count -eq 0) 'a warning marker on stderr was believed'
    Check ($script:CurrentStep -eq 0) 'a step marker on stderr was believed'
    Check ($ui.LogBox.Text -match 'forged') 'a marker on stderr is not in the details as a plain line'
    $script:CheckLog.Clear(); $script:CheckOut.Clear(); $script:PendingFacts = $null
    Receive-EngineLine ('@@htt ' + (ConvertTo-Json -Compress -InputObject @{ kind = 'facts' })) 'check' $false
    Check ($null -eq $script:PendingFacts) 'a facts marker on stderr was believed'
    Receive-EngineLine ('@@htt ' + (ConvertTo-Json -Compress -InputObject @{ kind = 'facts' })) 'check' $true
    Check ($null -ne $script:PendingFacts) 'a facts marker on stdout was not believed'
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
    $script:DoneInfo = $null; $script:ErrorText = $null
    Receive-RawLine $got[2] 'progress' $true
    Flush-Log
    Check ($null -eq $script:ErrorText) 'a marker after a lone carriage return was believed'
    Check ($ui.LogBox.Text -match 'forged') 'the text after a lone carriage return is not in the details'
    Receive-RawLine $got[1] 'progress' $true
    Check ($null -ne $script:DoneInfo) 'a normal marker was no longer believed after the reader change'
    $script:DoneInfo = $null

    # step markers: only a whole number from 1 to the number of titles
    Initialize-Progress @($script:Facts.steps); $script:Page = 'install'
    $rows = $script:StepRows.Count
    foreach ($l in @('@@htt {"kind":"step","n":99999999999,"total":99999999999,"title":"x"}',
                     '@@htt {"kind":"step","n":2147483648,"total":2147483648,"title":"x"}',
                     '@@htt {"kind":"step","n":"3","total":7,"title":"x"}', '@@htt {"kind":"step","n":3.5,"total":7,"title":"x"}',
                     '@@htt {"kind":"step","n":0,"total":7,"title":"x"}', '@@htt {"kind":"step","n":-4,"total":7,"title":"x"}',
                     '@@htt {"kind":"step","n":8,"total":7,"title":"x"}', '@@htt {"kind":"step","n":null,"total":null}',
                     '@@htt {"kind":"step","n":1e999,"total":1e999}', '@@htt {"kind":"step","n":[1],"total":{"a":1}}')) {
        Receive-ProgressLine $l
    }
    Check ($script:StepRows.Count -eq $rows) "a step marker changed the step list ($($script:StepRows.Count) rows, not $rows)"
    Check ($script:CurrentStep -eq 0) "a bad step marker moved the current step to $($script:CurrentStep)"
    Receive-ProgressLine '@@htt {"kind":"step","n":2,"total":99999999,"title":"Checking the files"}'
    Check ($script:CurrentStep -eq 2 -and $script:StepRows.Count -eq $rows) 'a good step number with a huge total was not taken as step 2 of the facts'
    Check ($ui.StepCount.Text -eq "Step 2 of $rows") "the step count says '$($ui.StepCount.Text)'"
    Initialize-Progress @(1..100 | ForEach-Object { "step $_" })
    Check ($script:StepRows.Count -eq $script:MaxSteps) 'a huge list of step titles was not capped'
    Initialize-Progress @($script:Facts.steps)

    # the kept log is capped; the oldest lines go first
    for ($k = 1; $k -le ($script:MaxLogLines + 500); $k++) { Add-Log "line $k" }
    Check ($script:FullLog.Count -eq $script:MaxLogLines) "the kept log has $($script:FullLog.Count) lines"
    Check ($script:FullLog[0] -eq 'line 501') 'the oldest lines were not the ones dropped'
    Check ((Get-LogText) -match "line $($script:MaxLogLines + 500)$") 'the newest line is missing from the copy text'
    Flush-Log
    Initialize-Progress @($script:Facts.steps)

    # "Start Hold to talk now": computed from the facts, whatever a marker says
    $script:DoneInfo = [pscustomobject]@{ kind = 'done'; launch = [pscustomobject]@{ exe = 'C:\Forged\evil.exe'; args = '--evil'; workDir = 'C:\Forged' } }
    $sc = Get-StartCommand
    $L = $script:Facts.layout
    Check ($sc.Exe -eq (Join-Path $L.envDir 'Scripts\pythonw.exe')) "the start program is $($sc.Exe)"
    Check ($sc.Args -eq ('"' + (Join-Path $L.installRoot 'dictate.py') + '" --open')) "the start arguments are $($sc.Args)"
    Check ($sc.WorkDir -eq $L.installRoot) "the start folder is $($sc.WorkDir)"
    Check ($sc.Exe -notmatch 'Forged' -and $sc.Args -notmatch 'evil') 'a forged launch was used'
    $script:DoneInfo = $null

    # Open folder: only an existing folder
    $ui.ChooseNote.Visibility = 'Collapsed'
    Open-Folder 'C:\Users\you\no\such\folder'
    Check ($ui.ChooseNote.Visibility -ne 'Visible') 'Open folder acted on a folder that does not exist'
    Open-Folder ([IO.Path]::GetTempPath())
    Check ($ui.ChooseNote.Visibility -eq 'Visible') 'Open folder did nothing for an existing folder'

    # the rail on the failure and the stopped pages
    Reset-Ui
    Set-Facts (Get-CannedFacts @{ Gpu = $true })
    foreach ($kind in 'failure', 'stopped') {
        Initialize-Progress @($script:Facts.steps); $script:Page = 'install'
        if ($kind -eq 'stopped') { $script:StopRequested = $true }
        Complete-Install 1
        Check ($ui.RailNum4.Text -eq '!') "the rail shows '$($ui.RailNum4.Text)' for step 4 after a $kind"
        Check ($ui.RailText5.FontWeight.ToString() -eq 'Normal' -and $ui.RailText4.FontWeight.ToString() -eq 'SemiBold') "the rail marks step 5 as reached after a $kind"
        Check ([int][char]$ui.RailNum3.Text[0] -eq 0xE73E) "step 3 is not ticked after a $kind"
    }
    Initialize-Progress @($script:Facts.steps); $script:Page = 'install'
    Receive-ProgressLine '@@htt {"kind":"done","installRoot":"C:\\x"}'
    Complete-Install 0
    Check ($ui.RailNum4.Text -ne '!' -and [int][char]$ui.RailNum4.Text[0] -eq 0xE73E -and $ui.RailText5.FontWeight.ToString() -eq 'SemiBold') 'the success page does not tick step 4 and show step 5 as current'

    # install, update and repair: the wording follows the facts
    foreach ($case in @(
        @{ Mode = 'install'; Installed = $null; Verb = 'Install'; Ger = 'Installing'; Past = 'installed'; Rail = 'Setup'; Noun = 'Installation'; Stop = 'Installation stopped'; Lead = 'Version 0.9.1 is installed in this folder:' },
        @{ Mode = 'update'; Installed = '0.8.2'; Verb = 'Update'; Ger = 'Updating'; Past = 'updated'; Rail = 'Update'; Noun = 'Update'; Stop = 'Update stopped'; Lead = 'Version 0.9.1 is now installed in this folder:' },
        @{ Mode = 'repair'; Installed = '0.9.1'; Verb = 'Repair'; Ger = 'Repairing'; Past = 'repaired'; Rail = 'Repair'; Noun = 'Repair'; Stop = 'Repair stopped'; Lead = 'Version 0.9.1 is installed in this folder:' })) {
        $m = $case.Mode
        Reset-Ui
        Set-Facts (Get-CannedFacts @{ Gpu = $true; Installed = $case.Installed })
        Check ($script:Mode -eq $m) "${m}: the mode is $($script:Mode)"
        Check ($script:Window.Title -eq "$($case.Verb) Hold to talk") "${m}: the window title is '$($script:Window.Title)'"
        Check ($ui.RailSub.Text -eq $case.Rail) "${m}: the word under the logo is '$($ui.RailSub.Text)'"
        Check ($ui.RailText4.Text -eq $case.Ger) "${m}: rail step 4 says '$($ui.RailText4.Text)'"
        Check ($ui.RailText1.Text -eq 'Welcome' -and $ui.RailText2.Text -eq 'Terms of Use' -and $ui.RailText3.Text -eq 'Choices' -and $ui.RailText5.Text -eq 'Finish') "${m}: another rail step changed"
        Check ($ui.UpdateCard.Visibility -eq $(if ($m -eq 'install') { 'Collapsed' } else { 'Visible' })) "${m}: the welcome card is wrong"
        if ($m -eq 'update') { Check ($ui.UpdateText.Text -eq 'This updates Hold to talk from version 0.8.2 to 0.9.1. It does not install a second copy: your settings and history are kept.') "update: the welcome line is '$($ui.UpdateText.Text)'" }
        if ($m -eq 'repair') { Check ($ui.UpdateText.Text -eq 'Version 0.9.1 is already installed. This installs it again to repair it; your settings and history are kept.') "repair: the welcome line is '$($ui.UpdateText.Text)'" }
        if ($m -ne 'install') { Check ($ui.AccountText.Text -notmatch 'nstall') "${m}: the welcome page still says install in '$($ui.AccountText.Text)'" }
        # the terms page is the same in every mode
        Show-Page 'terms'
        Check ($ui.TermsLead.Text -eq "Please read the terms before you install. Version $($script:TermsVersion).") "${m}: the terms lead changed"
        Check ($ui.AgreeText.Text -eq "I have read and agree to the Terms of Use (version $($script:TermsVersion))") "${m}: the agree text changed"
        Check ([string]$ui.NextBtn.Content -eq 'I agree') "${m}: the terms button says '$($ui.NextBtn.Content)'"
        Set-TermsBoxForTest $true
        Show-Page 'choose'
        Check ([string]$ui.NextBtn.Content -eq $case.Verb) "${m}: the choices button says '$($ui.NextBtn.Content)'"
        Check ($ui.ChooseTitle.Text -eq "What will be $($case.Past)") "${m}: the choices title is '$($ui.ChooseTitle.Text)'"
        Check ($ui.ChooseSub.Text -eq "Everything goes into your own Windows account. Nothing changes until you choose $($case.Verb).") "${m}: the choices subtitle is '$($ui.ChooseSub.Text)'"
        Initialize-Progress @($script:Facts.steps)
        Show-Page 'install'
        Check ($ui.InstallTitle.Text -eq "$($case.Ger) Hold to talk") "${m}: the progress title is '$($ui.InstallTitle.Text)'"
        Check ([Windows.Automation.AutomationProperties]::GetName($ui.RailText4) -eq "Step 4 of 5, current: $($case.Ger)") "${m}: the rail's accessible name is '$([Windows.Automation.AutomationProperties]::GetName($ui.RailText4))'"
        Check ([Windows.Automation.AutomationProperties]::GetName($ui.Bar) -eq "$($case.Noun) progress") "${m}: the bar's accessible name is '$([Windows.Automation.AutomationProperties]::GetName($ui.Bar))'"
        Check ($ui.StopTitle.Text -eq "Stop $($case.Ger.ToLower())?") "${m}: the stop question is '$($ui.StopTitle.Text)'"
        Check ($ui.StopBtn.Content -eq "Stop $($case.Ger.ToLower())" -and $ui.KeepBtn.Content -eq "Keep $($case.Ger.ToLower())") "${m}: the stop buttons say '$($ui.StopBtn.Content)' / '$($ui.KeepBtn.Content)'"
        Receive-ProgressLine '@@htt {"kind":"done","installRoot":"C:\\x"}'
        Complete-Install 0
        Check ($ui.ResultTitle.Text -eq "Hold to talk is $($case.Past)") "${m}: the success title is '$($ui.ResultTitle.Text)'"
        Check ($ui.ResultLead.Text -eq $case.Lead) "${m}: the success lead is '$($ui.ResultLead.Text)'"
        Check ([Windows.Automation.AutomationProperties]::GetName($ui.RailText5) -eq 'Step 5 of 5, current: Finish') "${m}: the finish step changed"
        Initialize-Progress @($script:Facts.steps); $script:Page = 'install'
        Complete-Install 1
        Check ($ui.ResultTitle.Text -eq "Hold to talk could not be $($case.Past)") "${m}: the failure title is '$($ui.ResultTitle.Text)'"
        Check ($ui.ResultLead.Text -match 'carries on where it stopped') "${m}: the failure lead is '$($ui.ResultLead.Text)'"
        Check ([Windows.Automation.AutomationProperties]::GetName($ui.RailText4) -eq "Step 4 of 5, failed: $($case.Ger)") "${m}: the failed rail name is '$([Windows.Automation.AutomationProperties]::GetName($ui.RailText4))'"
        Initialize-Progress @($script:Facts.steps); $script:Page = 'install'
        $script:StopRequested = $true
        Complete-Install 1
        Check ($ui.ResultTitle.Text -eq $case.Stop) "${m}: the stopped title is '$($ui.ResultTitle.Text)'"
        Check ($ui.ResultLead.Text -match 'stopped the (installation|update|repair)\.') "${m}: the stopped lead is '$($ui.ResultLead.Text)'"
        if ($m -ne 'install') { Check ($ui.ResultLead.Text -notmatch 'installation') "${m}: the stopped lead says installation" }
        Check ($ui.TermsLead.Text -match 'before you install') "${m}: the terms lead was reworded"
    }

    # the mode follows the facts after Check again
    Reset-Ui
    Set-Facts (Get-CannedFacts @{ NoPython = $true; Installed = '0.8.2' })
    Check ($script:Mode -eq 'update') 'Check again: the first mode is not update'
    Set-TermsBoxForTest $true
    Show-Page 'choose'
    Press $ui.CheckAgainBtn
    $script:PendingFacts = Get-CannedFacts @{ Gpu = $false; Installed = '0.9.1' }
    Complete-Check 0
    Check ($script:Mode -eq 'repair' -and [string]$ui.NextBtn.Content -eq 'Repair' -and $script:Window.Title -eq 'Repair Hold to talk' -and $ui.RailText4.Text -eq 'Repairing') "Check again to the same version: $($script:Mode) / $($ui.NextBtn.Content) / $($script:Window.Title)"
    Press $ui.BackBtn; Show-Page 'choose'
    $script:Page = 'choose'; Start-Check $true
    $script:PendingFacts = Get-CannedFacts @{ Gpu = $false }
    Complete-Check 0
    Check ($script:Mode -eq 'install' -and [string]$ui.NextBtn.Content -eq 'Install' -and $script:Window.Title -eq 'Install Hold to talk' -and $ui.RailSub.Text -eq 'Setup' -and $ui.RailText4.Text -eq 'Installing' -and $ui.UpdateCard.Visibility -eq 'Collapsed') "Check again with nothing installed: $($script:Mode) / $($ui.NextBtn.Content) / $($script:Window.Title)"
    $script:PendingFacts = Get-CannedFacts @{ Gpu = $false; Installed = '0.8.2' }
    Complete-Check 0
    Check ($script:Mode -eq 'update' -and [string]$ui.NextBtn.Content -eq 'Update' -and $ui.InstallTitle.Text -eq 'Updating Hold to talk') "Check again to update: $($script:Mode) / $($ui.NextBtn.Content)"

    # "Install Python for me": the text before the click, the pin, the stub in test mode, the checks, cancel
    Reset-Ui
    Set-Facts (Get-CannedFacts @{ NoPython = $true })
    Show-Page 'choose'
    Check ($ui.PythonPanel.Visibility -eq 'Visible') 'python panel: not shown'
    Check ([string]$ui.PyInstallBtn.Content -eq 'Install Python for me' -and (Pressable $ui.PyInstallBtn)) 'python panel: no "Install Python for me" button'
    Check ($ui.PyInstallBtn.Style -eq $ui.Root.FindResource('Primary')) 'python panel: "Install Python for me" is not the primary button'
    Check ($ui.PythonOrgBtn.Style -ne $ui.Root.FindResource('Primary') -and (Pressable $ui.PythonOrgBtn) -and (Pressable $ui.CheckAgainBtn)) 'python panel: the two other buttons changed'
    Check ($ui.PyLead.Text -eq 'Python is a free program that Hold to talk needs. Choose the button and Setup downloads it from python.org (about 27 MB) and installs it for your Windows account only. Nothing is downloaded until you choose the button.') "python panel: the lead changed: $($ui.PyLead.Text)"
    Check ($ui.PyManual.Header -eq 'Details' -and -not $ui.PyManual.IsExpanded) 'python panel: the Details expander is missing or open'
    $what = $ui.PyLead.Text + ' ' + $ui.PyWhat.Text
    foreach ($needle in @('official Python 3.12.10 installer', 'python.org', 'about 27 MB', 'installed for this Windows account only', 'with the "py" launcher', 'no administrator rights', 'does not add python.exe to your PATH', 'no file associations', 'Nothing is downloaded until', 'fixed fingerprint', 'Python Software Foundation', 'ARM')) {
        Check ($what.Contains($needle)) "python panel: the text before the click lacks '$needle'"
    }
    Check ($what -notmatch 'PATH setting is not') 'python panel: still claims that PATH is untouched'
    Check ($ui.PyLicence.Text -match 'PSF License' -and $ui.PyLicence.Text -match 'own licence') 'python panel: the PSF License is not mentioned'
    Check ($ui.PyProgressPanel.Visibility -ne 'Visible' -and $ui.PyError.Visibility -ne 'Visible') 'python panel: progress or error shown before the click'
    $pin = $script:PyPin
    Check ($pin.Url -match '^https://www\.python\.org/ftp/python/3\.12\.\d+/python-3\.12\.\d+-amd64\.exe$' -and $pin.Url.Contains($pin.Version)) 'python pin: the address is not the official one'
    Check ($pin.Sha256 -match '^[0-9a-f]{64}$' -and $pin.Bytes -gt 1000000) 'python pin: checksum or size missing'
    Check (($pin.Args -join ' ') -eq '/quiet InstallAllUsers=0 PrependPath=0 Include_launcher=1 InstallLauncherAllUsers=0 Include_tcltk=1 Include_test=0 SimpleInstall=1 AssociateFiles=0 Shortcuts=0') 'python pin: the installer options changed'

    # in test mode the button calls the stub: nothing is downloaded, run or made
    $before = $script:EngineCalls.Count
    $tempBefore = @(Get-ChildItem -LiteralPath ([IO.Path]::GetTempPath()) -Directory -Filter 'htt-py-*' -ErrorAction SilentlyContinue).Count
    Check ($null -eq $script:PyOpen -and $null -eq $script:PyRun) 'python: a test hook is set before the test'
    Press $ui.PyInstallBtn
    Check ($script:PyInstallCalls.Count -eq 1) 'python: the stub was not called in test mode'
    Check ($null -eq $script:Py) 'python: a download was started in test mode'
    Check ($script:EngineCalls.Count -eq $before + 1 -and $script:EngineCalls[$script:EngineCalls.Count - 1].Args[0] -eq '-Check') 'python: the stub is not followed by Check again'
    Check (@(Get-ChildItem -LiteralPath ([IO.Path]::GetTempPath()) -Directory -Filter 'htt-py-*' -ErrorAction SilentlyContinue).Count -eq $tempBefore) 'python: a folder was made in test mode'
    $refusedDl = $false
    try { Start-PythonDownload } catch { $refusedDl = $true }
    Check $refusedDl 'python: the real download could be started in test mode'
    $script:Checking = $false; $script:Run = $null; Update-PythonUi
    [Environment]::SetEnvironmentVariable('HTT_FAKE_PYTHON_INSTALLED', $null, 'Process')

    # the two checks, with small local files and a fake signature reader (never the network)
    $pyTmp = Join-Path ([IO.Path]::GetTempPath()) ('htt-selftest-' + [Guid]::NewGuid().ToString('N'))
    New-Item -ItemType Directory -Path $pyTmp | Out-Null
    try {
        $good = Join-Path $pyTmp 'good.bin'
        [IO.File]::WriteAllBytes($good, [byte[]](1..200 | ForEach-Object { $_ % 251 }))
        $bad = Join-Path $pyTmp 'bad.bin'
        $bytes = [IO.File]::ReadAllBytes($good); $bytes[10] = 99; [IO.File]::WriteAllBytes($bad, $bytes)
        $big = Join-Path $pyTmp 'big.bin'
        $rng = New-Object Random 7; $bb = New-Object byte[] 3000000; $rng.NextBytes($bb); [IO.File]::WriteAllBytes($big, $bb)
        $realPin = $script:PyPin
        $testPin = { param($f) [pscustomobject]@{ Version = '0.0.1'; Url = $realPin.Url; HostName = $realPin.HostName; Bytes = [int64](Get-Item -LiteralPath $f).Length
                       Sha256 = (Get-FileSha256 $f); Signer = $realPin.Signer; Args = $realPin.Args } }
        $pinGood = & $testPin $good
        $pinBig = & $testPin $big
        $okSig = { param($p) @{ Status = 'Valid'; Subject = 'CN=Python Software Foundation, O=Python Software Foundation, C=US' } }
        $script:PyGetSignature = $okSig
        Check ($null -eq (Test-PythonInstaller $good $pinGood)) 'verify: a good file with a good signature was refused'
        Check ($null -ne (Test-PythonInstaller $bad $pinGood)) 'verify: a file with another hash was accepted'
        Check ((Test-PythonInstaller $bad $pinGood) -match 'checksum') 'verify: a hash mismatch does not say so'
        Check ($null -ne (Test-PythonInstaller (Join-Path $pyTmp 'nope.bin') $pinGood)) 'verify: a missing file was accepted'
        $pinSize = $pinGood.PSObject.Copy(); $pinSize.Bytes = $pinGood.Bytes + 1
        Check ($null -ne (Test-PythonInstaller $good $pinSize)) 'verify: a file of another size was accepted'
        foreach ($case in @(@('NotSigned', 'CN=Python Software Foundation'), @('HashMismatch', 'CN=Python Software Foundation'), @('UnknownError', 'CN=Python Software Foundation'),
                            @('Valid', 'CN=Somebody Else, O=Somebody Else'), @('Valid', ''))) {
            $st = $case[0]; $sj = $case[1]
            $script:PyGetSignature = { param($p) @{ Status = $st; Subject = $sj } }.GetNewClosure()
            Check ($null -ne (Test-PythonInstaller $good $pinGood)) "verify: signature $st / '$sj' was accepted"
        }
        $script:PyGetSignature = { param($p) throw 'no signature reader' }
        Check ($null -ne (Test-PythonInstaller $good $pinGood)) 'verify: a failing signature reader was accepted'
        # the real reader: an unsigned file is not Valid; a file Windows signed is Valid but not by the PSF
        $script:PyGetSignature = $null
        Check ($null -ne (Test-PythonInstaller $good $pinGood)) 'verify: an unsigned file was accepted by the real reader'
        $cmdExe = Join-Path $env:SystemRoot 'System32\cmd.exe'
        $realSig = Get-InstallerSignature $cmdExe
        if ($realSig.Status -eq 'Valid') {
            Check ($realSig.Subject -notmatch 'Python Software Foundation') 'verify: cmd.exe claims to be the PSF'
            $pinCmd = & $testPin $cmdExe
            Check ((Test-PythonInstaller $cmdExe $pinCmd) -match 'not signed by the Python Software Foundation') 'verify: a file signed by someone else was accepted'
        }

        # the whole run with a local source and a fake installer program
        $script:PyGetSignature = $okSig
        $script:Ran = New-Object Collections.Generic.List[string]
        $script:PyRun = { param($f, $a) $script:Ran.Add("$f|$a"); [pscustomobject]@{ HasExited = $true; ExitCode = $script:FakeExit } }
        function Invoke-PyRunToEnd([string]$Src, $UsePin) {
            $script:PyPin = $UsePin
            $script:PyOpen = { param($u) @{ Stream = [IO.File]::OpenRead($script:PySrc); Length = [int64](Get-Item -LiteralPath $script:PySrc).Length } }
            $script:PySrc = $Src
            $script:PyLastError = $null; $script:Ran.Clear()
            $script:Checking = $false; $script:Run = $null
            Start-PythonInstall
            $dir = $null
            for ($k = 0; $k -lt 400 -and $script:Py; $k++) {
                if ($script:Py.Dir) { $dir = $script:Py.Dir }
                Step-PythonRun
            }
            return $dir
        }
        $script:FakeExit = 0
        $calls = $script:EngineCalls.Count
        $dir = Invoke-PyRunToEnd $good $pinGood
        Check ($null -eq $script:Py -and $null -eq $script:PyLastError) "python run: did not finish cleanly ($script:PyLastError)"
        Check ($script:Ran.Count -eq 1 -and $script:Ran[0].EndsWith('|/quiet InstallAllUsers=0 PrependPath=0 Include_launcher=1 InstallLauncherAllUsers=0 Include_tcltk=1 Include_test=0 SimpleInstall=1 AssociateFiles=0 Shortcuts=0')) "python run: the installer was run with '$($script:Ran -join ';')'"
        Check ($dir -and -not (Test-Path -LiteralPath $dir)) 'python run: the installer folder is left behind after a good run'
        Check ($script:EngineCalls.Count -eq $calls + 1 -and $script:EngineCalls[$script:EngineCalls.Count - 1].Args[0] -eq '-Check') 'python run: the page does not run Check again after the install'
        $script:Checking = $false; $script:Run = $null
        # a wrong hash, a bad signature: refused, nothing is run, the file is deleted, a plain error is shown
        $dir = Invoke-PyRunToEnd $bad $pinGood
        Check ($script:Ran.Count -eq 0) 'python run: a file with the wrong hash was run'
        Check ($dir -and -not (Test-Path -LiteralPath $dir)) 'python run: the wrong file was not deleted'
        Check ($script:PyLastError -match 'checksum' -and $ui.PyError.Visibility -eq 'Visible' -and $ui.PyError.Text -match 'checksum') 'python run: no plain error for a wrong hash'
        $script:PyGetSignature = { param($p) @{ Status = 'NotSigned'; Subject = '' } }
        $dir = Invoke-PyRunToEnd $good $pinGood
        Check ($script:Ran.Count -eq 0) 'python run: a file with a bad signature was run'
        Check ($dir -and -not (Test-Path -LiteralPath $dir)) 'python run: the badly signed file was not deleted'
        Check ($script:PyLastError -match 'no valid signature') 'python run: no plain error for a bad signature'
        $script:PyGetSignature = { param($p) @{ Status = 'Valid'; Subject = 'CN=Somebody Else' } }
        $dir = Invoke-PyRunToEnd $good $pinGood
        Check ($script:Ran.Count -eq 0 -and $script:PyLastError -match 'not signed by the Python Software Foundation') 'python run: a signature of someone else was run'
        $script:PyGetSignature = $okSig
        # a server that sends a different size
        $dir = Invoke-PyRunToEnd $big $pinGood
        Check ($script:Ran.Count -eq 0 -and $null -eq $script:Py -and $script:PyLastError -match 'size') 'python run: a source of another size was used'
        # a non-zero exit code of the installer is a failure
        $script:FakeExit = 1603
        $dir = Invoke-PyRunToEnd $good $pinGood
        Check ($script:Ran.Count -eq 1 -and $script:PyLastError -match '1603' -and $null -eq $script:Py) 'python run: a failing installer is not reported'
        Check ($dir -and -not (Test-Path -LiteralPath $dir)) 'python run: the folder is left behind after a failing installer'
        $script:FakeExit = 0
        # Cancel: stops, and deletes the partial file
        $script:PyPin = $pinBig
        $script:PySrc = $big
        $script:PyOpen = { param($u) @{ Stream = [IO.File]::OpenRead($script:PySrc); Length = [int64](Get-Item -LiteralPath $script:PySrc).Length } }
        $script:PyLastError = $null; $script:Ran.Clear()
        Start-PythonInstall
        $dir = $script:Py.Dir
        for ($k = 0; $k -lt 3; $k++) { Step-PythonRun }
        $partial = Join-Path $dir ('python-' + $pinBig.Version + '-amd64.exe')
        Check ($script:Py.Phase -eq 'download' -and (Test-Path -LiteralPath $partial) -and $script:Py.Got -gt 0 -and $script:Py.Got -lt $pinBig.Bytes) 'python cancel: the test did not get a partial file'
        Check (Pressable $ui.PyCancelBtn) 'python cancel: no Cancel button while downloading'
        Check (-not $ui.CheckAgainBtn.IsEnabled -and -not $ui.PyInstallBtn.IsEnabled) 'python: the other buttons are usable while downloading'
        Press $ui.PyCancelBtn
        Check ($null -eq $script:Py) 'python cancel: the run goes on'
        Check (-not (Test-Path -LiteralPath $partial) -and -not (Test-Path -LiteralPath $dir)) 'python cancel: the partial file was not deleted'
        Check ($script:Ran.Count -eq 0) 'python cancel: something was run'
        Check ($ui.PythonNote.Text -match 'cancelled' -and $ui.PyCancelBtn.Visibility -ne 'Visible' -and (Pressable $ui.PyInstallBtn)) 'python cancel: the panel did not return to its first state'
        # Esc during the download cancels it too, and the window stays
        Start-PythonInstall
        $dir = $script:Py.Dir
        Step-PythonRun
        Invoke-Cancel
        Check ($null -eq $script:Py -and -not (Test-Path -LiteralPath $dir) -and $null -eq $script:ClosedWith) 'python cancel: Esc did not cancel the download alone'
        # a Python installer that is running is not interrupted by Esc or by closing
        $script:PyRun = { param($f, $a) [pscustomobject]@{ HasExited = $false; ExitCode = 0 } }
        Start-PythonInstall
        $dir = $script:Py.Dir
        for ($k = 0; $k -lt 400 -and $script:Py -and $script:Py.Phase -ne 'installing'; $k++) { Step-PythonRun }
        Check ($script:Py.Phase -eq 'installing' -and $ui.PyCancelBtn.Visibility -ne 'Visible') 'python: the installing phase was not reached or shows Cancel'
        Invoke-Cancel
        Check ($null -ne $script:Py -and $null -eq $script:ClosedWith) 'python: Esc interrupted a running Python installer'
        $ev = New-Object ComponentModel.CancelEventArgs
        On-Closing $ev
        Check $ev.Cancel 'python: the window could be closed while the Python installer runs'
        # closing during the run asks the question "Stop waiting for Python?" instead of being refused outright
        Check ($ui.Overlay.Visibility -eq 'Visible' -and $ui.StopTitle.Text -match 'Stop waiting' -and $ui.StopBtn.Content -eq 'Stop waiting' -and $ui.KeepBtn.Content -eq 'Keep waiting') 'python: closing during the run did not ask "Stop waiting?"'
        Check ($null -ne $script:Py) 'python: the run ended before the question was answered'
        Press $ui.KeepBtn
        Check ($ui.Overlay.Visibility -ne 'Visible' -and $null -ne $script:Py -and $ui.StopTitle.Text -notmatch 'waiting') 'python: Keep waiting did not restore the window'
        # the same question from closing, answered with Stop waiting: ends only the process it started
        $script:Killed = 0
        $fake = [pscustomobject]@{ HasExited = $false; ExitCode = 0 }
        $fake | Add-Member -MemberType ScriptMethod -Name Kill -Value { $script:Killed++; $this.HasExited = $true }
        $fake | Add-Member -MemberType ScriptMethod -Name WaitForExit -Value { param($ms) return $true }
        $script:Py.Proc = $fake
        $script:ClosedWith = $null
        $ev = New-Object ComponentModel.CancelEventArgs
        On-Closing $ev
        Check ($ev.Cancel -and $ui.Overlay.Visibility -eq 'Visible') 'python: the second close did not ask again'
        Press $ui.StopBtn
        Check ($script:Killed -eq 1 -and $null -eq $script:Py -and $script:ClosedWith -eq (Get-CancelCode)) 'python: Stop waiting from the close question did not end the process and close'
        Check (-not (Test-Path -LiteralPath $dir)) 'python: the folder was left behind after Stop waiting'
        Check ($script:PyLastError -match 'Stopped waiting' -and $ui.PyManual.IsExpanded) 'python: Stop waiting did not show the manual route'
        Check ($ui.StopTitle.Text -notmatch 'waiting') 'python: the question texts were not restored'
        $script:ClosedWith = $null; $script:PyLastError = $null; $ui.PyManual.IsExpanded = $false
        # after the timeout the window offers "Stop waiting"
        $script:PyTimeoutSeconds = 0
        Start-PythonInstall
        $dir = $script:Py.Dir
        for ($k = 0; $k -lt 400 -and $script:Py -and $script:Py.Phase -ne 'installing'; $k++) { Step-PythonRun }
        Check (-not (Pressable $ui.PyStopBtn)) 'python: "Stop waiting" is shown before the timeout was seen'
        Step-PythonRun
        Check ($script:Py.OfferStop -and (Pressable $ui.PyStopBtn) -and $ui.PyStatus.Text -match 'taking a long time') 'python: no "Stop waiting" after the timeout'
        $script:Killed = 0
        $fake2 = [pscustomobject]@{ HasExited = $false; ExitCode = 0 }
        $fake2 | Add-Member -MemberType ScriptMethod -Name Kill -Value { $script:Killed++; $this.HasExited = $true }
        $fake2 | Add-Member -MemberType ScriptMethod -Name WaitForExit -Value { param($ms) return $true }
        $script:Py.Proc = $fake2
        Press $ui.PyStopBtn
        Check ($ui.Overlay.Visibility -eq 'Visible' -and $script:Killed -eq 0) 'python: the button killed the process without asking'
        Press $ui.StopBtn
        Check ($script:Killed -eq 1 -and $null -eq $script:Py -and -not (Test-Path -LiteralPath $dir)) 'python: Stop waiting did not end and clean up'
        Check ($null -eq $script:ClosedWith) 'python: Stop waiting closed the window'
        Check ($script:PyLastError -match 'Stopped waiting' -and -not (Pressable $ui.PyStopBtn)) 'python: the panel did not return to its first state after Stop waiting'
        $script:PyTimeoutSeconds = 900; $script:PyLastError = $null; $ui.PyManual.IsExpanded = $false
        # exit code 3010 (restart may be needed) is a success
        $script:FakeExit = 3010
        $script:PyRun = { param($f, $a) [pscustomobject]@{ HasExited = $true; ExitCode = $script:FakeExit } }
        $dir = Invoke-PyRunToEnd $good $pinGood
        Check ($null -eq $script:Py -and $null -eq $script:PyLastError -and $ui.PythonNote.Text -match 'restart later') "python: exit code 3010 was not a success ($script:PyLastError / $($ui.PythonNote.Text))"
        $script:Checking = $false; $script:Run = $null
        # the file is held open (read, nobody may write) from the check until the installer has started
        $script:LockSeen = $null
        $script:PyRun = { param($f, $a, $lock)
            $script:LockSeen = @{ Open = ($null -ne $lock); Write = $null; Rename = $null }
            try { $w = [IO.File]::Open($f, 'Open', 'Write', 'ReadWrite'); $w.Dispose(); $script:LockSeen.Write = $true } catch { $script:LockSeen.Write = $false }
            try { Rename-Item -LiteralPath $f -NewName 'moved.exe' -ErrorAction Stop; $script:LockSeen.Rename = $true; Rename-Item -LiteralPath (Join-Path (Split-Path -Parent $f) 'moved.exe') -NewName (Split-Path -Leaf $f) } catch { $script:LockSeen.Rename = $false }
            [pscustomobject]@{ HasExited = $true; ExitCode = 0 } }
        $dir = Invoke-PyRunToEnd $good $pinGood
        Check ($script:LockSeen -and $script:LockSeen.Open -and -not $script:LockSeen.Write -and -not $script:LockSeen.Rename) 'python: the installer file was not locked against writing and renaming while the installer started'
        Check ($dir -and -not (Test-Path -LiteralPath $dir)) 'python: the folder was left behind (a handle on the file was not released)'
        $script:Checking = $false; $script:Run = $null
        # a folder that cannot be made private stops the run with a plain error
        $script:PyRun = { param($f, $a) [pscustomobject]@{ HasExited = $true; ExitCode = 0 } }
        $script:FailAcl = $true
        $script:Ran.Clear()
        $dir = Invoke-PyRunToEnd $good $pinGood
        $script:FailAcl = $false
        Check ($null -eq $script:Py -and $script:Ran.Count -eq 0 -and $script:PyLastError -match 'private folder') "python: a failing ACL did not stop the run ($script:PyLastError)"
        # (an existing run continues below)
        $script:PyRun = { param($f, $a) [pscustomobject]@{ HasExited = $false; ExitCode = 0 } }
        Start-PythonInstall
        $dir = $script:Py.Dir
        for ($k = 0; $k -lt 400 -and $script:Py -and $script:Py.Phase -ne 'installing'; $k++) { Step-PythonRun }
        $script:Py.Proc = [pscustomobject]@{ HasExited = $true; ExitCode = 0 }
        Step-PythonRun
        Check ($null -eq $script:Py -and -not (Test-Path -LiteralPath $dir)) 'python: the run did not end after the installer ended'
        $script:Checking = $false; $script:Run = $null
    } finally {
        $script:PyOpen = $null; $script:PyRun = $null; $script:PyGetSignature = $null; $script:PyPin = $realPin
        $script:Py = $null; $script:PyLastError = $null; $script:Checking = $false; $script:Run = $null
        Remove-Item -LiteralPath $pyTmp -Recurse -Force -ErrorAction SilentlyContinue
    }
    Check (@(Get-ChildItem -LiteralPath ([IO.Path]::GetTempPath()) -Directory -Filter 'htt-py-*' -ErrorAction SilentlyContinue).Count -eq $tempBefore) 'python: a folder htt-py-* is left in the temp folder'
    $script:ClosedWith = $null
    Update-PythonUi

    # the sandbox rule of -AutoRun and -Engine
    $tmp = [IO.Path]::GetTempPath().TrimEnd('\')
    $ok4 = @("$tmp\htt-x\prog", "$tmp\htt-x\home\venv", "$tmp\htt-x\sm", "$tmp\htt-x\dt")
    $key = 'Software\HoldToTalkTest\x\Uninstall\HoldToTalk'
    Check ($null -eq (Get-SandboxProblem $ok4 $key)) "folders under the temp folder were refused: $(Get-SandboxProblem $ok4 $key)"
    Check ($null -ne (Get-SandboxProblem @($ok4[0], $ok4[1], $ok4[2], 'C:\Users\you\dt') $key)) 'a folder outside the temp folder was accepted'
    Check ($null -ne (Get-SandboxProblem @($ok4[0], $ok4[1], $ok4[2], "$tmp\..\elsewhere") $key)) 'a folder that climbs out of the temp folder was accepted'
    Check ($null -ne (Get-SandboxProblem @($ok4[0], $ok4[1], $ok4[2], $tmp) $key)) 'the temp folder itself was accepted'
    Check ($null -ne (Get-SandboxProblem $ok4 'Software\Other\x')) 'a key outside Software\HoldToTalkTest\ was accepted'
    Check ($null -ne (Get-SandboxProblem $ok4 'Software\HoldToTalkTest\')) 'the bare test key root was accepted'
    Check ($null -ne (Get-SandboxProblem $ok4 'Software\Microsoft\Windows\CurrentVersion\Uninstall\HoldToTalk')) 'the real uninstall key was accepted'
    Check ($null -eq (Get-SandboxProblem $ok4 'software\holdtotalktest\x')) 'the key prefix is case sensitive'
    Check ($null -eq (Get-SandboxProblem @($ok4[0].ToUpper(), $ok4[1], $ok4[2], $ok4[3]) $key)) 'the temp folder comparison is case sensitive'
    try {
        $short = (New-Object -ComObject Scripting.FileSystemObject).GetFolder($tmp).ShortPath
        Check ($null -eq (Get-SandboxProblem @("$short\htt-x\a", "$short\htt-x\b", "$short\htt-x\c", "$short\htt-x\d") $key)) 'the short (8.3) form of the temp folder was refused'
    } catch { }

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
        [Environment]::SetEnvironmentVariable('HOLDTOTALK_HOME', $tmp)
        Check ($null -ne (Get-SandboxProblem $ok4 $key)) 'sandbox: HOLDTOTALK_HOME equal to the temp folder was accepted'
        [Environment]::SetEnvironmentVariable('HOLDTOTALK_HOME', $null)
        [Environment]::SetEnvironmentVariable('HTT_ENV_DIR', 'C:\Users\you\venv')
        Check ((Get-SandboxProblem $ok4 $key) -match 'HTT_ENV_DIR') 'sandbox: HTT_ENV_DIR outside the temp folder was accepted'
    } finally {
        foreach ($n in $envOld.Keys) { [Environment]::SetEnvironmentVariable($n, $envOld[$n]) }
    }

    # the Setup.exe route: no text installer, a plain pointer to the project's page, documents opened from a copy
    $wasExe = $FromSetupExe
    try {
        $script:FromSetupExe = [switch]$false
        Check ((Get-DownloadAdvice $false) -match 'Extract the whole ZIP first') 'zip route: the extract advice changed'
        $script:FromSetupExe = [switch]$true
        foreach ($again in $true, $false) {
            $adv = Get-DownloadAdvice $again
            Check ($adv -match "project's page" -and $adv -match 'install\.bat' -and $adv -notmatch 'Extract the whole ZIP') "setup.exe route: the advice is '$adv'"
        }
        $before = $script:ClosedWith
        Open-TextInstaller
        Check ($script:ClosedWith -eq $before) 'setup.exe route: the text installer button closed the window'
        $env:HTT_FALLBACK_REASON = ''
        Start-TextInstallerFallback 'test'
        Check ([string]$env:HTT_FALLBACK_REASON -eq '') 'setup.exe route: the text installer fallback started'
        $text = [IO.File]::ReadAllText($ScriptPath)
        $text = $text.Substring(0, $text.IndexOf('function Invoke-' + 'SelfTest'))
        Check (([regex]::Matches($text, 'Extract the whole ZIP')).Count -eq 2) 'the Extract the whole ZIP wording is used outside Get-DownloadAdvice'
        # documents: a copy in a private folder of its own, deleted by Remove-DocumentCopies
        $copy = Get-DocumentPath 'TERMS.md'
        Check ($copy -ne (Join-Path $Here 'TERMS.md') -and (Test-Path -LiteralPath $copy) -and (Split-Path -Leaf (Split-Path -Parent $copy)) -match '^htt-doc-[0-9a-f]{32}$') "setup.exe route: the document was not copied to a private folder ($copy)"
        Check ((Get-Item -LiteralPath $copy).Length -eq (Get-Item -LiteralPath (Join-Path $Here 'TERMS.md')).Length) 'setup.exe route: the copy differs'
        $docDir = Split-Path -Parent $copy
        Remove-DocumentCopies
        Check (-not (Test-Path -LiteralPath $docDir)) 'setup.exe route: the copies were not deleted on exit'
        # "run install.bat again" says "run Setup again" on this route (own wording), and an engine
        # text that names install.bat gets one explaining line
        Check ((Get-RunAgainName) -eq 'Setup') 'setup.exe route: the run-again name is not Setup'
        Check ((Add-SetupExeHint 'Run install.bat again to retry.') -eq "Run install.bat again to retry.`n(If you used the Setup file: run Setup again.)") 'setup.exe route: the engine hint changed'
        Check ((Add-SetupExeHint 'Nothing to do with it.') -eq 'Nothing to do with it.') 'setup.exe route: a text without install.bat got the hint'
        Reset-Ui
        Set-Facts (Get-CannedFacts @{ Gpu = $true })
        $script:Warnings = @('The NVIDIA runtime could not be installed. Run install.bat again to retry.')
        $script:ErrorText = 'Installing the libraries failed. Run install.bat again; it carries on.'
        $script:StopRequested = $true; Show-Result 'stopped' 1
        Check ($ui.ResultLead.Text -match 'running Setup again' -and $ui.ResultLead.Text -notmatch 'install\.bat') "setup.exe route: stopped page says '$($ui.ResultLead.Text)'"
        $script:StopRequested = $false; Show-Result 'failure' 1
        Check ($ui.ResultLead.Text -match 'running Setup again' -and $ui.ResultLead.Text -notmatch 'install\.bat') "setup.exe route: failure page says '$($ui.ResultLead.Text)'"
        Check ($ui.ErrorBox.Text -match 'If you used the Setup file: run Setup again\.') 'setup.exe route: the error text lacks the hint'
        $script:DoneInfo = [pscustomobject]@{ desktopLnk = $null }
        Show-Result 'success' 0
        $warnText = [string]$ui.WarnList.Children[0].Child.Children[1].Text
        Check ($warnText -match 'run Setup again') "setup.exe route: the warning lacks the hint ($warnText)"
        $script:Warnings = @(); $script:ErrorText = $null; $script:DoneInfo = $null
        $script:FromSetupExe = [switch]$false
        Check ((Get-RunAgainName) -eq 'install.bat' -and (Add-SetupExeHint 'Run install.bat again') -eq 'Run install.bat again') 'zip route: the wording changed'
        Show-Result 'stopped' 1
        Check ($ui.ResultLead.Text -match 'running install\.bat again') 'zip route: the stopped page lost install.bat'
        $script:FromSetupExe = [switch]$true
        $script:FromSetupExe = [switch]$false
        Check ((Get-DocumentPath 'TERMS.md') -eq (Join-Path $Here 'TERMS.md')) 'zip route: the document was copied'
    } finally { $script:FromSetupExe = $wasExe }

    # the test switch guard
    $script:Harness = 'autorun'
    $saved = $script:OverridesComplete; $script:OverridesComplete = $false
    $guard = $false
    try { Set-TermsBoxForTest $true } catch { $guard = $true }
    Check $guard 'the terms box could be ticked by the program outside a test'
    $script:OverridesComplete = $saved; $script:Harness = 'selftest'

    if ($fails.Count) {
        [Console]::Out.WriteLine('selftest FAILED:')
        foreach ($f in $fails) { [Console]::Out.WriteLine("  - $f") }
        return 1
    }
    [Console]::Out.WriteLine('selftest ok')
    return 0
}

# -- the automatic test run (-AutoRun) -------------------------------------------------------------------
# Drives the window without a person. Only with all five test overrides, and
# the engine must say testMode in its facts; otherwise it refuses.

function Invoke-AutoStep {
    $a = $script:Auto
    if (((Get-Date) - $a.Started).TotalSeconds -gt 150) { Close-Window 1; return }
    if ($script:Checking -or ($script:Run -and $script:Run.Kind -eq 'check')) { return }
    $btn = $ui.NextBtn
    switch ($script:Page) {
        'welcome' { if ($script:Facts -and $btn.IsEnabled) { Press-Auto $btn } }
        'terms' {
            if ($AutoRun -eq 'cancel-terms') { Press-Auto $ui.CancelBtn; return }
            if ($ui.AgreeBox.IsChecked -ne $true) { Set-TermsBoxForTest $true; return }
            Press-Auto $btn
        }
        'choose' {
            $a.Button = [string]$btn.Content
            if ($script:Facts.python.ok -ne $true) {
                if ($AutoRun -eq 'install-python' -and -not $a.PyPressed) { $a.PyPressed = $true; Press-Auto $ui.PyInstallBtn; return }
                if ($AutoRun -eq 'install-python' -and $script:Py) { return }
                if (-not $a.Rechecked) { $a.Rechecked = $true; Press-Auto $ui.CheckAgainBtn }
                else { Press-Auto $ui.CancelBtn }
                return
            }
            if ($AutoRun -eq 'install-none' -and -not $a.Chosen) {
                $ui.NvidiaBox.IsChecked = $false; $ui.ModelBox.IsChecked = $false; $ui.DesktopBox.IsChecked = $false
                Update-Plan
                $a.Chosen = $true
                return
            }
            if ($btn.IsEnabled) { Press-Auto $btn }
        }
        'install' {
            if ($AutoRun -eq 'stop-install' -and -not $a.Stopped -and $script:CurrentStep -ge 5) {
                $a.Stopped = $true
                Invoke-Cancel
                if ($ui.Overlay.Visibility -eq 'Visible') { Press-Auto $ui.StopBtn }
            }
        }
        'result' { Press-Auto $btn }
        'error' { Press-Auto $btn }
    }
}

function Press-Auto($Button) {
    if (-not $Button.IsEnabled -or $Button.Visibility -ne 'Visible') { return }
    $Button.RaiseEvent((New-Object Windows.RoutedEventArgs([Windows.Controls.Primitives.ButtonBase]::ClickEvent)))
}

# -- main -----------------------------------------------------------------------------------------------------

$script:AutoTimerNeeded = $false
$script:Hwnd = [IntPtr]::Zero
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
    if ($ReasonFile -and (Split-Path -Leaf $ReasonFile) -like 'htt-setup-*.txt') { try { if (Test-Path -LiteralPath $ReasonFile) { Remove-Item -LiteralPath $ReasonFile -Force } } catch { } }
    $win = $script:Window

    $ui = @{}
    $names = @('Root', 'Main', 'Pages', 'Overlay', 'RailIcon', 'RailSub', 'RailVersion', 'TestBadge', 'WelcomeIcon', 'WelcomeVersion', 'UpdateCard', 'UpdateText', 'AccountCard', 'AccountText', 'ChooseTitle', 'ChooseSub', 'ChooseFoot', 'InstallTitle', 'FailHead', 'StopTitle', 'StopBody', 'CheckingRow',
        'PageWelcome', 'PageTerms', 'PageChoose', 'PageInstall', 'InstallScroll', 'PageResult', 'PageError',
        'TermsLead', 'TermsView', 'AgreeBox', 'AgreeText', 'LinkNotepad', 'LinkLicense', 'LinkNotices', 'LinkSecurity', 'LinkStatus',
        'RecheckRow', 'PythonPanel', 'PythonOrgBtn', 'CheckAgainBtn', 'PythonNote', 'PythonNotes', 'PlanPanel',
        'PyInstallBtn', 'PyLead', 'PyWhat', 'PyLicence', 'PyProgressPanel', 'PyStatus', 'PyBar', 'PyCancelBtn', 'PyStopBtn', 'PyError', 'PyManual',
        'ProgramWhat', 'ProgramOpen', 'ProgramPath', 'ProgramSize', 'EnvWhat', 'EnvOpen', 'EnvPath', 'EnvSize',
        'NvidiaRow', 'NvidiaBox', 'NvidiaTitle', 'NvidiaWhat', 'NvidiaOff', 'NvidiaSize', 'ModelBox', 'ModelTitle', 'ModelWhat', 'ModelOff', 'ModelSize',
        'DataOpen', 'DataPath', 'DesktopBox', 'TotalDownload', 'TotalDisk', 'FreeLabel', 'TotalFree', 'DiskWarn', 'DiskWarnText', 'ChooseNote',
        'StepCount', 'StepNow', 'Bar', 'StepList', 'DetailsExp', 'LogBox',
        'ResultBadge', 'ResultGlyph', 'ResultTitle', 'ResultLead', 'ResultPath', 'SuccessPanel', 'WarnList', 'DesktopHow', 'StartNow', 'FailPanel', 'ErrorBox',
        'ErrTitle', 'ErrText', 'ErrDetailsPanel', 'ErrDetails', 'TextInstallerBtn',
        'CancelBtn', 'CopyBtn', 'BackBtn', 'NextBtn', 'StopBtn', 'KeepBtn')
    for ($i = 1; $i -le 5; $i++) { $names += @("RailDot$i", "RailNum$i", "RailText$i") }
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
            $ui.WelcomeIcon.Source = $big
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
    $ui.BackBtn.Add_Click({ try { Invoke-Back } catch { Add-Log ('(window) ' + $_.Exception.Message) } })
    $ui.CancelBtn.Add_Click({ try { Invoke-Cancel } catch { Add-Log ('(window) ' + $_.Exception.Message) } })
    $ui.CopyBtn.Add_Click({ try { Copy-Details } catch { } })
    $ui.StopBtn.Add_Click({ try { Confirm-Stop } catch { Add-Log ('(window) ' + $_.Exception.Message) } })
    $ui.KeepBtn.Add_Click({ try { Hide-Confirm } catch { } })
    $ui.AgreeBox.Add_Checked({ try { Update-Nav } catch { } })
    $ui.AgreeBox.Add_Unchecked({ try { Update-Nav } catch { } })
    foreach ($b in @($ui.NvidiaBox, $ui.ModelBox, $ui.DesktopBox)) {
        $b.Add_Checked({ try { Update-Plan } catch { } })
        $b.Add_Unchecked({ try { Update-Plan } catch { } })
    }
    $ui.LinkNotepad.Add_Click({ try { Open-Document 'TERMS.md' } catch { } })
    $ui.LinkLicense.Add_Click({ try { Open-Document 'LICENSE' } catch { } })
    $ui.LinkNotices.Add_Click({ try { Open-Document 'THIRD-PARTY-NOTICES.md' } catch { } })
    $ui.LinkSecurity.Add_Click({ try { Open-Document 'SECURITY.md' } catch { } })
    $ui.ProgramOpen.Add_Click({ try { Open-Folder $ui.ProgramPath.Text } catch { } })
    $ui.EnvOpen.Add_Click({ try { Open-Folder $ui.EnvPath.Text } catch { } })
    $ui.DataOpen.Add_Click({ try { Open-Folder $ui.DataPath.Text } catch { } })
    $ui.CheckAgainBtn.Add_Click({ try { $ui.PythonNote.Visibility = 'Collapsed'; Start-Check $true } catch { } })
    $ui.PythonOrgBtn.Add_Click({
        try {
            if (Test-TestMode) { $ui.PythonNote.Text = 'Test mode: the browser is not opened.'; $ui.PythonNote.Visibility = 'Visible' }
            else { Start-Process 'https://www.python.org/downloads/windows/' }
        } catch { }
    })
    $ui.PyInstallBtn.Add_Click({ try { Start-PythonInstall } catch { Add-Log ('(window) ' + $_.Exception.Message) } })
    $ui.PyCancelBtn.Add_Click({ try { [void](Stop-PythonDownload) } catch { } })
    $ui.PyStopBtn.Add_Click({ try { Show-PythonStopConfirm } catch { } })
    $ui.TextInstallerBtn.Add_Click({ try { Open-TextInstaller } catch { } })
    $ui.DetailsExp.Add_Expanded({ try { Flush-Log; $ui.LogBox.ScrollToEnd(); $ui.InstallScroll.UpdateLayout(); $ui.InstallScroll.ScrollToBottom() } catch { } })
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
            $script:Auto = @{ Started = Get-Date; PyPressed = $false; Rechecked = $false; Chosen = $false; Stopped = $false }
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
                if (-not $script:Facts -and -not $script:Checking) { Start-Check $false }
            } catch { }
        })
        Show-Page 'welcome'
        Set-Shown $ui.CheckingRow $true
        [void]$win.ShowDialog()
        $exitCode = $script:ExitCode
        if ($null -eq $exitCode) { $exitCode = Get-CancelCode }
        if ($script:Harness -eq 'autorun') {
            [Console]::Out.WriteLine("mode: $($script:Mode) | window: $($script:Window.Title) | rail: $($script:RailLabels[3]) / $($ui.RailSub.Text) | button: $($script:Auto.Button) | page title: $($ui.InstallTitle.Text) | result: $($ui.ResultTitle.Text) | lead: $($ui.ResultLead.Text)")
            [Console]::Out.WriteLine("autorun: page=$($script:Page) result=$($script:ResultKind) exit=$exitCode steps=$($script:StepRows.Count) pyinstall=$($script:PyInstallCalls.Count)")
            if ($script:Page -eq 'error') { [Console]::Out.WriteLine('error page: ' + $ui.ErrTitle.Text + ' / ' + $ui.ErrText.Text) }
            $sc = Get-StartCommand
            if ($sc) { [Console]::Out.WriteLine('start-command: ' + $sc.Exe + ' | ' + $sc.Args + ' | ' + $sc.WorkDir) }
            if ($script:FullLog.Count) { [Console]::Out.WriteLine((Get-LogText)) }
        }
    }
} catch {
    $why = $_.Exception.Message
    if (-not $script:Ready) {
        Write-StartupFailure $why
        exit 10
    }
    [Console]::Error.WriteLine("The setup window stopped: $why")
    try { Remove-DocumentCopies } catch { }
    if ($ReadySignal -and -not $script:Harness) { Start-TextInstallerFallback "The setup window stopped: $why" }
    exit 1
}
try { Remove-DocumentCopies } catch { }
exit $exitCode
