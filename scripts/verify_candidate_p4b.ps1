param(
    [string]$DeploymentRoot = "E:\ms_mcp\deployments\1.3.0",
    [string]$ReceiptPath = ""
)
Set-StrictMode -Version Latest
$ErrorActionPreference="Stop"
throw "P4-B verification is historical after public P4-C registration; use verify_candidate_p4c_final.ps1"
$root=(Resolve-Path -LiteralPath (Split-Path -Parent $PSScriptRoot)).Path
if([string]::IsNullOrWhiteSpace($ReceiptPath)){$ReceiptPath=Join-Path $root "docs\validation\receipts\p4b-fixed-profile-contract-verification.json"}
$python=Join-Path $root ".venv\Scripts\python.exe"
$deployment=(Resolve-Path -LiteralPath $DeploymentRoot).Path
$deploymentPython=Join-Path $deployment ".venv\Scripts\python.exe"
$manifest=Join-Path $root "release-manifest.json"
$input=[Text.Encoding]::UTF8.GetString([Convert]::FromBase64String("RDpc5YiG5a2Q5Yqo5Yqb5a2m5qih5oufXDA3X21jcF9tYXRlcmlhbHNfc3R1ZGlvXHF0el9hbHBoYV9zdGFuZGFsb25lX2NhbmRpZGF0ZV8xXzNfMF9yMVxzdGFuZGFsb25lX2lucHV0X21hbmlmZXN0Lmpzb24="))
$inputSha="8CAF21ABEB448A6D2669AA10684362652B2E97A1677D8C1AC1682F11CECA1C79"
function Invoke-Text{param([string]$Name,[string]$Exe,[string[]]$Arguments);$saved=$ErrorActionPreference;$ErrorActionPreference="Continue";try{$out=& $Exe @Arguments 2>&1;$code=$LASTEXITCODE}finally{$ErrorActionPreference=$saved};if($code -ne 0){throw "$Name failed ($code)`n$($out|Out-String)"};return($out|Out-String).Trim()}
function Invoke-Json{param([string]$Name,[string]$Exe,[string[]]$Arguments);return(Invoke-Text $Name $Exe $Arguments)|ConvertFrom-Json}
if((Get-FileHash -Algorithm SHA256 -LiteralPath $input).Hash -ne $inputSha){throw "Fixed P3-C manifest drift"}
$forbidden=@(Get-Process -ErrorAction SilentlyContinue|Where-Object{$_.ProcessName -match '^(castep|castepexe|mpiexec|smpd|msserver|matserver|MaterialsStudio|RunCASTEP)$'})
if($forbidden.Count -ne 0){throw "Forbidden runtime process exists before P4-B"}
$full=Invoke-Text "unittest" $python @("-m","unittest","discover","-s","tests","-q")
if($full -notmatch "Ran 319 tests"){throw "Expected 319 tests: $full"}
$targeted=Invoke-Text "P4-B targeted" $python @("-m","unittest","tests.test_castep_p4b_contract","-q")
if($targeted -notmatch "Ran 5 tests"){throw "Expected 5 P4-B tests: $targeted"}
$contract=Invoke-Json "P4-B contract" $python @("-c","import json; from materials_studio_mcp.castep_p4b_contract import build_fixed_profile_public_api_contract; print(json.dumps(build_fixed_profile_public_api_contract()))")
$request=Invoke-Json "P4-B exact fixed input preflight" $python @("-c","import json,pathlib; from materials_studio_mcp.castep_p4b_contract import inspect_fixed_profile_preflight_request; print(json.dumps(inspect_fixed_profile_preflight_request(input_manifest=pathlib.Path(r'$input'),input_manifest_sha256=r'$inputSha')))")
if($contract.public_registration_state -ne "not_registered" -or $contract.execution.implemented -or $request.execution_allowed -or $request.public_registration_state -ne "not_registered"){throw "P4-B boundary failed"}
$state=Invoke-Text "public capability boundary" $python @("-c","from materials_studio_mcp.public_registry import PUBLIC_TOOLS; from materials_studio_mcp.capability_registry import load_capability_registry; c={x['id']:x for x in load_capability_registry()['capabilities']}; assert len(PUBLIC_TOOLS)==49; assert c['castep.p4b_fixed_profile_public_contract']['verified']; assert not c['castep.calculation']['verified']; assert not c['results.castep_parsing']['verified']; print('p4b_internal_contract_verified_public_unchanged')")
$sourcePip=Invoke-Text "source pip" $python @("-m","pip","check")
$source=Invoke-Json "source integrity" $python @("-m","materials_studio_mcp.release","verify","--manifest",$manifest)
$deploymentPip=Invoke-Text "deployment pip" $deploymentPython @("-m","pip","check")
$deployed=Invoke-Json "deployment integrity" $deploymentPython @("-m","materials_studio_mcp.release","verify-deployment","--root",$deployment)
if($source.status -ne "pass" -or $source.public_tool_count -ne 49 -or $deployed.status -ne "pass"){throw "P4-B integrity failure"}
$forbiddenAfter=@(Get-Process -ErrorAction SilentlyContinue|Where-Object{$_.ProcessName -match '^(castep|castepexe|mpiexec|smpd|msserver|matserver|MaterialsStudio|RunCASTEP)$'})
if($forbiddenAfter.Count -ne 0){throw "Forbidden runtime process appeared during P4-B"}
$receipt=[ordered]@{
 schema_version=1
 verification_entry="scripts/verify_candidate_p4b.ps1"
 outcome=[ordered]@{
  qualification="pass"
  scope="unregistered_fixed_profile_public_api_contract_only"
  contract_sha256=$contract.contract_sha256
  fixed_preflight_request_sha256=$request.request_sha256
  execution_allowed=$false
  public_tool_added=$false
  public_tool_count=49
  new_execution_authorization_required=$true
  public_confirmation_required_if_execution_is_implemented=$true
  rollback="candidate_only_no_deployment_or_current_pointer_change"
 }
 checks=@(
  [ordered]@{name="unittest";status="pass";expected_test_count=319},
  [ordered]@{name="p4b_targeted";status="pass";expected_test_count=5},
  [ordered]@{name="contract";status="pass";sha256=$contract.contract_sha256},
  [ordered]@{name="exact_input_preflight";status="pass";request_sha256=$request.request_sha256},
  [ordered]@{name="public_boundary";status="pass";output=$state},
  [ordered]@{name="source_pip_check";status="pass";output=$sourcePip},
  [ordered]@{name="source_integrity";status=$source.status;sha256=$source.manifest_sha256},
  [ordered]@{name="deployment_pip_check";status="pass";output=$deploymentPip},
  [ordered]@{name="deployment_integrity";status=$deployed.status;bundle_sha256=$deployed.bundle_sha256},
  [ordered]@{name="forbidden_processes";status="pass";count=0}
 )
}
New-Item -ItemType Directory -Path(Split-Path -Parent $ReceiptPath)-Force|Out-Null
[IO.File]::WriteAllText($ReceiptPath,($receipt|ConvertTo-Json -Depth 8)+[Environment]::NewLine,[Text.UTF8Encoding]::new($false))
Write-Output("P4B_VERIFICATION_PASS receipt="+(Resolve-Path -LiteralPath $ReceiptPath).Path)
