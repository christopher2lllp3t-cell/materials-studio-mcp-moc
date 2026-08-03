param(
    [Parameter(Mandatory = $true)]
    [ValidatePattern('^\d+\.\d+\.\d+$')]
    [string]$TargetVersion,
    [Parameter(Mandatory = $true)]
    [string]$ExpectedCurrentTarget,
    [string]$InstallRoot = "E:\ms_mcp\deployments",
    [string]$ReceiptDirectory = "E:\ms_mcp\deployment-activation-receipts",
    [switch]$ConfirmRollback
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

if (-not $ConfirmRollback) {
    throw "Refusing to roll back without -ConfirmRollback"
}

$switchArguments = @{
    TargetVersion = $TargetVersion
    ExpectedCurrentTarget = $ExpectedCurrentTarget
    InstallRoot = $InstallRoot
    ReceiptDirectory = $ReceiptDirectory
    ConfirmSwitch = $true
}
& (Join-Path $PSScriptRoot "switch_current_release_v1.ps1") @switchArguments
if ($LASTEXITCODE -ne 0) {
    throw "Rollback switch failed"
}
