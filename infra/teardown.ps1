<#
.SYNOPSIS
  Delete everything Phase 1 created. Run this straight after the demo.
#>
[CmdletBinding()]
param(
    [string]$ResourceGroup = "rg-lakebase-migration-demo",
    [switch]$Force
)

$ErrorActionPreference = "Stop"

Write-Host "Resources in $ResourceGroup :" -ForegroundColor Cyan
az resource list --resource-group $ResourceGroup --query "[].{name:name,type:type}" -o table

if (-not $Force) {
    $c = Read-Host "`nDelete resource group '$ResourceGroup' and everything in it? (yes/no)"
    if ($c -ne "yes") { Write-Host "aborted"; return }
}

az group delete --name $ResourceGroup --yes --no-wait
Write-Host "Deletion started (running in the background)." -ForegroundColor Green
Write-Host "Lakebase instances and Unity Catalog objects are NOT in this resource group -"
Write-Host "clean those up with databricks/99_teardown.sql."
