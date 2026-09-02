# Setup guide

Budget ~60 minutes the first time, mostly waiting on Azure. Everything after
the first run is `git pull` and `.env`.

## Prerequisites

| Thing | Notes |
| --- | --- |
| PostgreSQL 17 | Local install, or use `-WithSourceVm` in step 3 |
| Python 3.10+ | `pip install -r requirements.txt` |
| Azure CLI | `az login`, plus `az extension add --name rdbms-connect` |
| Databricks workspace | Lakebase enabled, permission to create UC objects |
| Databricks CLI | `pip install databricks-sdk` covers the Python path |

```powershell
cd C:\Workshops\LakebaseMigrationDemo
python -m venv .venv; .\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env    # then fill it in
```

---

## 1. Build the "on-premises" database

Logical replication has to be on before anything else works:

```sql
-- postgresql.conf
wal_level = logical
max_replication_slots = 10
max_wal_senders = 10
```

Restart Postgres, then:

```powershell
python src\setup_onprem.py
```

### If you can't restart the shared server

Changing `wal_level` needs a restart, and on a managed or shared workstation you
may not have rights to stop the service. Stand up your own cluster on a spare
port instead - this is what the demo was validated against:

```powershell
$bin = "C:\Program Files\PostgreSQL\17\bin"
$dd  = "C:\Workshops\pgdata-demo"

"YourPasswordHere" | Set-Content "$env:TEMP\pgpw.txt" -Encoding ascii -NoNewline
& "$bin\initdb.exe" -D $dd -U postgres --pwfile="$env:TEMP\pgpw.txt" -A scram-sha-256 -E UTF8 --locale=C
Remove-Item "$env:TEMP\pgpw.txt"

Add-Content "$dd\postgresql.conf" @"

port = 5433
listen_addresses = 'localhost'
wal_level = logical
max_replication_slots = 10
max_wal_senders = 10
"@

& "$bin\pg_ctl.exe" -D $dd -l "$dd\server.log" start
```

Set `ONPREM_PORT=5433` in `.env`. Stop it afterwards with
`& "$bin\pg_ctl.exe" -D $dd stop`. Nothing touches the pre-existing instance on
5432.

`setup_onprem.py` creates `retail_onprem`, applies the schema into the `retail`
schema, seeds 500 customers / 120 products / 400 orders, and creates the
`replicator` role and `retail_pub` publication. It refuses to continue and
prints the fix if `wal_level` is wrong.

Smoke test:

```powershell
python src\workload.py --tps 5 --duration 30
python src\verify_parity.py
```

If those two work, the local half of the demo is sound. Do this **before** you
spend money in Azure.

---

## 2. Reachability

The migration service connects *to* your source. A laptop Postgres behind NAT
is not reachable from Azure. Pick one:

- **Recommended for a demo:** `-WithSourceVm` in step 3 provisions an Ubuntu VM
  with PostgreSQL 17 already configured. Run `setup_onprem.py` against it by
  pointing `ONPREM_HOST` at its public IP. Honest framing: "this VM is standing
  in for the data centre."
- **Genuinely on-prem:** open 5432 inbound to the Azure PG service, or use a
  site-to-site VPN / ExpressRoute.

---

## 3. Provision Azure

```powershell
.\infra\01_provision_azure.ps1 -Location westeurope -WithSourceVm
```

Creates the resource group, the optional source VM, and an Azure PostgreSQL
Flexible Server v17 with `wal_level=logical` and the replication parameters
already set. It prints the `.env` values when it is done - paste them in.

Takes 8-12 minutes. Do it the morning of, not five minutes before.

---

## 4. Run the migration

```powershell
$env:AZPG_USER = "pgadmin"; $env:AZPG_PASSWORD = "<from .env>"

.\infra\02_run_migration.ps1 `
    -ResourceGroup rg-lakebase-migration-demo `
    -TargetName <server-name> `
    -SourceHost <source-ip> `
    -Mode Online -Watch
```

Wait for substate `WaitingForCutoverTrigger`. That means full load is done and
CDC is live - this is the state you want to be in when the demo starts, because
it is where the interesting bit happens.

Cut over with the same script plus `-Cutover` and the `-MigrationName` it
printed.

---

## 5. Databricks - ingest to Delta

Follow `databricks/01_lakeflow_connect.md`. Create the connection, then the
ingestion pipeline into `retail_demo.onprem`, schedule **Continuous**.

Import `databricks/01b_jdbc_ingest.py` as a notebook as well. You want the
fallback ready, not discovered.

Store the PG password properly:

```bash
databricks secrets create-scope lakebase-demo
databricks secrets put-secret lakebase-demo azpg-password
```

---

## 6. Databricks - Lakebase synced tables

Create a Lakebase project called `retail-lakebase` (Compute -> Lakebase), then:

```powershell
python databricks\02_create_synced_tables.py --mode CONTINUOUS
python databricks\02_create_synced_tables.py --status
```

Wait for all six to report an online/synced state. Initial sync is a few
minutes.

Then connect and run `databricks/03_serve_queries.sql`:

```powershell
$env:PGPASSWORD = "<lakebase-token>"
psql "host=<lakebase-host> port=5432 dbname=retail user=<you> sslmode=require"
```

---

## 7. Rehearse

```powershell
python src\workload.py --tps 10 --marker REHEARSAL
python src\verify_parity.py --watch 10
```

Confirm rows marked `REHEARSAL` reach Lakebase. If they do, you are ready.

---

## Teardown

```powershell
.\infra\teardown.ps1
```

Then run `databricks/99_teardown.sql` and delete the Lakebase project - it bills
independently of the Azure resource group.

---

## Troubleshooting

**`password authentication failed`** - check `pg_hba.conf` uses `scram-sha-256`
for host connections and that you restarted, not just reloaded.

**Migration stuck in `PerformingPreRequisiteSteps`** - the target could not
reach the source. Test with `psql` from the Azure Cloud Shell first.

**`wal_level` is `replica` on Azure PG** - server parameters need a restart
after change; the provisioning script does this, but verify with `SHOW
wal_level;`.

**Synced table creation fails on change data feed** - run the `ALTER TABLE ...
SET TBLPROPERTIES (delta.enableChangeDataFeed = true)` the error prints, then
retry. `02_create_synced_tables.py` attempts this automatically but needs a SQL
warehouse to exist.

**Replication slot left behind** - after teardown, check the source for orphans:
`SELECT * FROM pg_replication_slots;` then `SELECT
pg_drop_replication_slot('<name>');`. An abandoned slot will grow WAL until the
disk fills.
