param(
    [string]$DeploymentRoot = "E:\ms_mcp\deployments\1.3.0",
    [string]$ReceiptPath = "",
    [int]$ExpectedTestCount = 290
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$sourceRoot = (Resolve-Path -LiteralPath (Split-Path -Parent $PSScriptRoot)).Path
if ([string]::IsNullOrWhiteSpace($ReceiptPath)) {
    $ReceiptPath = Join-Path $sourceRoot "docs\validation\receipts\p3a-real-castep-plan-verification.json"
}
$sourcePython = Join-Path $sourceRoot ".venv\Scripts\python.exe"
$deployment = (Resolve-Path -LiteralPath $DeploymentRoot).Path
$deploymentPython = Join-Path $deployment ".venv\Scripts\python.exe"
$sourceManifest = Join-Path $sourceRoot "release-manifest.json"
$frozenPlan = Join-Path $sourceRoot "docs\validation\receipts\p3a-real-castep-qualification-plan.json"

if ($ExpectedTestCount -ne 290) {
    throw "P3-A candidate verification is fixed to 290 tests; received $ExpectedTestCount"
}
foreach ($required in @($sourcePython, $deploymentPython, $sourceManifest, $frozenPlan)) {
    if (-not (Test-Path -LiteralPath $required -PathType Leaf)) {
        throw "Required P3-A verification file is missing: $required"
    }
}

function Invoke-CheckedText {
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][string]$Executable,
        [Parameter(Mandatory = $true)][string[]]$Arguments
    )
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
    try { return ($text | ConvertFrom-Json) }
    catch { throw "$Name did not return valid JSON: $text" }
}

$savedRoot = $env:MATERIALS_STUDIO_MCP_ROOT
Remove-Item Env:MATERIALS_STUDIO_MCP_ROOT -ErrorAction SilentlyContinue
try {
    $unittestText = Invoke-CheckedText -Name "unittest" -Executable $sourcePython -Arguments @("-m", "unittest", "discover", "-s", "tests", "-q")
    if ($unittestText -notmatch "Ran 290 tests") {
        throw "Expected the P3-A suite to run exactly 290 tests; received: $unittestText"
    }
    $planText = Invoke-CheckedText -Name "frozen P3-A plan validation" -Executable $sourcePython -Arguments @(
        "-c",
        "import json,pathlib; from materials_studio_mcp.castep_real_qualification_plan import validate_real_castep_qualification_plan; p=pathlib.Path(r'$frozenPlan'); d=json.loads(p.read_text(encoding='utf-8')); validate_real_castep_qualification_plan(d); print(d['plan_sha256'])"
    )
    $sourcePipText = Invoke-CheckedText -Name "source pip check" -Executable $sourcePython -Arguments @("-m", "pip", "check")
    $sourceIntegrity = Invoke-CheckedJson -Name "source release manifest verification" -Executable $sourcePython -Arguments @("-m", "materials_studio_mcp.release", "verify", "--manifest", $sourceManifest)
    $deploymentPipText = Invoke-CheckedText -Name "deployment pip check" -Executable $deploymentPython -Arguments @("-m", "pip", "check")
    $deploymentIntegrity = Invoke-CheckedJson -Name "deployment verification" -Executable $deploymentPython -Arguments @("-m", "materials_studio_mcp.release", "verify-deployment", "--root", $deployment)
} finally {
    if ($null -eq $savedRoot) { Remove-Item Env:MATERIALS_STUDIO_MCP_ROOT -ErrorAction SilentlyContinue }
    else { $env:MATERIALS_STUDIO_MCP_ROOT = $savedRoot }
}

if ($sourceIntegrity.status -ne "pass" -or $sourceIntegrity.release_version -ne "1.3.0") {
    throw "Source release manifest is not a passing 1.3.0 P3-A candidate verification"
}
if ($deploymentIntegrity.status -ne "pass" -or $deploymentIntegrity.version -ne "1.3.0") {
    throw "Deployment verification is not a passing 1.3.0 verification"
}

$receiptDirectory = Split-Path -Parent $ReceiptPath
New-Item -ItemType Directory -Path $receiptDirectory -Force | Out-Null
$receipt = [ordered]@{
    schema_version = 1
    verification_entry = "scripts/verify_candidate_p3a.ps1"
    candidate = [ordered]@{
        qualification = "p3a_real_castep_plan_execution_blocked"
        release_version = "1.3.0"
        channel = "candidate"
        production_science_released = $false
        castep_execution = "unverified"
        castep_result_parsing = "unverified"
        public_mcp_tool_added = $false
        real_castep_execution_started = $false
        license_acquired = $false
        plan_sha256 = $planText
    }
    checks = @(
        [ordered]@{ name = "unittest"; status = "pass"; expected_test_count = 290 },
        [ordered]@{ name = "frozen_plan"; status = "pass"; sha256 = $planText },
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
Write-Output ("P3A_CANDIDATE_VERIFICATION_PASS receipt=" + (Resolve-Path -LiteralPath $ReceiptPath).Path)
