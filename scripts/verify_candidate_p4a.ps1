param(
    [string]$DeploymentRoot = "E:\ms_mcp\deployments\1.3.0",
    [string]$ReceiptPath = ""
)
Set-StrictMode -Version Latest
$ErrorActionPreference="Stop"
$root=(Resolve-Path -LiteralPath (Split-Path -Parent $PSScriptRoot)).Path
if([string]::IsNullOrWhiteSpace($ReceiptPath)){$ReceiptPath=Join-Path $root "docs\validation\receipts\p4a-locale-publication-preflight-verification.json"}
$python=Join-Path $root ".venv\Scripts\python.exe"
$deployment=(Resolve-Path -LiteralPath $DeploymentRoot).Path
$deploymentPython=Join-Path $deployment ".venv\Scripts\python.exe"
$manifest=Join-Path $root "release-manifest.json"
function Invoke-Text{param([string]$Name,[string]$Exe,[string[]]$Arguments);$saved=$ErrorActionPreference;$ErrorActionPreference="Continue";try{$out=& $Exe @Arguments 2>&1;$code=$LASTEXITCODE}finally{$ErrorActionPreference=$saved};if($code -ne 0){throw "$Name failed ($code)`n$($out|Out-String)"};return($out|Out-String).Trim()}
function Invoke-Json{param([string]$Name,[string]$Exe,[string[]]$Arguments);return(Invoke-Text $Name $Exe $Arguments)|ConvertFrom-Json}
$before=@{LC_ALL=$env:LC_ALL;LC_CTYPE=$env:LC_CTYPE;LANG=$env:LANG}
$forbidden=@(Get-Process -ErrorAction SilentlyContinue|Where-Object{$_.ProcessName -match '^(castep|castepexe|mpiexec|smpd|msserver|matserver|MaterialsStudio|RunCASTEP)$'})
if($forbidden.Count -ne 0){throw "Forbidden runtime process exists before P4-A"}
$full=Invoke-Text "unittest" $python @("-m","unittest","discover","-s","tests","-q")
if($full -notmatch "Ran 319 tests"){throw "Expected 319 tests: $full"}
$targeted=Invoke-Text "P4-A targeted" $python @("-m","unittest","tests.test_castep_p4a_preflight","-q")
if($targeted -notmatch "Ran 8 tests"){throw "Expected 8 P4-A tests: $targeted"}
$audit=Invoke-Json "MS Perl locale audit" $python @("-c","import json; from materials_studio_mcp.castep_p4a_preflight import audit_materials_studio_perl_locale; print(json.dumps(audit_materials_studio_perl_locale()))")
if($audit.status -ne "pass" -or $audit.stderr_bytes -ne 0 -or $audit.locale_warning_markers.Count -ne 0 -or $audit.castep_or_license_started){throw "P4-A locale audit failed"}
$preflight=Invoke-Json "fixed profile publication preflight" $python @("-c","import json; from materials_studio_mcp.castep_p4a_preflight import build_fixed_profile_publication_preflight; print(json.dumps(build_fixed_profile_publication_preflight()))")
if($preflight.execution_allowed -or $preflight.public_tool_added -or $preflight.status -ne "blocked_pending_p4b_public_api_review"){throw "P4-A publication boundary failed"}
$state=Invoke-Text "capability boundary" $python @("-c","from materials_studio_mcp.capability_registry import load_capability_registry; from materials_studio_mcp.public_registry import PUBLIC_TOOLS; c={x['id']:x for x in load_capability_registry()['capabilities']}; assert len(PUBLIC_TOOLS)==49; assert c['castep.p4a_locale_and_publication_preflight']['verified']; assert not c['castep.calculation']['verified']; assert not c['results.castep_parsing']['verified']; print('p4a_internal_verified_public_unchanged')")
$sourcePip=Invoke-Text "source pip" $python @("-m","pip","check")
$source=Invoke-Json "source integrity" $python @("-m","materials_studio_mcp.release","verify","--manifest",$manifest)
$deploymentPip=Invoke-Text "deployment pip" $deploymentPython @("-m","pip","check")
$deployed=Invoke-Json "deployment integrity" $deploymentPython @("-m","materials_studio_mcp.release","verify-deployment","--root",$deployment)
$after=@{LC_ALL=$env:LC_ALL;LC_CTYPE=$env:LC_CTYPE;LANG=$env:LANG}
if(($before|ConvertTo-Json -Compress) -ne ($after|ConvertTo-Json -Compress)){throw "Parent locale environment changed"}
$forbiddenAfter=@(Get-Process -ErrorAction SilentlyContinue|Where-Object{$_.ProcessName -match '^(castep|castepexe|mpiexec|smpd|msserver|matserver|MaterialsStudio|RunCASTEP)$'})
if($forbiddenAfter.Count -ne 0){throw "Forbidden runtime process appeared during P4-A"}
if($source.status -ne "pass" -or $source.public_tool_count -ne 49 -or $deployed.status -ne "pass"){throw "P4-A integrity failure"}
$receipt=[ordered]@{
 schema_version=1
 verification_entry="scripts/verify_candidate_p4a.ps1"
 outcome=[ordered]@{
  qualification="pass"
  scope="locale_safe_child_environment_and_nonexecuting_fixed_profile_publication_preflight"
  ms_perl_sha256=$audit.perl.sha256
  locale_before=$audit.environment_policy.locale_before
  locale_after=$audit.environment_policy.locale_after
  locale_stderr_bytes=0
  locale_warning_markers=@()
  parent_environment_unchanged=$true
  castep_or_license_started=$false
  public_tool_added=$false
  public_tool_count=49
  public_castep_calculation="unverified"
  public_castep_parsing="unverified"
 }
 checks=@(
  [ordered]@{name="unittest";status="pass";expected_test_count=319},
  [ordered]@{name="p4a_targeted";status="pass";expected_test_count=8},
  [ordered]@{name="ms_perl_locale";status=$audit.status;stderr_bytes=$audit.stderr_bytes;stderr_sha256=$audit.stderr_sha256},
  [ordered]@{name="publication_preflight";status="pass";execution_allowed=$false;public_tool_added=$false},
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
Write-Output("P4A_VERIFICATION_PASS receipt="+(Resolve-Path -LiteralPath $ReceiptPath).Path)
