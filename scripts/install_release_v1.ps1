param(
    [Parameter(Mandatory = $true)][string]$BundleDirectory,
    [string]$InstallRoot = "E:\ms_mcp\deployments",
    [string]$BootstrapPython = "E:\ms_mcp\ms_mcp_runtime\materials_studio_2023\.venv\Scripts\python.exe",
    [switch]$Activate
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$bundleRoot = (Resolve-Path -LiteralPath $BundleDirectory).Path
$bundlePath = Join-Path $bundleRoot "release-bundle.json"
$bundle = Get-Content -LiteralPath $bundlePath -Raw | ConvertFrom-Json
if ($bundle.schema_version -ne 1 -or $bundle.version -notmatch '^\d+\.\d+\.\d+$') {
    throw "Unsupported release bundle"
}
foreach ($entry in $bundle.files) {
    $path = Join-Path $bundleRoot ([string]$entry.path).Replace('/', '\')
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) { throw "Bundle file is missing: $path" }
    $actual = (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash
    if ($actual -ne $entry.sha256) { throw "Bundle file hash mismatch: $path" }
}
$requiredBundleDirectories = @("config", "docs", "moc", "scripts", "src", "tests", "wheelhouse")
foreach ($relative in $requiredBundleDirectories) {
    $path = Join-Path $bundleRoot $relative
    if (-not (Test-Path -LiteralPath $path -PathType Container)) {
        throw "Bundle directory is missing: $path"
    }
}
$requiredBundleFiles = @(
    "pyproject.toml",
    "install.ps1",
    "README.md",
    "requirements.lock",
    "release-manifest.json",
    "release-bundle.json",
    "mcp-config.example.json",
    "config\policy.json",
    "config\materialsscript-capabilities.json",
    "config\project-manifest.template.json",
    "config\project-manifest.schema.v2.json",
    "config\qualification-profiles.json",
    "config\research-environment.local.json",
    "config\research-workflow-requirements.json",
    "config\scientific-gate-intake.schema.v1.json",
    "config\science-contract.schema.json",
    "config\software.local.json",
    "docs\validation\receipts\p3c-real-castep-qualification-success.json",
    "moc\ms_moc.py",
    "moc\ms_mcp_bridge.py",
    "moc\MS_MOC_INTERFACE.md",
    "moc\MS_MOC_STATUS.md",
    "moc\SCIENCE_ENVIRONMENT.md",
    "moc\science-requirements.lock"
)
foreach ($relative in $requiredBundleFiles) {
    $path = Join-Path $bundleRoot $relative
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        throw "Required bundle file is missing: $path"
    }
}
$wheelhouse = Join-Path $bundleRoot "wheelhouse"
$projectWheel = @(Get-ChildItem -LiteralPath $wheelhouse -Filter "materials_studio_mcp-$($bundle.version)-*.whl")
if ($projectWheel.Count -ne 1) { throw "Expected exactly one project wheel" }
if (-not (Test-Path -LiteralPath $BootstrapPython -PathType Leaf)) {
    throw "Bootstrap Python is missing: $BootstrapPython"
}

$rootFull = [System.IO.Path]::GetFullPath($InstallRoot).TrimEnd('\')
$target = Join-Path $rootFull ([string]$bundle.version)
$targetFull = [System.IO.Path]::GetFullPath($target)
if (-not $targetFull.StartsWith($rootFull + '\', [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Install target escaped the install root"
}
if (Test-Path -LiteralPath $targetFull) { throw "Immutable version is already installed: $targetFull" }
$staging = Join-Path $rootFull ".$($bundle.version).installing-$PID"
$stagingFull = [System.IO.Path]::GetFullPath($staging)
if (-not $stagingFull.StartsWith($rootFull + '\', [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Install staging target escaped the install root"
}
if (Test-Path -LiteralPath $stagingFull) { throw "Install staging directory already exists: $stagingFull" }
New-Item -ItemType Directory -Path $stagingFull -Force | Out-Null
$installFull = $stagingFull

$venv = Join-Path $installFull ".venv"
& $BootstrapPython -m venv $venv
if ($LASTEXITCODE -ne 0) { throw "Virtual environment creation failed" }
$python = Join-Path $venv "Scripts\python.exe"
& $python -m pip install --no-index --find-links $wheelhouse --requirement (Join-Path $bundleRoot "requirements.lock")
if ($LASTEXITCODE -ne 0) { throw "Locked offline dependency installation failed" }
& $python -m pip install --no-index --no-deps $projectWheel[0].FullName
if ($LASTEXITCODE -ne 0) { throw "Project wheel installation failed" }
& $python -m pip check
if ($LASTEXITCODE -ne 0) { throw "Installed dependency check failed" }
Copy-Item -LiteralPath (Join-Path $bundleRoot "release-manifest.json") -Destination $installFull
Copy-Item -LiteralPath $bundlePath -Destination $installFull
Copy-Item -LiteralPath (Join-Path $bundleRoot "requirements.lock") -Destination $installFull
Copy-Item -LiteralPath (Join-Path $bundleRoot "mcp-config.example.json") -Destination $installFull
Copy-Item -LiteralPath (Join-Path $bundleRoot "pyproject.toml") -Destination $installFull
Copy-Item -LiteralPath (Join-Path $bundleRoot "install.ps1") -Destination $installFull
Copy-Item -LiteralPath (Join-Path $bundleRoot "README.md") -Destination $installFull
Copy-Item -LiteralPath (Join-Path $bundleRoot "config") -Destination $installFull -Recurse
Copy-Item -LiteralPath (Join-Path $bundleRoot "docs") -Destination $installFull -Recurse
Copy-Item -LiteralPath (Join-Path $bundleRoot "moc") -Destination $installFull -Recurse
Copy-Item -LiteralPath (Join-Path $bundleRoot "scripts") -Destination $installFull -Recurse
Copy-Item -LiteralPath (Join-Path $bundleRoot "src") -Destination $installFull -Recurse
Copy-Item -LiteralPath (Join-Path $bundleRoot "tests") -Destination $installFull -Recurse
$installedWheelhouse = New-Item -ItemType Directory -Path (Join-Path $installFull "wheelhouse")
Copy-Item -LiteralPath $projectWheel[0].FullName -Destination $installedWheelhouse.FullName

$previousMcpRoot = $env:MATERIALS_STUDIO_MCP_ROOT
$previousManifestRoot = $env:MS_MOC_MCP_ROOT
try {
    $env:MATERIALS_STUDIO_MCP_ROOT = $installFull
    $env:MS_MOC_MCP_ROOT = $installFull
    & $python -c "import materials_studio_mcp; assert materials_studio_mcp.__version__ == '$($bundle.version)'"
    if ($LASTEXITCODE -ne 0) { throw "Installed package version check failed" }

    & $python -m materials_studio_mcp.release verify-deployment --root $installFull
    if ($LASTEXITCODE -ne 0) { throw "Independent deployment verification failed" }
    & $python -c "from materials_studio_mcp.capability_registry import audit_capability_registry; assert audit_capability_registry()['status'] == 'pass'"
    if ($LASTEXITCODE -ne 0) { throw "Installed capability registry audit failed" }
}
finally {
    if ($null -eq $previousMcpRoot) { Remove-Item Env:MATERIALS_STUDIO_MCP_ROOT -ErrorAction SilentlyContinue } else { $env:MATERIALS_STUDIO_MCP_ROOT = $previousMcpRoot }
    if ($null -eq $previousManifestRoot) { Remove-Item Env:MS_MOC_MCP_ROOT -ErrorAction SilentlyContinue } else { $env:MS_MOC_MCP_ROOT = $previousManifestRoot }
}

Move-Item -LiteralPath $installFull -Destination $targetFull

$current = Join-Path $rootFull "current"
$previousTarget = $null
$activated = $false
if ($Activate) {
    if (Test-Path -LiteralPath $current) {
        $item = Get-Item -LiteralPath $current -Force
        if ($item.LinkType -ne "Junction") { throw "Current deployment pointer is not a junction: $current" }
        $previousTarget = [string]$item.Target
        [System.IO.Directory]::Delete($current)
    }
    New-Item -ItemType Junction -Path $current -Target $targetFull | Out-Null
    $activated = $true
}
$receipt = @{
    schema_version = 1
    version = [string]$bundle.version
    installed_at_utc = [DateTime]::UtcNow.ToString("o")
    target = $targetFull
    current_pointer = $current
    previous_target = $previousTarget
    activated = $activated
    production_science_released = $false
}
[System.IO.File]::WriteAllText(
    (Join-Path $targetFull "install-receipt.json"),
    ($receipt | ConvertTo-Json -Depth 5),
    [System.Text.UTF8Encoding]::new($false)
)
Write-Output $targetFull
