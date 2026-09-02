<#
.SYNOPSIS
  Phase 1 execution: run the online (CDC) migration into Azure PostgreSQL.

.DESCRIPTION
  Drives the migration service that is built into Azure Database for PostgreSQL
  Flexible Server. In Online mode it does an initial full load and then keeps
  replicating with logical decoding, so the workload generator can keep running
  against the source while you talk - that is the part the audience should see.

  Lifecycle:
    create   -> validate + full load starts
    (running)-> CDC keeps target in sync, "Waiting for cutover trigger"
    cutover  -> stops replication, target becomes authoritative

.EXAMPLE
  .\infra\02_run_migration.ps1 -ResourceGroup rg-lakebase-migration-demo `
      -TargetName pg-lakebase-demo-1234 -SourceHost 20.13.4.55 -Cutover:$false
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$ResourceGroup,
    [Parameter(Mandatory)][string]$TargetName,
    [Parameter(Mandatory)][string]$SourceHost,
    [int]$SourcePort         = 5432,
    [string]$SourceUser      = "replicator",
    [string]$SourcePassword  = "replicator_pw",
    [string]$SourceDb        = "retail_onprem",
    [string]$MigrationName   = "mig-retail-$(Get-Date -Format 'yyyyMMdd-HHmmss')",
    [ValidateSet("Online", "Offline")][string]$Mode = "Online",
    [switch]$Cutover,
    [switch]$Watch
)

$ErrorActionPreference = "Stop"

if ($Cutover) {
    Write-Host "==> Triggering cutover for $MigrationName" -ForegroundColor Cyan
    az postgres flexible-server migration update `
        --resource-group $ResourceGroup --name $TargetName `
        --migration-name $MigrationName --cutover
    return
}

# The CLI takes the source definition as a JSON document.
$definition = @{
    properties = @{
        sourceDbServerResourceId = "$SourceHost`:$SourcePort"
        secretParameters         = @{
            adminCredentials = @{
                sourceServerPassword = $SourcePassword
                targetServerPassword = $env:AZPG_PASSWORD
            }
            sourceServerUsername = $SourceUser
            targetServerUsername = $env:AZPG_USER
        }
        dbsToMigrate             = @($SourceDb)
        migrationMode            = $Mode
        sourceType               = "OnPremises"
        sslMode                  = "Prefer"
        overwriteDbsInTarget     = "True"
    }
} | ConvertTo-Json -Depth 8

$defFile = Join-Path $env:TEMP "migration-definition-$MigrationName.json"
$definition | Set-Content -Path $defFile -Encoding utf8

if (-not $env:AZPG_PASSWORD -or -not $env:AZPG_USER) {
    throw "Set AZPG_USER and AZPG_PASSWORD in the environment (they come from your .env) before running."
}

Write-Host "==> Creating $Mode migration '$MigrationName'" -ForegroundColor Cyan
Write-Host "    source : $SourceHost`:$SourcePort/$SourceDb"
Write-Host "    target : $TargetName"
az postgres flexible-server migration create `
    --resource-group $ResourceGroup --name $TargetName `
    --migration-name $MigrationName `
    --properties $defFile `
    --migration-mode $Mode.ToLower() `
    --output none

Write-Host "    submitted" -ForegroundColor Green

function Show-Status {
    $j = az postgres flexible-server migration show `
        --resource-group $ResourceGroup --name $TargetName `
        --migration-name $MigrationName -o json | ConvertFrom-Json
    $s = $j.currentStatus
    Write-Host ("  [{0:HH:mm:ss}] state={1}  substate={2}" -f (Get-Date), $s.state, $s.currentSubStateDetails.currentSubState)
    foreach ($d in $j.dbDetails.PSObject.Properties) {
        Write-Host ("      {0,-20} {1}" -f $d.Name, $d.Value.migrationState)
    }
    return $s.currentSubStateDetails.currentSubState
}

if ($Watch) {
    Write-Host "==> Watching (Ctrl+C to stop)" -ForegroundColor Cyan
    while ($true) {
        $sub = Show-Status
        if ($sub -eq "WaitingForCutoverTrigger") {
            Write-Host ""
            Write-Host "  Full load complete; CDC is keeping the target in sync." -ForegroundColor Green
            Write-Host "  Leave the workload generator running, show parity, then cut over with:" -ForegroundColor Yellow
            Write-Host "    .\infra\02_run_migration.ps1 -ResourceGroup $ResourceGroup -TargetName $TargetName -MigrationName $MigrationName -Cutover"
            break
        }
        if ($sub -in @("Completed", "Failed", "Canceled")) { break }
        Start-Sleep -Seconds 15
    }
} else {
    Show-Status | Out-Null
    Write-Host ""
    Write-Host "Watch progress with:" -ForegroundColor Yellow
    Write-Host "  .\infra\02_run_migration.ps1 -ResourceGroup $ResourceGroup -TargetName $TargetName -SourceHost $SourceHost -MigrationName $MigrationName -Watch"
}
