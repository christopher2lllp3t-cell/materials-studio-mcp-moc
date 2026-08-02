param(
    [string]$DeploymentRoot = "E:\ms_mcp\deployments\1.3.0",
    [string]$ReceiptPath = ""
)
Set-StrictMode -Version Latest
$ErrorActionPreference="Stop"
$root=(Resolve-Path -LiteralPath (Split-Path -Parent $PSScriptRoot)).Path
if([string]::IsNullOrWhiteSpace($ReceiptPath)){$ReceiptPath=Join-Path $root "docs\validation\receipts\p4c-public-fixed-profile-preflight-verification.json"}
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
if($forbidden.Count -ne 0){throw "Forbidden runtime process exists before P4-C"}
$full=Invoke-Text "unittest" $python @("-m","unittest","discover","-s","tests","-q")
if($full -notmatch "Ran 322 tests"){throw "Expected 322 tests: $full"}
$targeted=Invoke-Text "P4-C targeted" $python @("-m","unittest","tests.test_castep_p4c_public_preflight","-q")
if($targeted -notmatch "Ran 3 tests"){throw "Expected 3 P4-C tests: $targeted"}
$public=Invoke-Json "public fixed-profile preflight" $python @("-c","import json; from materials_studio_mcp.server import ms_castep_fixed_profile_preflight; print(json.dumps(ms_castep_fixed_profile_preflight(r'$input',r'$inputSha')))")
if(-not $public.ok -or $public.data.status -ne "fixed_profile_preflight_pass" -or $public.data.execution_allowed -or $public.data.public_registration_state -ne "not_registered"){throw "Public P4-C preflight boundary failed"}
$locale=Invoke-Json "MS Perl locale audit" $python @("-c","import json; from materials_studio_mcp.castep_p4a_preflight import audit_materials_studio_perl_locale; print(json.dumps(audit_materials_studio_perl_locale()))")
if($locale.status -ne "pass" -or $locale.stderr_bytes -ne 0 -or $locale.castep_or_license_started){throw "Locale safeguard failed"}
$state=Invoke-Text "capability boundary" $python @("-c","from materials_studio_mcp.public_registry import PUBLIC_TOOLS; from materials_studio_mcp.capability_registry import load_capability_registry; c={x['id']:x for x in load_capability_registry()['capabilities']}; assert len(PUBLIC_TOOLS)==50; assert c['castep.fixed_profile_public_preflight']['verified']; assert c['castep.fixed_profile_public_preflight']['exposure']=='public'; assert not c['castep.calculation']['verified']; assert not c['results.castep_parsing']['verified']; print('p4c_r0_fixed_preflight_only_general_execution_unverified')")
$sourcePip=Invoke-Text "source pip" $python @("-m","pip","check")
$source=Invoke-Json "source integrity" $python @("-m","materials_studio_mcp.release","verify","--manifest",$manifest)
$deploymentPip=Invoke-Text "deployment pip" $deploymentPython @("-m","pip","check")
$deployed=Invoke-Json "deployment integrity" $deploymentPython @("-m","materials_studio_mcp.release","verify-deployment","--root",$deployment)
if($source.status -ne "pass" -or $source.public_tool_count -ne 50 -or $deployed.status -ne "pass"){throw "P4-C integrity failure"}
$forbiddenAfter=@(Get-Process -ErrorAction SilentlyContinue|Where-Object{$_.ProcessName -match '^(castep|castepexe|mpiexec|smpd|msserver|matserver|MaterialsStudio|RunCASTEP)$'})
if($forbiddenAfter.Count -ne 0){throw "Forbidden runtime process appeared during P4-C"}
$receipt=[ordered]@{
 schema_version=1
 verification_entry="scripts/verify_candidate_p4c_final.ps1"
 outcome=[ordered]@{
  qualification="pass"
  scope="public_R0_exact_fixed_profile_preflight_only"
  fixed_preflight_request_sha256=$public.data.request_sha256
  execution_allowed=$false
  files_written=$false
  public_tool_count=50
  locale_stderr_bytes=0
  general_castep_calculation="unverified"
  general_castep_parsing="unverified"
 }
 checks=@(
  [ordered]@{name="unittest";status="pass";expected_test_count=322},
  [ordered]@{name="p4c_targeted";status="pass";expected_test_count=3},
  [ordered]@{name="public_preflight";status="pass";request_sha256=$public.data.request_sha256},
  [ordered]@{name="locale_safeguard";status="pass";stderr_bytes=0},
  [ordered]@{name="capability_boundary";status="pass";output=$state},
  [ordered]@{name="source_pip_check";status="pass";output=$sourcePip},
  [ordered]@{name="source_integrity";status=$source.status;sha256=$source.manifest_sha256},
  [ordered]@{name="deployment_pip_check";status="pass";output=$deploymentPip},
  [ordered]@{name="deployment_integrity";status=$deployed.status;bundle_sha256=$deployed.bundle_sha256},
  [ordered]@{name="forbidden_processes";status="pass";count=0}
 )
}
New-Item -ItemType Directory -Path(Split-Path -Parent $ReceiptPath)-Force|Out-Null
[IO.File]::WriteAllText($ReceiptPath,($receipt|ConvertTo-Json -Depth 8)+[Environment]::NewLine,[Text.UTF8Encoding]::new($false))
Write-Output("P4C_FINAL_VERIFICATION_PASS receipt="+(Resolve-Path -LiteralPath $ReceiptPath).Path)
