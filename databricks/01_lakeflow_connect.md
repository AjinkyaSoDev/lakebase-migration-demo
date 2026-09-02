# Phase 2a - ingest Azure PostgreSQL into Unity Catalog

Goal: get the migrated OLTP tables out of Azure PG Flexible Server and into
Delta tables in `retail_demo.onprem`, continuously, so that Phase 2b can serve
them from Lakebase.

Two options. Use **A** for the story, keep **B** in your pocket so the demo
never dies on stage.

---

## Option A - Lakeflow Connect managed Postgres connector (the story)

Lakeflow Connect reads the Postgres logical replication slot for you, so the
same CDC mechanism that fed the migration now feeds the lakehouse. No Spark
code to maintain.

### 1. Prepare the source (once)

Against the **Azure PG Flexible Server**, not the laptop:

```sql
-- server parameter wal_level must already be 'logical'
-- (01_provision_azure.ps1 sets this)
SHOW wal_level;

CREATE USER lakeflow WITH REPLICATION PASSWORD '<pw>';
GRANT USAGE ON SCHEMA retail TO lakeflow;
GRANT SELECT ON ALL TABLES IN SCHEMA retail TO lakeflow;
ALTER DEFAULT PRIVILEGES IN SCHEMA retail GRANT SELECT ON TABLES TO lakeflow;

-- a dedicated publication + slot for the lakehouse feed, separate from the
-- one the migration service is using
CREATE PUBLICATION lakeflow_pub FOR TABLES IN SCHEMA retail;
```

Allow the Databricks serverless egress IPs through the PG firewall (Azure
portal -> Networking), or use a private endpoint if the workspace has one.

### 2. Create the connection

```bash
databricks connections create --json '{
  "name": "azure_pg_retail",
  "connection_type": "POSTGRESQL",
  "options": {
    "host": "<server>.postgres.database.azure.com",
    "port": "5432",
    "user": "lakeflow",
    "password": "<pw>",
    "database": "retail_onprem",
    "sslmode": "require"
  }
}'
```

### 3. Create the ingestion pipeline

Workspace UI: **Data Ingestion -> PostgreSQL -> azure_pg_retail**, then

- destination catalog `retail_demo`, schema `onprem`
- source schema `retail`
- select the six tables: `customer`, `product`, `orders`, `order_item`,
  `payment`, `inventory_movement`
- schedule **Continuous**

The pipeline creates a staging catalog for the CDC snapshots and lands
SCD type 1 Delta tables at `retail_demo.onprem.<table>`.

### 4. Verify

```sql
SELECT count(*) FROM retail_demo.onprem.orders;
DESCRIBE HISTORY retail_demo.onprem.orders;   -- should tick over while the
                                              -- workload generator runs
```

---

## Option B - JDBC batch ingest (the safety net)

Import `01b_jdbc_ingest.py` as a notebook and run it on a serverless or
classic cluster. It does an incremental `MERGE` keyed on `updated_at`, so it
is idempotent and you can just re-run it between demo beats.

```
%run ./01b_jdbc_ingest  $mode=incremental
```

It needs the same firewall opening as Option A, plus the
`org.postgresql:postgresql:42.7.4` Maven library on the cluster (serverless
already has it).

---

## Which one to show

If the connector is already provisioned and healthy, demo A and mention that
it is the same logical-decoding slot the migration used - one CDC concept, two
consumers. If anything is red, run B, say "same outcome, batch instead of
streaming", and move on to Lakebase. The audience cares about the Lakebase
payoff, not the plumbing.
