param(
    [string]$DeploymentRoot = "E:\ms_mcp\deployments\1.3.4",
    [string]$BundleDirectory = "E:\ms_mcp\releases\materials-studio-mcp-moc-1.3.4",
    [string]$ExpectedCurrentTarget = "E:\ms_mcp\deployments\1.3.0",
    [string]$ReceiptPath = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$sourceRoot = (Resolve-Path -LiteralPath (Split-Path -Parent $PSScriptRoot)).Path
if ([string]::IsNullOrWhiteSpace($ReceiptPath)) {
    $ReceiptPath = Join-Path $sourceRoot "docs\validation\receipts\p5a-1.3.4-candidate-verification.json"
}
$deployment = (Resolve-Path -LiteralPath $DeploymentRoot).Path
$bundleRoot = (Resolve-Path -LiteralPath $BundleDirectory).Path
$deploymentPython = Join-Path $deployment ".venv\Scripts\python.exe"
$input = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String("RDpc5YiG5a2Q5Yqo5Yqb5a2m5qih5oufXDA3X21jcF9tYXRlcmlhbHNfc3R1ZGlvXHF0el9hbHBoYV9zdGFuZGFsb25lX2NhbmRpZGF0ZV8xXzNfMF9yMVxzdGFuZGFsb25lX2lucHV0X21hbmlmZXN0Lmpzb24="))
$inputSha = "8CAF21ABEB448A6D2669AA10684362652B2E97A1677D8C1AC1682F11CECA1C79"

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

function Get-DeploymentSnapshot {
    param([string]$Root)
    return @(
        Get-ChildItem -LiteralPath $Root -File -Recurse |
            Sort-Object FullName |
            ForEach-Object {
                $relative = $_.FullName.Substring($Root.Length).TrimStart('\')
                "{0}|{1}|{2}" -f $relative, $_.Length, $_.LastWriteTimeUtc.Ticks
            }
    )
}

if (-not (Test-Path -LiteralPath $deploymentPython -PathType Leaf)) {
    throw "Candidate deployment Python is missing: $deploymentPython"
}
if (-not (Test-Path -LiteralPath (Join-Path $deployment "scripts\switch_current_release_v1.ps1") -PathType Leaf)) {
    throw "Candidate current-switch script is missing"
}
if ((Get-FileHash -Algorithm SHA256 -LiteralPath $input).Hash -ne $inputSha) {
    throw "Fixed P3-C input manifest drift"
}
$current = Get-Item -LiteralPath "E:\ms_mcp\deployments\current" -Force
if ($current.LinkType -ne "Junction") { throw "Current deployment pointer is not a junction" }
$currentTarget = (Resolve-Path -LiteralPath ([string]$current.Target)).Path
$expectedTarget = (Resolve-Path -LiteralPath $ExpectedCurrentTarget).Path
if (-not $currentTarget.Equals($expectedTarget, [StringComparison]::OrdinalIgnoreCase)) {
    throw "Candidate verification must not activate current: actual=$currentTarget expected=$expectedTarget"
}

$forbiddenBefore = @(Get-ForbiddenProcesses)
if ($forbiddenBefore.Count -ne 0) { throw "Forbidden runtime process exists before P5-A" }

$bundlePath = Join-Path $bundleRoot "release-bundle.json"
$bundle = Get-Content -LiteralPath $bundlePath -Raw | ConvertFrom-Json
if ($bundle.schema_version -ne 1 -or $bundle.version -ne "1.3.4" -or $bundle.production_science_released) {
    throw "Candidate bundle metadata is invalid"
}
$bundleFailures = @()
foreach ($entry in $bundle.files) {
    $path = Join-Path $bundleRoot ([string]$entry.path).Replace('/', '\')
    if (-not (Test-Path -LiteralPath $path -PathType Leaf)) {
        $bundleFailures += "missing:$($entry.path)"
        continue
    }
    if ((Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash -ne $entry.sha256) {
        $bundleFailures += "hash:$($entry.path)"
    }
}
if ($bundleFailures.Count -ne 0) { throw "Candidate bundle integrity failed: $($bundleFailures -join ', ')" }
$generated = @($bundle.files | Where-Object {
    ([string]$_.path) -match '(?i)(^|/)(__pycache__|[^/]+\.egg-info)(/|$)' -or
    ([string]$_.path) -match '(?i)\.(pyc|pyo)$'
})
if ($generated.Count -ne 0) { throw "Candidate bundle contains generated Python artifacts" }

$installReceipt = Get-Content -LiteralPath (Join-Path $deployment "install-receipt.json") -Raw | ConvertFrom-Json
if ($installReceipt.version -ne "1.3.4" -or $installReceipt.activated) {
    throw "Candidate installation receipt is invalid or activated"
}

$before = Get-DeploymentSnapshot $deployment
$previousMcpRoot = $env:MATERIALS_STUDIO_MCP_ROOT
$previousManifestRoot = $env:MS_MOC_MCP_ROOT
$previousBytecode = $env:PYTHONDONTWRITEBYTECODE
try {
    $env:MATERIALS_STUDIO_MCP_ROOT = $deployment
    $env:MS_MOC_MCP_ROOT = $deployment
    $env:PYTHONDONTWRITEBYTECODE = "1"
    $pip = Invoke-Text "candidate pip check" $deploymentPython @("-m", "pip", "check")
    $integrity = Invoke-Json "candidate deployment integrity" $deploymentPython @("-m", "materials_studio_mcp.release", "verify-deployment", "--root", $deployment)
    if ($integrity.status -ne "pass") { throw "Candidate deployment integrity did not pass" }
    $state = Invoke-Json "candidate public boundary" $deploymentPython @(
        "-c",
        "import json; from materials_studio_mcp import __version__; from materials_studio_mcp.public_registry import PUBLIC_TOOLS; from materials_studio_mcp.capability_registry import load_capability_registry; c={x['id']:x for x in load_capability_registry()['capabilities']}; assert __version__=='1.3.4'; assert len(PUBLIC_TOOLS)==50; assert c['castep.fixed_profile_public_preflight']['verified']; assert c['castep.fixed_profile_public_preflight']['exposure']=='public'; assert not c['castep.calculation']['verified']; assert not c['results.castep_parsing']['verified']; print(json.dumps({'version':__version__,'public_tool_count':len(PUBLIC_TOOLS)}))"
    )
    $public = Invoke-Json "candidate fixed-profile preflight" $deploymentPython @(
        "-c",
        "import json; from materials_studio_mcp.server import ms_castep_fixed_profile_preflight; print(json.dumps(ms_castep_fixed_profile_preflight(r'$input',r'$inputSha')))"
    )
    if (-not $public.ok -or $public.data.status -ne "fixed_profile_preflight_pass" -or $public.data.execution_allowed -or $public.data.public_registration_state -ne "not_registered") {
        throw "Candidate fixed-profile public preflight boundary failed"
    }
    $locale = Invoke-Json "candidate MS Perl locale audit" $deploymentPython @(
        "-c",
        "import json; from materials_studio_mcp.castep_p4a_preflight import audit_materials_studio_perl_locale; print(json.dumps(audit_materials_studio_perl_locale()))"
    )
    if ($locale.status -ne "pass" -or $locale.stderr_bytes -ne 0 -or $locale.castep_or_license_started) {
        throw "Candidate locale safeguard failed"
    }
}
finally {
    if ($null -eq $previousMcpRoot) { Remove-Item Env:MATERIALS_STUDIO_MCP_ROOT -ErrorAction SilentlyContinue } else { $env:MATERIALS_STUDIO_MCP_ROOT = $previousMcpRoot }
    if ($null -eq $previousManifestRoot) { Remove-Item Env:MS_MOC_MCP_ROOT -ErrorAction SilentlyContinue } else { $env:MS_MOC_MCP_ROOT = $previousManifestRoot }
    if ($null -eq $previousBytecode) { Remove-Item Env:PYTHONDONTWRITEBYTECODE -ErrorAction SilentlyContinue } else { $env:PYTHONDONTWRITEBYTECODE = $previousBytecode }
}
$after = Get-DeploymentSnapshot $deployment
if (($before -join [Environment]::NewLine) -ne ($after -join [Environment]::NewLine)) {
    throw "Candidate read-only preflight wrote to the deployment"
}
$forbiddenAfter = @(Get-ForbiddenProcesses)
if ($forbiddenAfter.Count -ne 0) { throw "Forbidden runtime process appeared during P5-A" }

$receipt = [ordered]@{
    schema_version = 1
    verification_entry = "scripts/verify_candidate_p5a.ps1"
    outcome = [ordered]@{
        qualification = "pass"
        candidate_version = "1.3.4"
        activated = $false
        current_target = $currentTarget
        public_tool_count = $state.public_tool_count
        fixed_preflight_request_sha256 = $public.data.request_sha256
        execution_allowed = $false
        generated_python_artifact_count = 0
        locale_stderr_bytes = $locale.stderr_bytes
        general_castep_calculation = "unverified"
        general_castep_parsing = "unverified"
    }
    checks = @(
        [ordered]@{name="current_unchanged";status="pass";target=$currentTarget},
        [ordered]@{name="bundle_integrity";status="pass";file_count=@($bundle.files).Count},
        [ordered]@{name="install_receipt";status="pass";activated=$false},
        [ordered]@{name="deployment_pip_check";status="pass";output=$pip},
        [ordered]@{name="deployment_integrity";status=$integrity.status;bundle_sha256=$integrity.bundle_sha256},
        [ordered]@{name="public_boundary";status="pass";version=$state.version;public_tool_count=$state.public_tool_count},
        [ordered]@{name="public_fixed_preflight";status="pass";request_sha256=$public.data.request_sha256},
        [ordered]@{name="locale_safeguard";status="pass";stderr_bytes=$locale.stderr_bytes},
        [ordered]@{name="deployment_read_only";status="pass"},
        [ordered]@{name="forbidden_processes";status="pass";count=0}
    )
}
New-Item -ItemType Directory -Path (Split-Path -Parent $ReceiptPath) -Force | Out-Null
[IO.File]::WriteAllText($ReceiptPath, ($receipt | ConvertTo-Json -Depth 8) + [Environment]::NewLine, [Text.UTF8Encoding]::new($false))
Write-Output ("P5A_CANDIDATE_VERIFICATION_PASS receipt=" + (Resolve-Path -LiteralPath $ReceiptPath).Path)
