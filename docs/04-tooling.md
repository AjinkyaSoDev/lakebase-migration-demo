# Tooling: VS Code extensions and PostgreSQL extensions

Two different questions get conflated here, so they are answered separately.

- **PostgreSQL server extensions** - what has to be installed *inside* the
  database for the migration to work.
- **VS Code extensions** - what you drive it all from.

---

## Part 1: PostgreSQL server extensions

### The short answer: none

**This demo requires zero PostgreSQL extensions.** No `CREATE EXTENSION` runs
anywhere in `sql/`. Both CDC consumers use **native logical replication** with
the **built-in `pgoutput` output plugin**, which ships with PostgreSQL core
since v10 and is not an installable extension.

What is actually required is *configuration*, not extensions:

| Setting | Value | Why |
| --- | --- | --- |
| `wal_level` | `logical` | Puts row-level change data in the WAL. Needs a restart. |
| `max_replication_slots` | `>= 2` | One slot for the migration service, one for Lakeflow Connect. |
| `max_wal_senders` | `>= max_replication_slots` | One sender process per active slot. |
| `wal_sender_timeout` | `0` | Azure recommends disabling the 60s default so a long full-load isn't dropped mid-copy. |

Plus three objects, all core SQL - see `sql/02_enable_cdc.sql`:

```sql
CREATE ROLE replicator WITH LOGIN REPLICATION PASSWORD '...';
CREATE PUBLICATION retail_pub FOR TABLES IN SCHEMA retail;
ALTER TABLE retail.orders REPLICA IDENTITY DEFAULT;   -- PK is the row identity
```

`src/setup_onprem.py` checks all four settings and prints exact remediation.

### The common misconception: pglogical

You will find a lot of blog posts saying you need the `pglogical` extension.
**That was true for DMS Classic, which is retired for PostgreSQL -> Flexible
Server.** The current migration service (`az postgres flexible-server
migration`) is built on `pgcopydb` and uses `pgoutput` natively.

> "PgOutput is the default logical decoding plugin used for Online migration.
> If source PostgreSQL version < 10, test_decoding plugin is used."
> - [Azure online migration tutorial](https://learn.microsoft.com/en-us/azure/postgresql/migrate/migration-service/tutorial-migration-service-iaas-online)

Databricks Lakeflow Connect for PostgreSQL is the same story - `pgoutput`, a
publication, and a replication slot. No extension appears anywhere in its
[source setup docs](https://docs.databricks.com/aws/en/ingestion/lakeflow-connect/postgresql-source-setup).

> **AWS DMS is the exception - do not carry `pgoutput` across.** Everything above
> is scoped to the *Azure* migration service and Lakeflow Connect. AWS Database
> Migration Service does **not** support `pgoutput`. It uses **`test_decoding`**
> by default, and can use **`pglogical`** where available. So on an AWS-sourced
> migration the "you don't need pglogical" advice flips. The plugin depends on
> which service is reading the WAL, not on PostgreSQL itself.

### Where extensions *do* bite you

Even though this demo installs none, extensions matter in a real migration:

**Azure PG Flexible Server allow-lists extensions.** `CREATE EXTENSION` fails
unless the extension is first added to the `azure.extensions` server parameter.
The migration service's schema phase will therefore **fail** if your source
database has an extension that you have not allow-listed on the target.

```bash
az postgres flexible-server parameter set \
  --resource-group <rg> --server-name <target> \
  --name azure.extensions --value "pg_stat_statements,postgis"
```

Audit the source first:

```sql
SELECT extname, extversion FROM pg_extension ORDER BY 1;
```

Verdicts for extensions people commonly ask about:

| Extension | Needed here? | Notes |
| --- | --- | --- |
| `pglogical` | **No** | Not needed by *this* (Azure) design; DMS Classic required it. **AWS DMS can use it** - see the note above. |
| `pgoutput` | **Yes, implicitly** | Built-in plugin, *not* an extension. Nothing to install. **Not supported by AWS DMS.** |
| `test_decoding` | No | Built-in. Used for source PG < 10 here - but it is **AWS DMS's default plugin**. |
| `wal2json` | No | A different output plugin. Neither tool uses it. |
| `pg_stat_statements` | Optional | Useful for the "why is it slow" story. Allow-list on target if present on source. |
| `postgres_fdw` / `dblink` | No | Allow-list on target only if the source has them. |
| `pg_cron`, `azure_storage` | No | Azure-side conveniences, irrelevant to CDC. |

### Other real limits worth knowing

- **Tables need a primary key** (or `REPLICA IDENTITY FULL`/`INDEX`). With no PK
  and `REPLICA IDENTITY DEFAULT`, INSERT and TRUNCATE replicate but **UPDATE and
  DELETE are not supported and the migration fails**. Every table in
  `sql/01_onprem_schema.sql` has a single-column PK for exactly this reason.
- Databricks additionally recommends `REPLICA IDENTITY FULL` for tables with
  TOASTable (large) columns.
- **Roles are not migrated.** Use `pg_dumpall --globals-only` separately.
- A migration lives at most **7 days**; the online cutover window is **3 days**.
- **Disable HA and read replicas on the target** while migrating.
- Lakeflow Connect needs **PostgreSQL 13+**.

---

## Part 2: VS Code extensions

### The one that matters: PostgreSQL for VS Code

**`ms-ossdata.vscode-pgsql`** (publisher `ms-ossdata`, Microsoft's official
extension). This replaces the need for pgAdmin entirely and covers most of what
this demo does outside the terminal:

| Capability | Where it helps in this demo |
| --- | --- |
| Browse Azure subscriptions to find Flexible Servers | Finding the target created by `01_provision_azure.ps1` |
| Entra ID **or** password auth | Connecting to Azure PG without putting a password in a config file |
| Manage Flexible Server: start/stop, **firewall rules, server parameters** | Setting `wal_level=logical` and opening the firewall for Databricks egress - no portal round-trip |
| Create a Flexible Server from the IDE | An alternative to the provisioning script |
| Query editor with IntelliSense | Running `sql/02_enable_cdc.sql`, inspecting `pg_replication_slots` |
| Object Explorer + schema visualisation | Showing the `retail` schema on stage without a slide |
| Query plan visualisation | The "why the old system was slow" part of the story |
| `@pgsql` Copilot chat participant (Ask mode) | Asking questions about the live schema |
| Copilot **agent mode** tools - run queries, create tables, import CSV | Driving setup conversationally |

You already have this installed at **v1.28.0**.

### Worth adding: Databricks

**`databricks.databricks`** - not currently installed on this machine. It is
cloud-agnostic, so it works against your AWS-hosted workspace:

- Databricks Connect setup, and cell-by-cell notebook debugging
- Run local Python files on a cluster - useful for
  `databricks/02_create_synced_tables.py`
- Sync local files into the workspace - how `01b_jdbc_ingest.py` gets there
- Asset Bundles for jobs and pipelines

Note: deep interactive support is **Python-first**. R/Scala/SQL notebooks can be
run as jobs but get no richer IDE support.

### Already installed and used by this repo

| Extension | Used for |
| --- | --- |
| `ms-vscode.powershell` | `infra/*.ps1` - provisioning, migration, teardown |
| `ms-azuretools.vscode-azureresourcegroups` | Watching the resource group during the demo |
| `ms-azuretools.vscode-bicep` | Only if you convert the PowerShell to IaC |
| `ms-azuretools.vscode-azure-github-copilot` | `@azure` chat participant for CLI syntax |
| `ms-azuretools.vscode-azure-mcp-server` | Azure MCP tools in agent mode |
| `eamodio.gitlens` | History on `create_report.py`-style fix commits |

You also have two third-party SQL clients installed -
`cweijan.vscode-postgresql-client2` and `mtxr.sqltools`. They work, but
`ms-ossdata.vscode-pgsql` supersedes both for this workflow and is the only one
with Azure Flexible Server management and Entra auth built in. Running several
SQL clients at once mostly produces duplicate context menus.

### Not needed

- Any "DMS" or "Database Migration Service" extension - **there isn't one**.
  The migration is driven by `az postgres flexible-server migration` from the
  CLI, which is what `infra/02_run_migration.ps1` wraps.
- `ms-mssql.mssql` - that is SQL Server, a different engine.

---

## Minimum viable setup

```
code --install-extension ms-ossdata.vscode-pgsql
code --install-extension databricks.databricks
code --install-extension ms-vscode.powershell

az extension add --name rdbms-connect
pip install -r requirements.txt
```

Everything else in this repo runs from the terminal.
