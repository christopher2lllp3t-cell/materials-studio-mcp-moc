param(
    [string]$DeploymentRoot = "E:\ms_mcp\deployments\1.3.0",
    [string]$ReceiptPath = ""
)
Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$root = (Resolve-Path -LiteralPath (Split-Path -Parent $PSScriptRoot)).Path
if ([string]::IsNullOrWhiteSpace($ReceiptPath)) {
    $ReceiptPath = Join-Path $root "docs\validation\receipts\p3c-corrected-prerun-verification.json"
}
$python = Join-Path $root ".venv\Scripts\python.exe"
$deployment = (Resolve-Path -LiteralPath $DeploymentRoot).Path
$deploymentPython = Join-Path $deployment ".venv\Scripts\python.exe"
$manifest = Join-Path $root "release-manifest.json"
$plan = Join-Path $root "docs\validation\receipts\p3c-corrected-real-castep-qualification-plan.json"
$planSha = "10F3C622A161EAB3F25B0A9E19031AA9C485C7946E758CFDE5C1CD625B5F726B"
$planFileSha = "2630D7E6CB02F5A6E907E1800A8B76E2720C2E6E196EFE007DEED178A41CB454"
$inputSha = "8CAF21ABEB448A6D2669AA10684362652B2E97A1677D8C1AC1682F11CECA1C79"
$input = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String("RDpc5YiG5a2Q5Yqo5Yqb5a2m5qih5oufXDA3X21jcF9tYXRlcmlhbHNfc3R1ZGlvXHF0el9hbHBoYV9zdGFuZGFsb25lX2NhbmRpZGF0ZV8xXzNfMF9yMVxzdGFuZGFsb25lX2lucHV0X21hbmlmZXN0Lmpzb24="))

function Invoke-Text {
    param([string]$Name,[string]$Exe,[string[]]$Arguments)
    $saved=$ErrorActionPreference; $ErrorActionPreference="Continue"
    try { $out=& $Exe @Arguments 2>&1; $code=$LASTEXITCODE } finally { $ErrorActionPreference=$saved }
    if ($code -ne 0) { throw "$Name failed ($code)`n$($out | Out-String)" }
    return ($out | Out-String).Trim()
}
function Invoke-Json {
    param([string]$Name,[string]$Exe,[string[]]$Arguments)
    return (Invoke-Text $Name $Exe $Arguments) | ConvertFrom-Json
}
foreach ($file in @($python,$deploymentPython,$manifest,$plan,$input)) {
    if (-not (Test-Path -LiteralPath $file -PathType Leaf)) { throw "Missing P3-C prerequisite: $file" }
}
if ((Get-FileHash -Algorithm SHA256 -LiteralPath $plan).Hash -ne $planFileSha) { throw "P3-C plan file drift" }
if ((Get-FileHash -Algorithm SHA256 -LiteralPath $input).Hash -ne $inputSha) { throw "P3-C input drift" }
$forbidden=@(Get-Process -ErrorAction SilentlyContinue | Where-Object { $_.ProcessName -match '^(castep|mpiexec|msserver|matserver|MaterialsStudio|RunCASTEP)$' })
if ($forbidden.Count -ne 0) { throw "Forbidden process exists before P3-C" }

$full=Invoke-Text "unittest" $python @("-m","unittest","discover","-s","tests","-q")
if ($full -notmatch "Ran 305 tests") { throw "Expected 305 tests: $full" }
$targeted=Invoke-Text "P3-C targeted" $python @("-m","unittest","tests.test_castep_real_qualification_runner","-q")
if ($targeted -notmatch "Ran 15 tests") { throw "Expected 15 targeted tests: $targeted" }
$binding=Invoke-Text "P3-C binding" $python @("-c","import pathlib; from materials_studio_mcp.castep_real_qualification_runner import _load_plan,APPROVED_PLAN_SHA256,RETIRED_PLAN_SHA256; p=_load_plan(pathlib.Path(r'$plan')); assert p['plan_sha256']==APPROVED_PLAN_SHA256==r'$planSha'; assert RETIRED_PLAN_SHA256!=APPROVED_PLAN_SHA256; print(APPROVED_PLAN_SHA256)")
$sourcePip=Invoke-Text "source pip" $python @("-m","pip","check")
$source=Invoke-Json "source integrity" $python @("-m","materials_studio_mcp.release","verify","--manifest",$manifest)
$deploymentPip=Invoke-Text "deployment pip" $deploymentPython @("-m","pip","check")
$deployed=Invoke-Json "deployment integrity" $deploymentPython @("-m","materials_studio_mcp.release","verify-deployment","--root",$deployment)
if ($source.status -ne "pass" -or $source.public_tool_count -ne 49 -or $deployed.status -ne "pass") { throw "P3-C integrity failure" }
$forbiddenAfter=@(Get-Process -ErrorAction SilentlyContinue | Where-Object { $_.ProcessName -match '^(castep|mpiexec|msserver|matserver|MaterialsStudio|RunCASTEP)$' })
if ($forbiddenAfter.Count -ne 0) { throw "Forbidden process appeared during P3-C pre-run verification" }

$receipt=[ordered]@{
    schema_version=1
    verification_entry="scripts/verify_candidate_p3c.ps1"
    candidate=[ordered]@{
        qualification="p3c_corrected_single_use_real_castep_prerun"
        plan_sha256=$planSha
        input_manifest_sha256=$inputSha
        prior_attempt=1
        prior_plan_retired=$true
        new_authorization_consumed=$false
        real_castep_execution_started=$false
        castep_execution="unverified"
        license_evidence="unverified"
        production_science_released=$false
        public_tool_count=49
    }
    checks=@(
        [ordered]@{name="unittest";status="pass";expected_test_count=305},
        [ordered]@{name="p3c_targeted";status="pass";expected_test_count=15},
        [ordered]@{name="corrected_plan_binding";status="pass";output=$binding},
        [ordered]@{name="harmless_quoted_batch_path";status="pass";covered_by="test_raw_windows_command_line_handles_quoted_batch_path_without_backslash_quotes"},
        [ordered]@{name="source_pip_check";status="pass";output=$sourcePip},
        [ordered]@{name="source_integrity";status=$source.status;sha256=$source.manifest_sha256},
        [ordered]@{name="deployment_pip_check";status="pass";output=$deploymentPip},
        [ordered]@{name="deployment_integrity";status=$deployed.status;bundle_sha256=$deployed.bundle_sha256},
        [ordered]@{name="forbidden_processes_before_after";status="pass";count=0}
    )
}
New-Item -ItemType Directory -Path (Split-Path -Parent $ReceiptPath) -Force | Out-Null
[IO.File]::WriteAllText($ReceiptPath,($receipt | ConvertTo-Json -Depth 8)+[Environment]::NewLine,[Text.UTF8Encoding]::new($false))
Write-Output ("P3C_PRERUN_VERIFICATION_PASS receipt="+(Resolve-Path -LiteralPath $ReceiptPath).Path)
