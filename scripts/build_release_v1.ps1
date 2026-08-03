param(
    [string]$ReleaseRoot = "E:\ms_mcp\releases"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $projectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw "MCP Python is missing: $python"
}

$version = (& $python -c "import materials_studio_mcp; print(materials_studio_mcp.__version__)").Trim()
if ($LASTEXITCODE -ne 0 -or $version -notmatch '^\d+\.\d+\.\d+$') {
    throw "Unable to read a stable semantic version"
}
$releaseName = "materials-studio-mcp-moc-$version"
$finalReleaseDir = Join-Path $ReleaseRoot $releaseName
if (Test-Path -LiteralPath $finalReleaseDir) {
    throw "Immutable release directory already exists: $finalReleaseDir"
}
$releaseDir = Join-Path $ReleaseRoot ".$releaseName.staging-$PID"
if (Test-Path -LiteralPath $releaseDir) { throw "Release staging directory already exists: $releaseDir" }

& $python -m unittest discover -s (Join-Path $projectRoot "tests") -q
if ($LASTEXITCODE -ne 0) { throw "Regression suite failed" }
& $python -m pip check
if ($LASTEXITCODE -ne 0) { throw "Dependency consistency check failed" }
$sourceManifestPath = Join-Path $projectRoot "release-manifest.json"
& $python -m materials_studio_mcp.release build --manifest $sourceManifestPath --force
if ($LASTEXITCODE -ne 0) { throw "Release manifest build failed" }

New-Item -ItemType Directory -Path $releaseDir | Out-Null
$wheelhouse = New-Item -ItemType Directory -Path (Join-Path $releaseDir "wheelhouse")
& $python -m pip wheel --no-build-isolation --no-deps --wheel-dir $wheelhouse.FullName $projectRoot
if ($LASTEXITCODE -ne 0) { throw "Project wheel build failed" }
& $python -m pip download --only-binary=:all: --requirement (Join-Path $projectRoot "requirements.lock") --dest $wheelhouse.FullName
if ($LASTEXITCODE -ne 0) { throw "Locked dependency download failed" }

$bundleFiles = @(
    "pyproject.toml",
    "install.ps1",
    "README.md",
    "requirements.lock",
    "release-manifest.json",
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
    "config\software.local.json"
)
foreach ($relative in $bundleFiles) {
    $source = Join-Path $projectRoot $relative
    $destination = Join-Path $releaseDir $relative
    New-Item -ItemType Directory -Path (Split-Path -Parent $destination) -Force | Out-Null
    Copy-Item -LiteralPath $source -Destination $destination
}

foreach ($directory in @("src", "scripts", "tests")) {
    $sourceDirectory = Join-Path $projectRoot $directory
    Get-ChildItem -LiteralPath $sourceDirectory -File -Recurse |
        Where-Object {
            $_.FullName -notmatch '(?i)(\\|/)(__pycache__|[^\\/]+\.egg-info)(\\|/)' -and
            $_.Extension -notin @('.pyc', '.pyo')
        } |
        ForEach-Object {
            $relative = $_.FullName.Substring($projectRoot.Length).TrimStart('\')
            $destination = Join-Path $releaseDir $relative
            New-Item -ItemType Directory -Path (Split-Path -Parent $destination) -Force | Out-Null
            Copy-Item -LiteralPath $_.FullName -Destination $destination
        }
}

$generatedPythonArtifacts = @(
    Get-ChildItem -LiteralPath $releaseDir -File -Recurse |
        Where-Object {
            $_.FullName -match '(?i)(\\|/)(__pycache__|[^\\/]+\.egg-info)(\\|/)' -or
            $_.Extension -in @('.pyc', '.pyo')
        }
)
if ($generatedPythonArtifacts.Count -ne 0) {
    throw "Generated Python artifacts must not be bundled: $($generatedPythonArtifacts.FullName -join ', ')"
}

$mocDir = New-Item -ItemType Directory -Path (Join-Path $releaseDir "moc")
$sourceManifestJson = [System.IO.File]::ReadAllText(
    $sourceManifestPath,
    [System.Text.UTF8Encoding]::new($false)
)
$sourceManifest = $sourceManifestJson | ConvertFrom-Json
$mocEntries = @($sourceManifest.files | Where-Object { $_.label -like 'workspace/*' })
if ($mocEntries.Count -ne 6) { throw "Expected six MOC and scientific-environment files in the source release manifest" }
foreach ($entry in $mocEntries) {
    Copy-Item -LiteralPath ([string]$entry.path) -Destination $mocDir.FullName
}

$hashes = @()
foreach ($file in Get-ChildItem -LiteralPath $releaseDir -File -Recurse | Sort-Object FullName) {
    $relative = $file.FullName.Substring($releaseDir.Length).TrimStart('\').Replace('\', '/')
    $hashes += @{
        path = $relative
        bytes = $file.Length
        sha256 = (Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash
    }
}
$bundle = @{
    schema_version = 1
    name = "materials-studio-mcp-moc"
    version = $version
    channel = "v1-release-candidate"
    created_at_utc = [DateTime]::UtcNow.ToString("o")
    source_manifest_sha256 = (Get-FileHash -LiteralPath $sourceManifestPath -Algorithm SHA256).Hash
    production_science_released = $false
    files = $hashes
}
[System.IO.File]::WriteAllText(
    (Join-Path $releaseDir "release-bundle.json"),
    ($bundle | ConvertTo-Json -Depth 8),
    [System.Text.UTF8Encoding]::new($false)
)
Move-Item -LiteralPath $releaseDir -Destination $finalReleaseDir
Write-Output $finalReleaseDir
