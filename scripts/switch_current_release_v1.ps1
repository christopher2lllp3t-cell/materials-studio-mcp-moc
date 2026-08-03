param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^\d+\.\d+\.\d+$')]
    [string]$TargetVersion,
    [Parameter(Mandatory = $true)]
    [string]$ExpectedCurrentTarget,
    [string]$InstallRoot = "E:\ms_mcp\deployments",
    [string]$ReceiptDirectory = "E:\ms_mcp\deployment-activation-receipts",
    [switch]$ConfirmSwitch
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

if (-not $ConfirmSwitch) {
    throw "Refusing to switch current without -ConfirmSwitch"
}

$rootFull = [System.IO.Path]::GetFullPath($InstallRoot).TrimEnd('\')
$current = Join-Path $rootFull "current"
$targetFull = [System.IO.Path]::GetFullPath((Join-Path $rootFull $TargetVersion))
if (-not $targetFull.StartsWith($rootFull + '\', [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Switch target escaped the install root"
}
if (-not (Test-Path -LiteralPath $targetFull -PathType Container)) {
    throw "Switch target deployment is missing: $targetFull"
}
if (-not (Test-Path -LiteralPath $current)) {
    throw "Current deployment pointer is missing: $current"
}
$currentItem = Get-Item -LiteralPath $current -Force
if ($currentItem.LinkType -ne "Junction") {
    throw "Current deployment pointer is not a junction: $current"
}
$currentTarget = (Resolve-Path -LiteralPath ([string]$currentItem.Target)).Path
$expectedTarget = (Resolve-Path -LiteralPath $ExpectedCurrentTarget).Path
if (-not $currentTarget.Equals($expectedTarget, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Current deployment changed before switch: actual=$currentTarget expected=$expectedTarget"
}
if ($currentTarget.Equals($targetFull, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Switch target is already current: $targetFull"
}

$python = Join-Path $targetFull ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python -PathType Leaf)) {
    throw "Target deployment Python is missing: $python"
}
$previousMcpRoot = $env:MATERIALS_STUDIO_MCP_ROOT
$previousManifestRoot = $env:MS_MOC_MCP_ROOT
try {
    $env:MATERIALS_STUDIO_MCP_ROOT = $targetFull
    $env:MS_MOC_MCP_ROOT = $targetFull
    & $python -m pip check
    if ($LASTEXITCODE -ne 0) { throw "Target deployment dependency check failed" }
    & $python -m materials_studio_mcp.release verify-deployment --root $targetFull
    if ($LASTEXITCODE -ne 0) { throw "Target deployment integrity check failed" }
}
finally {
    if ($null -eq $previousMcpRoot) { Remove-Item Env:MATERIALS_STUDIO_MCP_ROOT -ErrorAction SilentlyContinue } else { $env:MATERIALS_STUDIO_MCP_ROOT = $previousMcpRoot }
    if ($null -eq $previousManifestRoot) { Remove-Item Env:MS_MOC_MCP_ROOT -ErrorAction SilentlyContinue } else { $env:MS_MOC_MCP_ROOT = $previousManifestRoot }
}

$next = Join-Path $rootFull ".current-next-$PID"
$backup = Join-Path $rootFull ".current-backup-$PID"
if ((Test-Path -LiteralPath $next) -or (Test-Path -LiteralPath $backup)) {
    throw "Refusing to reuse an existing switch staging pointer"
}
New-Item -ItemType Junction -Path $next -Target $targetFull | Out-Null
$nextItem = Get-Item -LiteralPath $next -Force
if ($nextItem.LinkType -ne "Junction") {
    throw "Switch staging pointer is not a junction"
}
$nextTarget = (Resolve-Path -LiteralPath ([string]$nextItem.Target)).Path
if (-not $nextTarget.Equals($targetFull, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Switch staging pointer target mismatch"
}

$switched = $false
try {
    Move-Item -LiteralPath $current -Destination $backup
    try {
        Move-Item -LiteralPath $next -Destination $current
    }
    catch {
        if (Test-Path -LiteralPath $current) {
            throw "Switch failed with an unexpected current pointer; manual recovery is required"
        }
        Move-Item -LiteralPath $backup -Destination $current
        throw
    }
    $active = Get-Item -LiteralPath $current -Force
    if ($active.LinkType -ne "Junction") { throw "Activated current pointer is not a junction" }
    $activeTarget = (Resolve-Path -LiteralPath ([string]$active.Target)).Path
    if (-not $activeTarget.Equals($targetFull, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Activated current pointer target mismatch"
    }
    $switched = $true
}
finally {
    if (-not $switched -and (Test-Path -LiteralPath $next)) {
        $nextItem = Get-Item -LiteralPath $next -Force
        if ($nextItem.LinkType -eq "Junction") { [System.IO.Directory]::Delete($next) }
    }
}

New-Item -ItemType Directory -Path $ReceiptDirectory -Force | Out-Null
$receipt = [ordered]@{
    schema_version = 1
    action = "switch_current_release"
    switched_at_utc = [DateTime]::UtcNow.ToString("o")
    previous_target = $currentTarget
    current_target = $targetFull
    target_version = $TargetVersion
    rollback_target = $currentTarget
    target_deployment_verified = $true
}
$receiptPath = Join-Path $ReceiptDirectory ("switch-{0}-{1}.json" -f $TargetVersion, [DateTime]::UtcNow.ToString("yyyyMMddTHHmmssfffZ"))
[IO.File]::WriteAllText($receiptPath, ($receipt | ConvertTo-Json -Depth 6) + [Environment]::NewLine, [Text.UTF8Encoding]::new($false))
if (Test-Path -LiteralPath $backup) {
    $backupItem = Get-Item -LiteralPath $backup -Force
    if ($backupItem.LinkType -ne "Junction") { throw "Switch backup pointer is not a junction" }
    $backupTarget = (Resolve-Path -LiteralPath ([string]$backupItem.Target)).Path
    if (-not $backupTarget.Equals($currentTarget, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Switch backup pointer target mismatch"
    }
    [System.IO.Directory]::Delete($backup)
}
Write-Output ("CURRENT_SWITCH_PASS target=" + $targetFull + " receipt=" + $receiptPath)
