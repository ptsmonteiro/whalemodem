$ErrorActionPreference = 'Stop'

$Repository = if ($env:WHALE_REPOSITORY) { $env:WHALE_REPOSITORY } else { 'ptsmonteiro/whalemodem' }
$InstallRoot = if ($env:WHALE_INSTALL_ROOT) { $env:WHALE_INSTALL_ROOT } else { Join-Path $HOME '.local\share\whale' }
$BinDir = if ($env:WHALE_BIN_DIR) { $env:WHALE_BIN_DIR } else { Join-Path $HOME '.local\bin' }

function Get-WhalePlatformTag {
    param([string]$OS,
          [string]$Architecture)
    if (!$OS) { $OS = $env:OS }
    # PROCESSOR_ARCHITEW6432 reports the native architecture when this script
    # runs under 32-bit PowerShell on 64-bit Windows.
    if (!$Architecture) { $Architecture = $env:PROCESSOR_ARCHITEW6432 }
    if (!$Architecture) { $Architecture = $env:PROCESSOR_ARCHITECTURE }
    if ($OS -notmatch 'Windows' -or $Architecture -notmatch '^(AMD64|X64)$') {
        throw "Unsupported platform: $OS $Architecture (Whale supports Windows x86_64)"
    }
    'windows-x86_64'
}

function Get-WhaleReleaseUrl([string]$Version, [string]$Asset) {
    "https://github.com/$Repository/releases/download/$Version/$Asset"
}

function Add-WhalePath {
    $current = [Environment]::GetEnvironmentVariable('Path', 'User')
    $parts = @($current -split ';' | Where-Object { $_ })
    if ($parts -notcontains $BinDir) {
        [Environment]::SetEnvironmentVariable('Path', (($parts + $BinDir) -join ';'), 'User')
    }
    if (($env:Path -split ';') -notcontains $BinDir) { $env:Path = "$BinDir;$env:Path" }
}

function Install-Whale {
$platform = Get-WhalePlatformTag
$asset = "whale-$platform.zip"
$release = Invoke-RestMethod -Headers @{ Accept = 'application/vnd.github+json' } -Uri "https://api.github.com/repos/$Repository/releases/latest"
$version = [string]$release.tag_name
if (!$version -or $version -notmatch '^[A-Za-z0-9._+-]+$') { throw "Invalid latest release tag: $version" }

New-Item -ItemType Directory -Force -Path $InstallRoot, $BinDir | Out-Null
$temp = Join-Path ([IO.Path]::GetTempPath()) ("whale-install-" + [guid]::NewGuid())
New-Item -ItemType Directory -Path $temp | Out-Null
try {
    $archive = Join-Path $temp $asset
    $checksums = Join-Path $temp 'SHA256SUMS'
    Invoke-WebRequest -UseBasicParsing -Uri (Get-WhaleReleaseUrl $version $asset) -OutFile $archive
    Invoke-WebRequest -UseBasicParsing -Uri (Get-WhaleReleaseUrl $version 'SHA256SUMS') -OutFile $checksums
    $entry = Get-Content $checksums | Where-Object { $_ -match "^[0-9a-fA-F]{64}\s+\*?$([regex]::Escape($asset))$" } | Select-Object -First 1
    if (!$entry) { throw "No SHA-256 checksum published for $asset" }
    $expected = ($entry -split '\s+')[0].ToLowerInvariant()
    $actual = (Get-FileHash -Algorithm SHA256 $archive).Hash.ToLowerInvariant()
    if ($actual -ne $expected) { throw "SHA-256 verification failed for $asset" }

    $destination = Join-Path $InstallRoot $version
    if ((Test-Path $destination) -and !(Test-Path (Join-Path $destination 'whale\whale-server.exe'))) {
        throw "$destination exists but is not a valid Whale installation; refusing to overwrite it."
    }
    if (!(Test-Path (Join-Path $destination 'whale\whale-server.exe'))) {
        $staging = Join-Path $InstallRoot (".install-$version-" + [guid]::NewGuid())
        Expand-Archive -LiteralPath $archive -DestinationPath $staging
        if (!(Test-Path (Join-Path $staging 'whale\whale-server.exe')) -or !(Test-Path (Join-Path $staging 'whale\whale-configure.exe')) -or !(Test-Path (Join-Path $staging 'whale\whale-test.exe'))) {
            throw 'Release archive does not contain the expected Whale commands.'
        }
        Move-Item -LiteralPath $staging -Destination $destination
    }

    $current = Join-Path $InstallRoot 'current'
    if (Test-Path $current) {
        $currentItem = Get-Item -LiteralPath $current -Force
        if ($currentItem.LinkType -ne 'Junction') {
            throw "$current already exists and is not an installer-managed junction"
        }
        Remove-Item -LiteralPath $current -Force
    }
    New-Item -ItemType Junction -Path $current -Target $destination | Out-Null
    foreach ($command in 'whale-server', 'whale-configure', 'whale-test') {
        $launcher = Join-Path $BinDir "$command.cmd"
        $content = "@echo off`r`n`"$current\whale\$command.exe`" %*`r`n"
        [IO.File]::WriteAllText($launcher, $content, [Text.Encoding]::ASCII)
    }
    Add-WhalePath
    Write-Host "Whale $version installed. Existing config.toml files were left unchanged."
    Write-Host 'Open a new terminal before running whale-server (this window is also ready).'
}
finally {
    if (Test-Path $temp) { Remove-Item -LiteralPath $temp -Recurse -Force }
}
}

if ($env:WHALE_INSTALLER_TEST -ne '1') { Install-Whale }
