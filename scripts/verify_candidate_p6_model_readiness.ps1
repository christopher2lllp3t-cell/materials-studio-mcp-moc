param(
    [string]$DeploymentRoot = "E:\ms_mcp\deployments\1.3.9",
    [string]$ExpectedCurrentTarget = "E:\ms_mcp\deployments\1.3.6",
    [string]$ReceiptPath = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$sourceRoot = (Resolve-Path -LiteralPath (Split-Path -Parent $PSScriptRoot)).Path
if ([string]::IsNullOrWhiteSpace($ReceiptPath)) {
    $ReceiptPath = Join-Path $sourceRoot "docs\validation\receipts\p6-model-readiness-candidate-verification.json"
}
$sourcePython = Join-Path $sourceRoot ".venv\Scripts\python.exe"
$deployment = (Resolve-Path -LiteralPath $DeploymentRoot).Path
$deploymentPython = Join-Path $deployment ".venv\Scripts\python.exe"
$manifest = Join-Path $sourceRoot "release-manifest.json"

function Invoke-Text {
    param([string]$Name, [string]$Exe, [string[]]$Arguments)
    $saved = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    try {
        $out = & $Exe @Arguments 2>&1
        $code = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $saved
    }
    if ($code -ne 0) { throw ("{0} failed ({1}): {2}" -f $Name, $code, ($out | Out-String)) }
    return ($out | Out-String).Trim()
}

function Invoke-Json {
    param([string]$Name, [string]$Exe, [string[]]$Arguments)
    return (Invoke-Text $Name $Exe $Arguments) | ConvertFrom-Json
}

function Get-ForbiddenProcesses {
    return @(
        Get-Process -ErrorAction SilentlyContinue |
            Where-Object { $_.ProcessName -match '^(castep|castepexe|mpiexec|smpd|msserver|matserver|MaterialsStudio|RunCASTEP)$' }
    )
}

foreach ($file in @($sourcePython, $deploymentPython, $manifest)) {
    if (-not (Test-Path -LiteralPath $file -PathType Leaf)) { throw "P6 verification input is missing: $file" }
}
$current = Get-Item -LiteralPath "E:\ms_mcp\deployments\current" -Force
if ($current.LinkType -ne "Junction") { throw "Current deployment pointer is not a junction" }
$currentTarget = (Resolve-Path -LiteralPath ([string]$current.Target)).Path
$expectedTarget = (Resolve-Path -LiteralPath $ExpectedCurrentTarget).Path
if (-not $currentTarget.Equals($expectedTarget, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Candidate verification must not activate current: actual=$currentTarget expected=$expectedTarget"
}
if (@(Get-ForbiddenProcesses).Count -ne 0) { throw "Forbidden runtime process exists before P6 verification" }

$sourceTests = Invoke-Text "source regression" $sourcePython @("-m", "unittest", "discover", "-s", "tests", "-q")
if ($sourceTests -notmatch "Ran\s+(\d+)\s+tests") { throw "Unable to determine source test count: $sourceTests" }
$sourceTestCount = [int]$Matches[1]
if ($sourceTestCount -lt 1) { throw "Source test count must be positive" }
$sourcePip = Invoke-Text "source pip check" $sourcePython @("-m", "pip", "check")
$sourceIntegrity = Invoke-Json "source release integrity" $sourcePython @("-m", "materials_studio_mcp.release", "verify", "--manifest", $manifest)
if ($sourceIntegrity.status -ne "pass" -or $sourceIntegrity.public_tool_count -ne 53) {
    throw "Source P6 release integrity/public registry verification failed"
}

$beforeDeployment = @(
    Get-ChildItem -LiteralPath $deployment -File -Recurse |
        Sort-Object FullName |
        ForEach-Object { "{0}|{1}|{2}" -f $_.FullName.Substring($deployment.Length), $_.Length, $_.LastWriteTimeUtc.Ticks }
)
$previousMcpRoot = $env:MATERIALS_STUDIO_MCP_ROOT
$previousManifestRoot = $env:MS_MOC_MCP_ROOT
$previousBytecode = $env:PYTHONDONTWRITEBYTECODE
try {
    $env:MATERIALS_STUDIO_MCP_ROOT = $deployment
    $env:MS_MOC_MCP_ROOT = $deployment
    $env:PYTHONDONTWRITEBYTECODE = "1"
    $candidatePip = Invoke-Text "candidate pip check" $deploymentPython @("-m", "pip", "check")
    $candidateIntegrity = Invoke-Json "candidate deployment integrity" $deploymentPython @("-m", "materials_studio_mcp.release", "verify-deployment", "--root", $deployment)
    if ($candidateIntegrity.status -ne "pass") { throw "Candidate deployment integrity failed" }
    $capabilityAudit = Invoke-Json "candidate capability registry audit" $deploymentPython @("-c", "import json; from materials_studio_mcp.capability_registry import audit_capability_registry; print(json.dumps(audit_capability_registry()))")
    if ($capabilityAudit.status -ne "pass" -or $capabilityAudit.summary.declared_verified -ne $capabilityAudit.summary.effective_verified) {
        throw "Candidate capability registry evidence audit failed"
    }
    $candidateTests = Invoke-Text "candidate P6 regression" $deploymentPython @("-m", "unittest", "discover", "-s", (Join-Path $deployment "tests"), "-p", "test_model_readiness_and_public_evidence.py", "-q")
    if ($candidateTests -notmatch "Ran\s+17\s+tests") { throw "Expected 17 P6 candidate tests: $candidateTests" }
    $boundary = Invoke-Json "candidate P6 boundary" $deploymentPython @(
        "-c",
        "import asyncio,json; from materials_studio_mcp import server; from materials_studio_mcp.public_registry import PUBLIC_TOOLS; from materials_studio_mcp.capability_registry import load_capability_registry; names={x.name for x in asyncio.run(server.mcp.list_tools())}; assert len(PUBLIC_TOOLS)==53==len(names); assert {'md_model_readiness_assess','md_model_gap_resolution_plan','md_search_public_model_evidence'} <= names; a=server.md_model_readiness_assess({'model_class':'organic_condensed_phase','components':[{'name':'benzene','count':1}],'target':{'engine':'structure_only','purpose':'build_only'}}); assert a['ok'] and a['data']['readiness']=='blocked' and not a['data']['execution_allowed']; p=server.md_search_public_model_evidence('benzene','pubchem'); assert p['ok'] and p['data']['network_access']=='not_requested'; c={x['id']:x for x in load_capability_registry()['capabilities']}; assert not c['castep.calculation']['verified'] and not c['results.castep_parsing']['verified']; print(json.dumps({'tool_count':len(names),'readiness':a['data']['readiness'],'network_access':p['data']['network_access']}))"
    )
    $locale = Invoke-Json "candidate MS Perl locale audit" $deploymentPython @("-c", "import json; from materials_studio_mcp.castep_p4a_preflight import audit_materials_studio_perl_locale; print(json.dumps(audit_materials_studio_perl_locale()))")
    if ($locale.status -ne "pass" -or $locale.stderr_bytes -ne 0 -or $locale.castep_or_license_started) {
        throw "Windows Perl locale safeguard failed"
    }
}
finally {
    if ($null -eq $previousMcpRoot) { Remove-Item Env:MATERIALS_STUDIO_MCP_ROOT -ErrorAction SilentlyContinue } else { $env:MATERIALS_STUDIO_MCP_ROOT = $previousMcpRoot }
    if ($null -eq $previousManifestRoot) { Remove-Item Env:MS_MOC_MCP_ROOT -ErrorAction SilentlyContinue } else { $env:MS_MOC_MCP_ROOT = $previousManifestRoot }
    if ($null -eq $previousBytecode) { Remove-Item Env:PYTHONDONTWRITEBYTECODE -ErrorAction SilentlyContinue } else { $env:PYTHONDONTWRITEBYTECODE = $previousBytecode }
}
$afterDeployment = @(
    Get-ChildItem -LiteralPath $deployment -File -Recurse |
        Sort-Object FullName |
        ForEach-Object { "{0}|{1}|{2}" -f $_.FullName.Substring($deployment.Length), $_.Length, $_.LastWriteTimeUtc.Ticks }
)
if (($beforeDeployment -join [Environment]::NewLine) -ne ($afterDeployment -join [Environment]::NewLine)) {
    throw "P6 candidate verification wrote to the immutable deployment"
}
if (@(Get-ForbiddenProcesses).Count -ne 0) { throw "Forbidden runtime process appeared during P6 verification" }

$receipt = [ordered]@{
    schema_version = 1
    verification_entry = "scripts/verify_candidate_p6_model_readiness.ps1"
    outcome = [ordered]@{
        qualification = "pass"
        candidate_version = "1.3.9"
        activated = $false
        current_target = $currentTarget
        public_tool_count = $boundary.tool_count
        model_readiness = $boundary.readiness
        public_network_access = $boundary.network_access
        general_castep_calculation = "unverified"
        general_castep_parsing = "unverified"
        locale_stderr_bytes = $locale.stderr_bytes
        production_science_released = $false
    }
    checks = @(
        [ordered]@{name="current_unchanged"; status="pass"; target=$currentTarget},
        [ordered]@{name="source_regression"; status="pass"; observed_test_count=$sourceTestCount},
        [ordered]@{name="source_pip_check"; status="pass"; output=$sourcePip},
        [ordered]@{name="source_integrity"; status=$sourceIntegrity.status; sha256=$sourceIntegrity.manifest_sha256},
        [ordered]@{name="candidate_pip_check"; status="pass"; output=$candidatePip},
        [ordered]@{name="candidate_integrity"; status=$candidateIntegrity.status; bundle_sha256=$candidateIntegrity.bundle_sha256},
        [ordered]@{name="candidate_capability_registry"; status=$capabilityAudit.status; declared_verified=$capabilityAudit.summary.declared_verified; effective_verified=$capabilityAudit.summary.effective_verified},
        [ordered]@{name="candidate_p6_regression"; status="pass"; expected_test_count=17},
        [ordered]@{name="p6_public_boundary"; status="pass"; public_tool_count=$boundary.tool_count; network_access=$boundary.network_access},
        [ordered]@{name="windows_perl_locale"; status="pass"; stderr_bytes=$locale.stderr_bytes},
        [ordered]@{name="deployment_read_only"; status="pass"},
        [ordered]@{name="forbidden_processes"; status="pass"; count=0}
    )
}
New-Item -ItemType Directory -Path (Split-Path -Parent $ReceiptPath) -Force | Out-Null
[IO.File]::WriteAllText($ReceiptPath, ($receipt | ConvertTo-Json -Depth 8) + [Environment]::NewLine, [Text.UTF8Encoding]::new($false))
Write-Output ("P6_CANDIDATE_VERIFICATION_PASS receipt=" + (Resolve-Path -LiteralPath $ReceiptPath).Path)
