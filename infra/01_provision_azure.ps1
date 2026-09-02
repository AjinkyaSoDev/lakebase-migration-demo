<#
.SYNOPSIS
  Phase 1 infrastructure: the Azure landing zone for the migration.

.DESCRIPTION
  Provisions:
    * a resource group
    * (optional, -WithSourceVm) an Ubuntu VM running PostgreSQL 17 that plays the
      role of the "on-premises" server. You need this for a *live* online
      migration, because the migration service connects **inbound** to the
      source - a laptop behind NAT is not reachable from Azure.
    * Azure Database for PostgreSQL Flexible Server - the migration target,
      pre-configured for logical replication so online (CDC) mode works.

  A note on tooling: Azure Database Migration Service *Classic* no longer covers
  PostgreSQL -> Flexible Server. That scenario now lives in the migration service
  built into Flexible Server itself (`az postgres flexible-server migration ...`),
  which is what infra/02_run_migration.ps1 drives.

.EXAMPLE
  .\infra\01_provision_azure.ps1 -Location westeurope -WithSourceVm
#>
[CmdletBinding()]
param(
    [string]$ResourceGroup = "rg-lakebase-migration-demo",
    [string]$Location      = "westeurope",
    [string]$TargetName    = "pg-lakebase-demo-$((Get-Random -Maximum 9999))",
    [string]$AdminUser     = "pgadmin",
    [securestring]$AdminPassword,
    [string]$SourceVmName  = "vm-onprem-pg",
    [switch]$WithSourceVm,
    [string]$Sku           = "Standard_D2ds_v4",
    [string]$Tier          = "GeneralPurpose"
)

$ErrorActionPreference = "Stop"

if (-not $AdminPassword) {
    $AdminPassword = Read-Host -AsSecureString "Admin password for the Azure PostgreSQL target"
}
$plainPw = [Runtime.InteropServices.Marshal]::PtrToStringAuto(
    [Runtime.InteropServices.Marshal]::SecureStringToBSTR($AdminPassword))

Write-Host "==> Resource group $ResourceGroup ($Location)" -ForegroundColor Cyan
az group create --name $ResourceGroup --location $Location --output none

# --------------------------------------------------------------- source VM ---
if ($WithSourceVm) {
    Write-Host "==> 'On-premises' PostgreSQL 17 VM: $SourceVmName" -ForegroundColor Cyan
    Write-Host "    (this stands in for the on-prem estate so the online migration"
    Write-Host "     has a source it can actually reach)"

    $cloudInit = Join-Path $PSScriptRoot "cloud-init-postgres.yaml"
    az vm create `
        --resource-group $ResourceGroup --name $SourceVmName `
        --image Ubuntu2404 --size Standard_D2s_v3 `
        --admin-username azureuser --generate-ssh-keys `
        --custom-data $cloudInit --public-ip-sku Standard --output none

    az vm open-port --resource-group $ResourceGroup --name $SourceVmName --port 5432 --output none
    $vmIp = az vm show -d --resource-group $ResourceGroup --name $SourceVmName --query publicIps -o tsv
    Write-Host "    source VM public IP: $vmIp"
    Write-Host "    postgres user 'replicator' / password 'replicator_pw' (demo only)"
}

# ------------------------------------------------------------------ target ---
Write-Host "==> Azure Database for PostgreSQL Flexible Server: $TargetName" -ForegroundColor Cyan
az postgres flexible-server create `
    --resource-group $ResourceGroup --name $TargetName --location $Location `
    --admin-user $AdminUser --admin-password $plainPw `
    --sku-name $Sku --tier $Tier --version 17 `
    --storage-size 32 --public-access 0.0.0.0 --yes --output none

Write-Host "==> Enabling logical replication on the target (required for online mode)" -ForegroundColor Cyan
foreach ($p in @(
    @{ n = "wal_level";             v = "logical" },
    @{ n = "max_replication_slots"; v = "20" },
    @{ n = "max_wal_senders";       v = "20" },
    @{ n = "max_worker_processes";  v = "16" }
)) {
    az postgres flexible-server parameter set `
        --resource-group $ResourceGroup --server-name $TargetName `
        --name $p.n --value $p.v --output none
    Write-Host "    $($p.n) = $($p.v)"
}

az postgres flexible-server restart --resource-group $ResourceGroup --name $TargetName --output none

# Allow Azure services + this machine
$myIp = (Invoke-RestMethod -Uri "https://api.ipify.org?format=json").ip
az postgres flexible-server firewall-rule create `
    --resource-group $ResourceGroup --name $TargetName `
    --rule-name allow-demo-client --start-ip-address $myIp --end-ip-address $myIp --output none
Write-Host "    firewall opened for $myIp"

$fqdn = az postgres flexible-server show `
    --resource-group $ResourceGroup --name $TargetName --query fullyQualifiedDomainName -o tsv

Write-Host ""
Write-Host "Phase 1 landing zone ready." -ForegroundColor Green
Write-Host "  target FQDN : $fqdn"
Write-Host ""
Write-Host "Add to your .env:" -ForegroundColor Yellow
Write-Host "  AZPG_HOST=$fqdn"
Write-Host "  AZPG_USER=$AdminUser"
Write-Host "  AZPG_PASSWORD=<the password you just entered>"
Write-Host "  AZPG_DB=retail_onprem"
Write-Host ""
Write-Host "Next: .\infra\02_run_migration.ps1 -ResourceGroup $ResourceGroup -TargetName $TargetName"
