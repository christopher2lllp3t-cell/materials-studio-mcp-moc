param(
    [string]$DeploymentRoot = "E:\ms_mcp\deployments\1.3.0",
    [string]$ReceiptPath = "",
    [int]$ExpectedTestCount = 303
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$sourceRoot = (Resolve-Path -LiteralPath (Split-Path -Parent $PSScriptRoot)).Path
if ([string]::IsNullOrWhiteSpace($ReceiptPath)) {
    $ReceiptPath = Join-Path $sourceRoot "docs\validation\receipts\p3b-real-castep-prerun-verification.json"
}
$sourcePython = Join-Path $sourceRoot ".venv\Scripts\python.exe"
$deployment = (Resolve-Path -LiteralPath $DeploymentRoot).Path
$deploymentPython = Join-Path $deployment ".venv\Scripts\python.exe"
$sourceManifest = Join-Path $sourceRoot "release-manifest.json"
$frozenPlan = Join-Path $sourceRoot "docs\validation\receipts\p3a-real-castep-qualification-plan.json"
$inputManifest = [Text.Encoding]::UTF8.GetString(
    [Convert]::FromBase64String("RDpc5YiG5a2Q5Yqo5Yqb5a2m5qih5oufXDA3X21jcF9tYXRlcmlhbHNfc3R1ZGlvXHF0el9hbHBoYV9zdGFuZGFsb25lX2NhbmRpZGF0ZV8xXzNfMF9yMVxzdGFuZGFsb25lX2lucHV0X21hbmlmZXN0Lmpzb24=")
)
$approvedPlanSha = "E461D57676903DEA6A19886D1AE85EB28859DC4AE2DC933D9890AA1E8D59C35E"
$approvedInputSha = "8CAF21ABEB448A6D2669AA10684362652B2E97A1677D8C1AC1682F11CECA1C79"

if ($ExpectedTestCount -ne 303) {
    throw "P3-B pre-run verification is fixed to 303 tests; received $ExpectedTestCount"
}
foreach ($required in @($sourcePython, $deploymentPython, $sourceManifest, $frozenPlan, $inputManifest)) {
    if (-not (Test-Path -LiteralPath $required -PathType Leaf)) {
        throw "Required P3-B pre-run file is missing: $required"
    }
}

function Invoke-CheckedText {
    param([string]$Name, [string]$Executable, [string[]]$Arguments)
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
    param([string]$Name, [string]$Executable, [string[]]$Arguments)
    $text = Invoke-CheckedText -Name $Name -Executable $Executable -Arguments $Arguments
    try { return ($text | ConvertFrom-Json) }
    catch { throw "$Name did not return valid JSON: $text" }
}

$forbidden = Get-Process -ErrorAction SilentlyContinue | Where-Object {
    $_.ProcessName -match '^(castep|mpiexec|msserver|matserver|MaterialsStudio|RunCASTEP)$'
}
if ($forbidden) {
    throw "A CASTEP/MS/MPI process already exists before P3-B pre-run qualification"
}

$savedRoot = $env:MATERIALS_STUDIO_MCP_ROOT
Remove-Item Env:MATERIALS_STUDIO_MCP_ROOT -ErrorAction SilentlyContinue
try {
    $unittestText = Invoke-CheckedText -Name "unittest" -Executable $sourcePython -Arguments @("-m", "unittest", "discover", "-s", "tests", "-q")
    if ($unittestText -notmatch "Ran 303 tests") {
        throw "Expected 303 tests; received: $unittestText"
    }
    $targetedText = Invoke-CheckedText -Name "P3-B targeted tests" -Executable $sourcePython -Arguments @("-m", "unittest", "tests.test_castep_real_qualification_runner", "-q")
    if ($targetedText -notmatch "Ran 13 tests") {
        throw "Expected 13 P3-B tests; received: $targetedText"
    }
    $bindingText = Invoke-CheckedText -Name "P3-B frozen binding" -Executable $sourcePython -Arguments @(
        "-c",
        "import hashlib,json,pathlib; from materials_studio_mcp.castep_real_qualification_runner import _load_plan,APPROVED_PLAN_SHA256; p=_load_plan(pathlib.Path(r'$frozenPlan')); m=pathlib.Path(r'$inputManifest'); assert p['plan_sha256']==APPROVED_PLAN_SHA256==r'$approvedPlanSha'; assert hashlib.sha256(m.read_bytes()).hexdigest().upper()==r'$approvedInputSha'; print(p['plan_sha256']+' '+r'$approvedInputSha')"
    )
    $sourcePipText = Invoke-CheckedText -Name "source pip check" -Executable $sourcePython -Arguments @("-m", "pip", "check")
    $sourceIntegrity = Invoke-CheckedJson -Name "source release manifest verification" -Executable $sourcePython -Arguments @("-m", "materials_studio_mcp.release", "verify", "--manifest", $sourceManifest)
    $deploymentPipText = Invoke-CheckedText -Name "deployment pip check" -Executable $deploymentPython -Arguments @("-m", "pip", "check")
    $deploymentIntegrity = Invoke-CheckedJson -Name "deployment verification" -Executable $deploymentPython -Arguments @("-m", "materials_studio_mcp.release", "verify-deployment", "--root", $deployment)
} finally {
    if ($null -eq $savedRoot) { Remove-Item Env:MATERIALS_STUDIO_MCP_ROOT -ErrorAction SilentlyContinue }
    else { $env:MATERIALS_STUDIO_MCP_ROOT = $savedRoot }
}

if ($sourceIntegrity.status -ne "pass" -or $sourceIntegrity.public_tool_count -ne 49) {
    throw "Source candidate integrity or public tool count failed"
}
if ($deploymentIntegrity.status -ne "pass" -or $deploymentIntegrity.version -ne "1.3.0") {
    throw "Immutable deployment verification failed"
}
$forbiddenAfter = Get-Process -ErrorAction SilentlyContinue | Where-Object {
    $_.ProcessName -match '^(castep|mpiexec|msserver|matserver|MaterialsStudio|RunCASTEP)$'
}
if ($forbiddenAfter) {
    throw "A CASTEP/MS/MPI process appeared during P3-B pre-run verification"
}

$receiptDirectory = Split-Path -Parent $ReceiptPath
New-Item -ItemType Directory -Path $receiptDirectory -Force | Out-Null
$receipt = [ordered]@{
    schema_version = 1
    verification_entry = "scripts/verify_candidate_p3b.ps1"
    candidate = [ordered]@{
        qualification = "p3b_real_castep_single_use_runner_prerun"
        release_version = "1.3.0"
        production_science_released = $false
        castep_execution = "unverified"
        castep_result_parsing = "unverified"
        public_mcp_tool_added = $false
        public_tool_count = 49
        real_castep_execution_started = $false
        authorization_consumed = $false
        license_acquired = $false
        plan_sha256 = $approvedPlanSha
        input_manifest_sha256 = $approvedInputSha
    }
    checks = @(
        [ordered]@{ name = "unittest"; status = "pass"; expected_test_count = 303 },
        [ordered]@{ name = "p3b_targeted"; status = "pass"; expected_test_count = 13 },
        [ordered]@{ name = "frozen_plan_and_input_binding"; status = "pass"; output = $bindingText },
        [ordered]@{ name = "source_pip_check"; status = "pass"; output = $sourcePipText },
        [ordered]@{ name = "source_release_manifest"; status = $sourceIntegrity.status; sha256 = $sourceIntegrity.manifest_sha256 },
        [ordered]@{ name = "deployment_pip_check"; status = "pass"; output = $deploymentPipText },
        [ordered]@{ name = "deployment_verify"; status = $deploymentIntegrity.status; bundle_sha256 = $deploymentIntegrity.bundle_sha256 },
        [ordered]@{ name = "forbidden_processes_before_and_after"; status = "pass"; count = 0 }
    )
}
[System.IO.File]::WriteAllText(
    $ReceiptPath,
    ($receipt | ConvertTo-Json -Depth 8) + [Environment]::NewLine,
    [System.Text.UTF8Encoding]::new($false)
)
Write-Output ("P3B_PRERUN_VERIFICATION_PASS receipt=" + (Resolve-Path -LiteralPath $ReceiptPath).Path)
