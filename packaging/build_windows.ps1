param(
    [string]$Python = "python",
    [string]$Iscc = "",
    [string]$Version = ""
)
$ErrorActionPreference = "Stop"
$Root = Split-Path $PSScriptRoot -Parent
Set-Location $Root
$VersionPattern = '(<small id="app-version">[^<>]*?v)\d+\.\d+\.\d+(</small>)'
if (-not $Version) {
    $SourceHtml = [IO.File]::ReadAllText((Join-Path $Root 'static\index.html'))
    if ($SourceHtml -notmatch '<small id="app-version">[^<>]*?v(\d+\.\d+\.\d+)</small>') { throw 'Missing UI version' }
    $Version = $Matches[1]
}
if ($Version -notmatch '^\d+\.\d+\.\d+$') { throw "Version must be X.Y.Z" }
function Checked([string]$Exe, [string[]]$Arguments) {
    & $Exe @Arguments
    if ($LASTEXITCODE -ne 0) { throw "$Exe failed ($LASTEXITCODE)" }
}
if (-not (Test-Path ".venv-windows-build\Scripts\python.exe")) {
    Checked $Python @("-m", "venv", ".venv-windows-build")
}
$BuildPython = Join-Path $Root ".venv-windows-build\Scripts\python.exe"
Checked $BuildPython @("-m", "pip", "install", "-r", "requirements-windows.txt")
Checked $BuildPython @("packaging/create_icon.py")
Checked $BuildPython @("packaging/collect_licenses.py")
Checked $BuildPython @("-m", "PyInstaller", "--noconfirm", "--clean", "packaging/NetwordMonitor.spec")
$BundledHtmlPath = Join-Path $Root 'dist\NetwordMonitor\_internal\static\index.html'
$BundledHtml = [IO.File]::ReadAllText($BundledHtmlPath)
if ([regex]::Matches($BundledHtml, $VersionPattern).Count -ne 1) { throw 'Expected one UI version marker' }
$BundledHtml = $BundledHtml -replace $VersionPattern, ('${1}' + $Version + '${2}')
[IO.File]::WriteAllText($BundledHtmlPath, $BundledHtml, [Text.UTF8Encoding]::new($false))
# Fail closed if the distributable ever gains private runtime files.
$Forbidden = Get-ChildItem "dist\NetwordMonitor" -Recurse -File | Where-Object {
    $_.Name -in @("hosts.json", "settings.json", "notifications.json", "configuration.json", "agent.env") -or
    $_.Name -like "*.env" -or $_.Name -like "hosts.pre-*"
}
if ($Forbidden) { throw "Private runtime files found in distributable" }
if (-not $Iscc) {
    $Candidates = @(
        "${env:ProgramFiles(x86)}\Inno Setup 6\ISCC.exe",
        "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe",
        "$env:ProgramFiles\Inno Setup 6\ISCC.exe"
    )
    $Command = Get-Command ISCC.exe -ErrorAction SilentlyContinue
    if ($Command) { $Iscc = $Command.Source }
    foreach ($Candidate in $Candidates) {
        if (-not $Iscc -and (Test-Path $Candidate)) { $Iscc = $Candidate }
    }
}
if (-not $Iscc -or -not (Test-Path $Iscc)) {
    throw "Portable build ready in dist\NetwordMonitor. Install Inno Setup 6 and pass -Iscc to create the installer."
}
Checked $Iscc @("/DAppVersion=$Version", "packaging/installer.iss")
Write-Output "Installer: dist\installer\NetwordMonitor-Setup-$Version-x64.exe"
