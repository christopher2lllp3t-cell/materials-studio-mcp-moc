param(
    [string]$InstallRoot = "E:\ms_mcp\deployments"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$rootFull = [System.IO.Path]::GetFullPath($InstallRoot).TrimEnd('\')
$current = Join-Path $rootFull "current"
if (-not (Test-Path -LiteralPath $current)) { throw "No active deployment pointer exists: $current" }
$item = Get-Item -LiteralPath $current -Force
if ($item.LinkType -ne "Junction") { throw "Current deployment pointer is not a junction: $current" }
$activeTarget = [string]$item.Target
$activeFull = [System.IO.Path]::GetFullPath($activeTarget)
if (-not $activeFull.StartsWith($rootFull + '\', [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Active deployment target escaped the install root"
}
$receiptPath = Join-Path $activeFull "install-receipt.json"
$receipt = Get-Content -LiteralPath $receiptPath -Raw | ConvertFrom-Json
$previous = [string]$receipt.previous_target
[System.IO.Directory]::Delete($current)
if (-not [string]::IsNullOrWhiteSpace($previous)) {
    $previousFull = [System.IO.Path]::GetFullPath($previous)
    if (-not $previousFull.StartsWith($rootFull + '\', [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "Rollback target escaped the install root"
    }
    if (-not (Test-Path -LiteralPath $previousFull -PathType Container)) {
        throw "Rollback target is missing: $previousFull"
    }
    New-Item -ItemType Junction -Path $current -Target $previousFull | Out-Null
    Write-Output $previousFull
} else {
    Write-Output "No previous deployment; current pointer removed."
}
