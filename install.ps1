Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$venvPath = Join-Path $projectRoot ".venv"
$pythonExe = Join-Path $venvPath "Scripts\python.exe"
$configPath = Join-Path $projectRoot "mcp-config.local.json"
$lockPath = Join-Path $projectRoot "requirements.lock"
$runtimeAlias = "E:\ms_mcp\ms_mcp_runtime\materials_studio_2023"
$runtimeAliasPython = Join-Path $runtimeAlias ".venv\Scripts\python.exe"
$codexPython = "C:\Users\86130\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe"
$pathPython = (Get-Command python -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Source -ErrorAction SilentlyContinue)
$bootstrapPython = if (Test-Path -LiteralPath $codexPython) {
    $codexPython
} elseif ($pathPython) {
    $pathPython
} else {
    throw "No usable bootstrap Python found. Install Python or restore the Codex runtime Python first."
}
$bootstrapSitePackages = (& $bootstrapPython -c "import sysconfig; print(sysconfig.get_paths()['purelib'])").Trim()
$venvSitePackages = Join-Path $venvPath "Lib\site-packages"

function Ensure-BootstrapPackage {
    param(
        [Parameter(Mandatory = $true)][string]$PackageDirName,
        [string]$DistInfoPattern = ""
    )

    $targetPackageDir = Join-Path $venvSitePackages $PackageDirName
    if (-not (Test-Path -LiteralPath $targetPackageDir)) {
        $sourcePackageDir = Join-Path $bootstrapSitePackages $PackageDirName
        if (-not (Test-Path -LiteralPath $sourcePackageDir)) {
            throw "Bootstrap package directory not found: $sourcePackageDir"
        }
        Copy-Item -LiteralPath $sourcePackageDir -Destination $targetPackageDir -Recurse -Force
    }

    if (-not [string]::IsNullOrWhiteSpace($DistInfoPattern)) {
        $hasDistInfo = @(Get-ChildItem -LiteralPath $venvSitePackages -Directory -Filter $DistInfoPattern -ErrorAction SilentlyContinue).Count -gt 0
        if (-not $hasDistInfo) {
            $sourceDistInfos = @(Get-ChildItem -LiteralPath $bootstrapSitePackages -Directory -Filter $DistInfoPattern -ErrorAction SilentlyContinue)
            if (-not $sourceDistInfos) {
                throw "Bootstrap dist-info not found for pattern: $DistInfoPattern"
            }
            foreach ($distInfo in $sourceDistInfos) {
                Copy-Item -LiteralPath $distInfo.FullName -Destination (Join-Path $venvSitePackages $distInfo.Name) -Recurse -Force
            }
        }
    }
}

if (-not (Test-Path -LiteralPath $venvPath)) {
    & $bootstrapPython -m venv $venvPath
} else {
    & $bootstrapPython -m venv --upgrade $venvPath
}

Ensure-BootstrapPackage -PackageDirName "setuptools" -DistInfoPattern "setuptools-*.dist-info"
Ensure-BootstrapPackage -PackageDirName "_distutils_hack" -DistInfoPattern ""
Ensure-BootstrapPackage -PackageDirName "wheel" -DistInfoPattern "wheel-*.dist-info"
Ensure-BootstrapPackage -PackageDirName "packaging" -DistInfoPattern "packaging-*.dist-info"

& $pythonExe -m pip --disable-pip-version-check --version | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw "pip is not usable in $venvPath"
}

if (-not (Test-Path -LiteralPath $lockPath)) {
    throw "Locked dependency file not found: $lockPath"
}

& $pythonExe -m pip --disable-pip-version-check install --requirement $lockPath
if ($LASTEXITCODE -ne 0) {
    throw "Failed to install the locked runtime dependency set from $lockPath"
}

& $pythonExe -m pip --disable-pip-version-check install --no-build-isolation --no-deps -e $projectRoot
if ($LASTEXITCODE -ne 0) {
    throw "Failed to install materials-studio-mcp in editable mode from $projectRoot"
}

& $pythonExe -m pip --disable-pip-version-check check
if ($LASTEXITCODE -ne 0) {
    throw "Installed environment failed dependency consistency check"
}

$runtimeAliasParent = Split-Path -Parent $runtimeAlias
if (-not (Test-Path -LiteralPath $runtimeAliasParent)) {
    New-Item -ItemType Directory -Path $runtimeAliasParent | Out-Null
}

if (-not (Test-Path -LiteralPath $runtimeAlias)) {
    New-Item -ItemType Junction -Path $runtimeAlias -Target $projectRoot | Out-Null
}

$msRoot = "D:\Program Files (x86)\BIOVIA\Materials Studio 23.1"
$config = @{
    mcpServers = @{
        "materials-studio-2023" = @{
            command = $runtimeAliasPython
            args = @("-m", "materials_studio_mcp.server")
            env = @{
                MATERIALS_STUDIO_ROOT = $msRoot
            }
        }
    }
} | ConvertTo-Json -Depth 8

[System.IO.File]::WriteAllText($configPath, $config, [System.Text.UTF8Encoding]::new($false))

Write-Host "Installed virtual environment:" $venvPath
Write-Host "Prepared runtime alias:" $runtimeAlias
Write-Host "Wrote client config:" $configPath
