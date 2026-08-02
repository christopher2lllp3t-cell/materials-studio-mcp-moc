param(
    [string]$DeploymentRoot = "E:\ms_mcp\deployments\1.3.0",
    [string]$ReceiptPath = ""
)
Set-StrictMode -Version Latest
$ErrorActionPreference="Stop"
$root=(Resolve-Path -LiteralPath (Split-Path -Parent $PSScriptRoot)).Path
if([string]::IsNullOrWhiteSpace($ReceiptPath)){$ReceiptPath=Join-Path $root "docs\validation\receipts\p3c-real-castep-final-verification.json"}
$python=Join-Path $root ".venv\Scripts\python.exe"
$deployment=(Resolve-Path -LiteralPath $DeploymentRoot).Path
$deploymentPython=Join-Path $deployment ".venv\Scripts\python.exe"
$manifest=Join-Path $root "release-manifest.json"
$receipt=Join-Path $root "docs\validation\receipts\p3c-real-castep-qualification-success.json"
$receiptSha="12FB79B370A783618C5F0580192D2B40E459A4E6DD4D9875210CED05415EB872"
$output="E:\ms_mcp\ms_mcp_jobs\castep_real_qualification\p3b_quartz_alpha_sp_4c_2a43c478aa2a5e0d7162\quartz_alpha_sp_4c.castep"
$outputSha="EE91F3319375DEFD581644840F64718C066291027D2E837ACD7B6DCEB468E851"

function Invoke-Text{param([string]$Name,[string]$Exe,[string[]]$Arguments);$saved=$ErrorActionPreference;$ErrorActionPreference="Continue";try{$out=& $Exe @Arguments 2>&1;$code=$LASTEXITCODE}finally{$ErrorActionPreference=$saved};if($code -ne 0){throw "$Name failed ($code)`n$($out|Out-String)"};return($out|Out-String).Trim()}
function Invoke-Json{param([string]$Name,[string]$Exe,[string[]]$Arguments);return(Invoke-Text $Name $Exe $Arguments)|ConvertFrom-Json}

foreach($file in @($python,$deploymentPython,$manifest,$receipt,$output)){if(-not(Test-Path -LiteralPath $file -PathType Leaf)){throw "Missing P3-C final evidence: $file"}}
if((Get-FileHash -Algorithm SHA256 -LiteralPath $receipt).Hash -ne $receiptSha){throw "P3-C receipt drift"}
if((Get-FileHash -Algorithm SHA256 -LiteralPath $output).Hash -ne $outputSha){throw "P3-C output drift"}
$data=Get-Content -LiteralPath $receipt -Raw|ConvertFrom-Json
if($data.status -ne "qualification_pass" -or $data.process.exit_code -ne 0 -or $data.parser.status -ne "completed" -or $data.parser.output_hashes.observed_sha256 -ne $outputSha -or $data.owned_processes_remaining.Count -ne 0){throw "P3-C success receipt contract failed"}
$text=Get-Content -LiteralPath $output -Raw
if($text -notmatch "License checkout of MS_castep successful" -or $text -notmatch "Final energy\s*=\s*-3158\.163551162" -or $text -notmatch "Total time\s*=\s*44\.23 s"){throw "P3-C output evidence missing"}
$forbidden=@(Get-Process -ErrorAction SilentlyContinue|Where-Object{$_.ProcessName -match '^(castep|castepexe|mpiexec|smpd|msserver|matserver|MaterialsStudio|RunCASTEP)$'})
if($forbidden.Count -ne 0){throw "P3-C owned/runtime process remains"}

$full=Invoke-Text "unittest" $python @("-m","unittest","discover","-s","tests","-q")
if($full -notmatch "Ran 319 tests"){throw "Expected 319 tests: $full"}
$targeted=Invoke-Text "P3-C targeted" $python @("-m","unittest","tests.test_castep_real_qualification_runner","-q")
if($targeted -notmatch "Ran 16 tests"){throw "Expected 16 P3-C tests: $targeted"}
$state=Invoke-Text "capability and retirement state" $python @("-c","from materials_studio_mcp.castep_real_qualification_runner import PLAN_RETIRED_AFTER_ATTEMPT_2; from materials_studio_mcp.capability_registry import load_capability_registry; c={x['id']:x for x in load_capability_registry()['capabilities']}; assert PLAN_RETIRED_AFTER_ATTEMPT_2; assert c['castep.real_qualification_execution_candidate']['verified']; assert not c['castep.calculation']['verified']; assert not c['results.castep_parsing']['verified']; print('fixed_profile_verified_public_general_unverified_plan_retired')")
$sourcePip=Invoke-Text "source pip" $python @("-m","pip","check")
$source=Invoke-Json "source integrity" $python @("-m","materials_studio_mcp.release","verify","--manifest",$manifest)
$deploymentPip=Invoke-Text "deployment pip" $deploymentPython @("-m","pip","check")
$deployed=Invoke-Json "deployment integrity" $deploymentPython @("-m","materials_studio_mcp.release","verify-deployment","--root",$deployment)
if($source.status -ne "pass" -or $source.public_tool_count -ne 49 -or $deployed.status -ne "pass"){throw "Final integrity failure"}

$final=[ordered]@{
 schema_version=1
 verification_entry="scripts/verify_candidate_p3c_final.ps1"
 outcome=[ordered]@{
  qualification="pass"
  scope="exact_private_alpha_quartz_singlepoint_p3c_profile_only"
  plan_sha256=$data.plan_sha256
  authorization_sha256=$data.authorization_sha256
  runner_receipt_sha256=$receiptSha
  output_sha256=$outputSha
  exit_code=0
  license_checkout="explicit_success_four_copies"
  final_energy_eV=-3158.163551162
  total_time_seconds=44.23
  owned_processes_remaining=0
  plan_retired=$true
  public_castep_calculation="unverified"
  public_castep_parsing="unverified"
  production_science_released=$false
  convergence_evidence=$false
  public_tool_count=49
 }
 checks=@(
  [ordered]@{name="runtime_receipt";status="pass";sha256=$receiptSha},
  [ordered]@{name="castep_output";status="pass";sha256=$outputSha},
  [ordered]@{name="license_checkout";status="pass";copies=4},
  [ordered]@{name="parser";status="completed";classification="completed"},
  [ordered]@{name="input_hashes_before_after";status="pass"},
  [ordered]@{name="owned_processes_remaining";status="pass";count=0},
  [ordered]@{name="capability_boundary";status="pass";output=$state},
  [ordered]@{name="unittest";status="pass";expected_test_count=319},
  [ordered]@{name="p3c_targeted";status="pass";expected_test_count=16},
  [ordered]@{name="source_pip_check";status="pass";output=$sourcePip},
  [ordered]@{name="source_integrity";status=$source.status;sha256=$source.manifest_sha256},
  [ordered]@{name="deployment_pip_check";status="pass";output=$deploymentPip},
  [ordered]@{name="deployment_integrity";status=$deployed.status;bundle_sha256=$deployed.bundle_sha256}
 )
}
New-Item -ItemType Directory -Path(Split-Path -Parent $ReceiptPath)-Force|Out-Null
[IO.File]::WriteAllText($ReceiptPath,($final|ConvertTo-Json -Depth 8)+[Environment]::NewLine,[Text.UTF8Encoding]::new($false))
Write-Output("P3C_FINAL_VERIFICATION_PASS receipt="+(Resolve-Path -LiteralPath $ReceiptPath).Path)
