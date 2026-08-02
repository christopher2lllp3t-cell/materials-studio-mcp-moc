param(
    [string]$DeploymentRoot = "E:\ms_mcp\deployments\1.3.0",
    [string]$ReceiptPath = "",
    [int]$ExpectedTestCount = 262
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$sourceRoot = (Resolve-Path -LiteralPath (Split-Path -Parent $PSScriptRoot)).Path
if ([string]::IsNullOrWhiteSpace($ReceiptPath)) {
    $ReceiptPath = Join-Path $sourceRoot "docs\validation\receipts\p1-castep-result-parser-verification.json"
}
$sourcePython = Join-Path $sourceRoot ".venv\Scripts\python.exe"
$deployment = (Resolve-Path -LiteralPath $DeploymentRoot).Path
$deploymentPython = Join-Path $deployment ".venv\Scripts\python.exe"
$sourceManifest = Join-Path $sourceRoot "release-manifest.json"

if ($ExpectedTestCount -ne 262) {
    throw "P1 candidate verification is fixed to 262 tests; received $ExpectedTestCount"
}
foreach ($required in @($sourcePython, $deploymentPython, $sourceManifest)) {
    if (-not (Test-Path -LiteralPath $required -PathType Leaf)) {
        throw "Required P1 candidate verification file is missing: $required"
    }
}

function Invoke-CheckedText {
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][string]$Executable,
        [Parameter(Mandatory = $true)][string[]]$Arguments
    )

    # unittest writes normal summaries to stderr; native exit status decides.
    $savedPreference = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        $output = & $Executable @Arguments 2>&1
        $exitCode = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $savedPreference
    }
    if ($exitCode -ne 0) {
        throw "$Name failed with exit code $exitCode`n$($output | Out-String)"
    }
    return ($output | Out-String).Trim()
}

function Invoke-CheckedJson {
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][string]$Executable,
        [Parameter(Mandatory = $true)][string[]]$Arguments
    )

    $text = Invoke-CheckedText -Name $Name -Executable $Executable -Arguments $Arguments
    try {
        return ($text | ConvertFrom-Json)
    } catch {
        throw "$Name did not return valid JSON: $text"
    }
}

# Do not permit an ambient source-root override to mask source/deployment drift.
$savedRoot = $env:MATERIALS_STUDIO_MCP_ROOT
Remove-Item Env:MATERIALS_STUDIO_MCP_ROOT -ErrorAction SilentlyContinue
try {
    $unittestText = Invoke-CheckedText -Name "unittest" -Executable $sourcePython -Arguments @("-m", "unittest", "discover", "-s", "tests", "-q")
    if ($unittestText -notmatch "Ran 262 tests") {
        throw "Expected the P1 candidate suite to run exactly 262 tests; received: $unittestText"
    }
    $sourcePipText = Invoke-CheckedText -Name "source pip check" -Executable $sourcePython -Arguments @("-m", "pip", "check")
    $sourceIntegrity = Invoke-CheckedJson -Name "source release manifest verification" -Executable $sourcePython -Arguments @("-m", "materials_studio_mcp.release", "verify", "--manifest", $sourceManifest)
    $deploymentPipText = Invoke-CheckedText -Name "deployment pip check" -Executable $deploymentPython -Arguments @("-m", "pip", "check")
    $deploymentIntegrity = Invoke-CheckedJson -Name "deployment verification" -Executable $deploymentPython -Arguments @("-m", "materials_studio_mcp.release", "verify-deployment", "--root", $deployment)
} finally {
    if ($null -eq $savedRoot) {
        Remove-Item Env:MATERIALS_STUDIO_MCP_ROOT -ErrorAction SilentlyContinue
    } else {
        $env:MATERIALS_STUDIO_MCP_ROOT = $savedRoot
    }
}

if ($sourceIntegrity.status -ne "pass" -or $sourceIntegrity.release_version -ne "1.3.0") {
    throw "Source release manifest is not a passing 1.3.0 candidate verification"
}
if ($deploymentIntegrity.status -ne "pass" -or $deploymentIntegrity.version -ne "1.3.0") {
    throw "Deployment verification is not a passing 1.3.0 verification"
}

$receiptDirectory = Split-Path -Parent $ReceiptPath
New-Item -ItemType Directory -Path $receiptDirectory -Force | Out-Null
$receipt = [ordered]@{
    schema_version = 1
    verification_entry = "scripts/verify_candidate_v1.ps1"
    candidate = [ordered]@{
        qualification = "p1_private_offline_castep_result_parser"
        release_version = "1.3.0"
        channel = "candidate"
        production_science_released = $false
        castep_execution = "unverified"
        castep_result_parsing = "unverified"
        public_mcp_tool_added = $false
        execution_started = $false
    }
    checks = @(
        [ordered]@{ name = "unittest"; status = "pass"; expected_test_count = 262 },
        [ordered]@{ name = "source_pip_check"; status = "pass"; output = $sourcePipText },
        [ordered]@{ name = "source_release_manifest"; status = $sourceIntegrity.status; sha256 = $sourceIntegrity.manifest_sha256 },
        [ordered]@{ name = "deployment_pip_check"; status = "pass"; output = $deploymentPipText },
        [ordered]@{ name = "deployment_verify"; status = $deploymentIntegrity.status; bundle_sha256 = $deploymentIntegrity.bundle_sha256 }
    )
}
[System.IO.File]::WriteAllText(
    $ReceiptPath,
    ($receipt | ConvertTo-Json -Depth 8) + [Environment]::NewLine,
    [System.Text.UTF8Encoding]::new($false)
)
Write-Output ("CANDIDATE_VERIFICATION_PASS receipt=" + (Resolve-Path -LiteralPath $ReceiptPath).Path)
