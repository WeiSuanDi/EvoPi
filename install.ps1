[CmdletBinding()]
param(
    [string]$InstallRoot = (Join-Path $HOME ".evopi"),
    [string]$LocalReleaseDirectory,
    [string]$LocalVersion,
    [switch]$SkipPathUpdate
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

function Get-EvoPiPython {
    $candidates = @(
        @{ Command = "py"; Arguments = @("-3.12") },
        @{ Command = "py"; Arguments = @("-3.11") },
        @{ Command = "python"; Arguments = @() }
    )
    foreach ($candidate in $candidates) {
        try {
            $version = & $candidate.Command @($candidate.Arguments) -c `
                "import sys; print('.'.join(map(str, sys.version_info[:3])))" 2>$null
            if ($LASTEXITCODE -eq 0) {
                $parts = $version.Trim().Split(".")
                if ([int]$parts[0] -eq 3 -and [int]$parts[1] -ge 11) {
                    return $candidate
                }
            }
        } catch { }
    }
    throw @"
Python 3.11 or newer is required.
Install Python from https://www.python.org/downloads/windows/
or run: winget install --id Python.Python.3.12 -e
Then open a new terminal and run this installer again.
"@
}

function Test-EvoPiHttpsGitHubUrl([string]$Url) {
    $uri = [Uri]$Url
    if ($uri.Scheme -ne "https" -or $uri.Host -notin @("github.com", "objects.githubusercontent.com")) {
        throw "Release assets must use an HTTPS GitHub host: $Url"
    }
    if ($uri.UserInfo -or $uri.Query -or $uri.Fragment) {
        throw "Release asset URL contains forbidden components: $Url"
    }
}

function Get-EvoPiRelease([string]$TemporaryDirectory) {
    if ($LocalReleaseDirectory) {
        if ($env:EVOPI_INSTALL_TESTING -ne "1") {
            throw "Local Release assets are available only to the installer test harness"
        }
        if ($LocalVersion -notmatch '^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$') {
            throw "-LocalVersion must be stable MAJOR.MINOR.PATCH SemVer"
        }
        $wheelName = "evopi-$LocalVersion-py3-none-any.whl"
        return @{
            Version = $LocalVersion
            ReleaseUrl = "local-release-test"
            WheelName = $wheelName
            WheelPath = Join-Path $LocalReleaseDirectory $wheelName
            ChecksumPath = Join-Path $LocalReleaseDirectory "SHA256SUMS"
        }
    }

    $api = "https://api.github.com/repos/WeiSuanDi/EvoPi/releases/latest"
    $headers = @{ Accept = "application/vnd.github+json"; "User-Agent" = "EvoPi-Installer" }
    $release = Invoke-RestMethod -Uri $api -Headers $headers
    if ($release.draft -or $release.prerelease -or $release.tag_name -notmatch '^v(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$') {
        throw "The latest GitHub Release is not a stable EvoPi version"
    }
    $version = $release.tag_name.Substring(1)
    $wheelName = "evopi-$version-py3-none-any.whl"
    $wheelAsset = @($release.assets | Where-Object { $_.name -eq $wheelName })
    $checksumAsset = @($release.assets | Where-Object { $_.name -eq "SHA256SUMS" })
    if ($wheelAsset.Count -ne 1 -or $checksumAsset.Count -ne 1) {
        throw "Release must contain exactly one $wheelName and SHA256SUMS"
    }
    Test-EvoPiHttpsGitHubUrl $wheelAsset[0].browser_download_url
    Test-EvoPiHttpsGitHubUrl $checksumAsset[0].browser_download_url
    $wheelPath = Join-Path $TemporaryDirectory $wheelName
    $checksumPath = Join-Path $TemporaryDirectory "SHA256SUMS"
    Invoke-WebRequest -Uri $wheelAsset[0].browser_download_url -OutFile $wheelPath
    Invoke-WebRequest -Uri $checksumAsset[0].browser_download_url -OutFile $checksumPath
    return @{
        Version = $version
        ReleaseUrl = $release.html_url
        WheelName = $wheelName
        WheelPath = $wheelPath
        ChecksumPath = $checksumPath
    }
}

function Test-EvoPiWheel($Release, $Python) {
    if (-not (Test-Path -LiteralPath $Release.WheelPath -PathType Leaf) -or
        -not (Test-Path -LiteralPath $Release.ChecksumPath -PathType Leaf)) {
        throw "Release wheel or SHA256SUMS is missing"
    }
    $matching = @(Get-Content -LiteralPath $Release.ChecksumPath | Where-Object {
        $_ -match "^([0-9a-fA-F]{64})\s+\*?$([regex]::Escape($Release.WheelName))$"
    })
    if ($matching.Count -ne 1) {
        throw "SHA256SUMS must contain exactly one wheel digest"
    }
    $expected = $matching[0].Split()[0].ToLowerInvariant()
    $actual = (Get-FileHash -LiteralPath $Release.WheelPath -Algorithm SHA256).Hash.ToLowerInvariant()
    if ($actual -ne $expected) { throw "EvoPi wheel SHA-256 verification failed" }
    $metadataVersion = & $Python.Command @($Python.Arguments) -c `
        "import email.parser,sys,zipfile; z=zipfile.ZipFile(sys.argv[1]); n=[x for x in z.namelist() if x.endswith('.dist-info/METADATA')]; assert len(n)==1; m=email.parser.BytesParser().parsebytes(z.read(n[0])); assert m['Name'].lower()=='evopi'; print(m['Version'])" `
        $Release.WheelPath
    if ($LASTEXITCODE -ne 0 -or $metadataVersion.Trim() -ne $Release.Version) {
        throw "Wheel metadata version does not match the Release version"
    }
    return $actual
}

function Set-EvoPiUserPath([string]$BinDirectory) {
    $current = [Environment]::GetEnvironmentVariable("Path", "User")
    $parts = @($current -split ";" | Where-Object { $_ })
    if ($parts -notcontains $BinDirectory) {
        $updated = (@($parts) + $BinDirectory) -join ";"
        [Environment]::SetEnvironmentVariable("Path", $updated, "User")
        $env:Path = "$BinDirectory;$env:Path"
        Write-Host "Added $BinDirectory to the current user's PATH. Open a new terminal if needed."
    }
}

$python = Get-EvoPiPython
$temporary = Join-Path ([IO.Path]::GetTempPath()) ("evopi-install-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $temporary | Out-Null
$runtimeRoot = Join-Path $InstallRoot "runtime"
New-Item -ItemType Directory -Force -Path $runtimeRoot | Out-Null
$lockPath = Join-Path $runtimeRoot "update.lock"
try {
    $updateLock = [IO.File]::Open($lockPath, [IO.FileMode]::OpenOrCreate, [IO.FileAccess]::ReadWrite, [IO.FileShare]::None)
} catch {
    Remove-Item -LiteralPath $temporary -Recurse -Force
    throw "Another EvoPi install or update is already running"
}
try {
    $release = Get-EvoPiRelease $temporary
    $digest = Test-EvoPiWheel $release $python
    $versionsRoot = Join-Path $runtimeRoot "versions"
    $currentPath = Join-Path $runtimeRoot "current.txt"
    $target = Join-Path $versionsRoot $release.Version
    if ((Test-Path -LiteralPath $currentPath) -and
        ((Get-Content -LiteralPath $currentPath -Raw).Trim() -eq $release.Version) -and
        (Test-Path -LiteralPath (Join-Path $target ".evopi-runtime.json"))) {
        Write-Host "EvoPi $($release.Version) is already installed."
    } else {
        New-Item -ItemType Directory -Force -Path $versionsRoot | Out-Null
        if ((Test-Path -LiteralPath $target) -and
            -not (Test-Path -LiteralPath (Join-Path $target ".evopi-runtime.json"))) {
            throw "The target runtime exists without a verification marker"
        }
        if (-not (Test-Path -LiteralPath $target)) {
            try {
                & $python.Command @($python.Arguments) -m venv $target
                if ($LASTEXITCODE -ne 0) { throw "Unable to create the EvoPi runtime" }
                $runtimePython = Join-Path $target "Scripts\python.exe"
                $runtimeExe = Join-Path $target "Scripts\evopi.exe"
                & $runtimePython -m pip install --disable-pip-version-check $release.WheelPath
                if ($LASTEXITCODE -ne 0) { throw "Unable to install the EvoPi wheel" }
                & $runtimePython -c "import evopi"
                if ($LASTEXITCODE -ne 0) { throw "EvoPi import smoke test failed" }
                & $runtimeExe --version
                if ($LASTEXITCODE -ne 0) { throw "EvoPi version smoke test failed" }
                & $runtimeExe --help *> $null
                if ($LASTEXITCODE -ne 0) { throw "EvoPi help smoke test failed" }
                @{ schema_version = 1; version = $release.Version; sha256 = $digest } |
                    ConvertTo-Json -Compress |
                    Set-Content -LiteralPath (Join-Path $target ".evopi-runtime.json") -Encoding UTF8
            } catch {
                if (Test-Path -LiteralPath $target) { Remove-Item -LiteralPath $target -Recurse -Force }
                throw
            }
        }
        $pointerTemp = Join-Path $runtimeRoot (".current." + [guid]::NewGuid().ToString("N"))
        $release.Version | Set-Content -LiteralPath $pointerTemp -Encoding ASCII
        Move-Item -LiteralPath $pointerTemp -Destination $currentPath -Force
        Write-Host "Installed EvoPi $($release.Version) from $($release.ReleaseUrl)"
        $versions = @(Get-ChildItem -LiteralPath $versionsRoot -Directory | Where-Object {
            -not $_.Name.StartsWith(".")
        } | Sort-Object Name -Descending)
        foreach ($old in $versions | Select-Object -Skip 2) {
            try { Remove-Item -LiteralPath $old.FullName -Recurse -Force }
            catch { Write-Warning "Unable to remove old runtime $($old.Name): $_" }
        }
    }

    $bin = Join-Path $InstallRoot "bin"
    New-Item -ItemType Directory -Force -Path $bin | Out-Null
    $launcher = @"
@echo off
setlocal
set "EVOPI_HOME=$InstallRoot"
set "EVOPI_MANAGED_ROOT=$InstallRoot"
for /f "usebackq delims=" %%V in ("$InstallRoot\runtime\current.txt") do set "EVOPI_VERSION=%%V"
if not defined EVOPI_VERSION exit /b 1
"$InstallRoot\runtime\versions\%EVOPI_VERSION%\Scripts\evopi.exe" %*
exit /b %errorlevel%
"@
    Set-Content -LiteralPath (Join-Path $bin "evopi.cmd") -Value $launcher -Encoding ASCII
    if (-not $SkipPathUpdate) { Set-EvoPiUserPath $bin }
    Write-Host "Run 'evopi setup' to configure your model provider, then run 'evopi'."
} finally {
    if ($updateLock) { $updateLock.Dispose() }
    if (Test-Path -LiteralPath $temporary) { Remove-Item -LiteralPath $temporary -Recurse -Force }
}
