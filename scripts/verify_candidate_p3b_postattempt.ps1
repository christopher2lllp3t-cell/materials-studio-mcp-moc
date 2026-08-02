param(
    [string]$DeploymentRoot = "E:\ms_mcp\deployments\1.3.0",
    [string]$ReceiptPath = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$sourceRoot = (Resolve-Path -LiteralPath (Split-Path -Parent $PSScriptRoot)).Path
if ([string]::IsNullOrWhiteSpace($ReceiptPath)) {
    $ReceiptPath = Join-Path $sourceRoot "docs\validation\receipts\p3b-real-castep-attempt-1-verification.json"
}
$python = Join-Path $sourceRoot ".venv\Scripts\python.exe"
$deployment = (Resolve-Path -LiteralPath $DeploymentRoot).Path
$deploymentPython = Join-Path $deployment ".venv\Scripts\python.exe"
$manifest = Join-Path $sourceRoot "release-manifest.json"
$attempt = Join-Path $sourceRoot "docs\validation\receipts\p3b-real-castep-attempt-1.json"
$attemptSha = "B42D1C43E9ACE35AA9EECE4A0E28C1912997E0EA07AAF7A7D50F72BA1B13054C"

function Invoke-CheckedText {
    param([string]$Name, [string]$Executable, [string[]]$Arguments)
    $saved = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try { $output = & $Executable @Arguments 2>&1; $code = $LASTEXITCODE }
    finally { $ErrorActionPreference = $saved }
    if ($code -ne 0) { throw "$Name failed with exit code $code`n$($output | Out-String)" }
    return ($output | Out-String).Trim()
}

function Invoke-CheckedJson {
    param([string]$Name, [string]$Executable, [string[]]$Arguments)
    $text = Invoke-CheckedText -Name $Name -Executable $Executable -Arguments $Arguments
    try { return $text | ConvertFrom-Json }
    catch { throw "$Name did not return JSON: $text" }
}

foreach ($required in @($python, $deploymentPython, $manifest, $attempt)) {
    if (-not (Test-Path -LiteralPath $required -PathType Leaf)) {
        throw "Required post-attempt evidence is missing: $required"
    }
}
if ((Get-FileHash -Algorithm SHA256 -LiteralPath $attempt).Hash -ne $attemptSha) {
    throw "Archived attempt-1 receipt hash changed"
}
$data = Get-Content -LiteralPath $attempt -Raw | ConvertFrom-Json
if (
    $data.status -ne "nonzero_exit" -or
    $data.process.exit_code -ne 1 -or
    $null -ne $data.parser -or
    $data.owned_processes_remaining.Count -ne 0 -or
    $data.authorization_sha256 -ne "34E07A795C681FD0E8D71C18A0E6479AF02E9446576A90D6E9F867BDD6BC3C2F"
) {
    throw "Archived attempt-1 receipt does not match the reviewed failed-before-CASTEP outcome"
}
$forbidden = Get-Process -ErrorAction SilentlyContinue | Where-Object {
    $_.ProcessName -match '^(castep|mpiexec|msserver|matserver|MaterialsStudio|RunCASTEP)$'
}
if ($forbidden) { throw "CASTEP/MS/MPI process remains after attempt 1" }

$full = Invoke-CheckedText -Name "unittest" -Executable $python -Arguments @("-m","unittest","discover","-s","tests","-q")
if ($full -notmatch "Ran 305 tests") { throw "Expected 305 tests: $full" }
$targeted = Invoke-CheckedText -Name "P3-B targeted" -Executable $python -Arguments @("-m","unittest","tests.test_castep_real_qualification_runner","-q")
if ($targeted -notmatch "Ran 15 tests") { throw "Expected 15 P3-B tests: $targeted" }
$retired = Invoke-CheckedText -Name "r1 retirement" -Executable $python -Arguments @(
    "-c",
    "from materials_studio_mcp.castep_real_qualification_runner import PLAN_RETIRED_AFTER_ATTEMPT_1; assert PLAN_RETIRED_AFTER_ATTEMPT_1; print('retired')"
)
$sourcePip = Invoke-CheckedText -Name "source pip check" -Executable $python -Arguments @("-m","pip","check")
$sourceIntegrity = Invoke-CheckedJson -Name "source integrity" -Executable $python -Arguments @("-m","materials_studio_mcp.release","verify","--manifest",$manifest)
$deploymentPip = Invoke-CheckedText -Name "deployment pip check" -Executable $deploymentPython -Arguments @("-m","pip","check")
$deploymentIntegrity = Invoke-CheckedJson -Name "deployment integrity" -Executable $deploymentPython -Arguments @("-m","materials_studio_mcp.release","verify-deployment","--root",$deployment)
if ($sourceIntegrity.status -ne "pass" -or $sourceIntegrity.public_tool_count -ne 49) { throw "Source integrity failed" }
if ($deploymentIntegrity.status -ne "pass") { throw "Deployment integrity failed" }

$receipt = [ordered]@{
    schema_version = 1
    verification_entry = "scripts/verify_candidate_p3b_postattempt.ps1"
    outcome = [ordered]@{
        qualification = "failed_before_castep_start"
        attempt_number = 1
        retry_authorized = $false
        old_plan_retired = $true
        castep_execution = "unverified"
        license_evidence = "unverified"
        result_parsing = "not_exercised"
        production_science_released = $false
        public_tool_count = 49
        attempt_receipt_sha256 = $attemptSha
        original_job_receipt_sha256 = "DFBC27FB62A490D9D8559A804C55B413A9803C11060E2976E72C51646AC0B187"
    }
    checks = @(
        [ordered]@{ name = "attempt_receipt"; status = "pass"; exit_code = 1; parser = $null },
        [ordered]@{ name = "owned_processes_remaining"; status = "pass"; count = 0 },
        [ordered]@{ name = "r1_plan_retired"; status = "pass"; output = $retired },
        [ordered]@{ name = "unittest"; status = "pass"; expected_test_count = 305 },
        [ordered]@{ name = "p3b_targeted"; status = "pass"; expected_test_count = 15 },
        [ordered]@{ name = "source_pip_check"; status = "pass"; output = $sourcePip },
        [ordered]@{ name = "source_integrity"; status = $sourceIntegrity.status; sha256 = $sourceIntegrity.manifest_sha256 },
        [ordered]@{ name = "deployment_pip_check"; status = "pass"; output = $deploymentPip },
        [ordered]@{ name = "deployment_integrity"; status = $deploymentIntegrity.status; bundle_sha256 = $deploymentIntegrity.bundle_sha256 }
    )
}
New-Item -ItemType Directory -Path (Split-Path -Parent $ReceiptPath) -Force | Out-Null
[IO.File]::WriteAllText($ReceiptPath, ($receipt | ConvertTo-Json -Depth 8) + [Environment]::NewLine, [Text.UTF8Encoding]::new($false))
Write-Output ("P3B_POSTATTEMPT_VERIFICATION_PASS receipt=" + (Resolve-Path -LiteralPath $ReceiptPath).Path)
