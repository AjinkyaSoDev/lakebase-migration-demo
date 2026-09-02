"""Phase 2b - create the Lakebase synced tables.

This is the step that actually puts on-prem data into Postgres inside
Databricks. Flow:

    Azure PG (DMS target)  --Lakeflow Connect-->  Delta in Unity Catalog
                                                        |
                                            create_synced_table (this file)
                                                        v
                                            Lakebase Postgres  <-- apps/psql

Synced tables are the supported route. Lakebase cannot be a Postgres logical
replication subscriber, so you cannot point DMS or pglogical straight at it -
data has to land in Unity Catalog first.

Usage:
    pip install databricks-sdk
    python databricks/02_create_synced_tables.py --mode CONTINUOUS
    python databricks/02_create_synced_tables.py --status
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import config  # noqa: E402

from databricks.sdk import WorkspaceClient  # noqa: E402
from databricks.sdk.service.postgres import (  # noqa: E402
    SyncedTable,
    SyncedTableSyncedTableSpec,
    SyncedTableSyncedTableSpecSyncedTableSchedulingPolicy as Policy,
)

# Primary keys must match the on-prem schema. Rows with a NULL pk are dropped
# by the sync, which is why every source table has a surrogate key.
PRIMARY_KEYS = {
    "customer": ["customer_id"],
    "product": ["product_id"],
    "orders": ["order_id"],
    "order_item": ["order_item_id"],
    "payment": ["payment_id"],
    "inventory_movement": ["movement_id"],
}


def enable_cdf(w, table_fqn):
    """Triggered/Continuous sync needs a change data feed on the Delta source."""
    warehouse = next(iter(w.warehouses.list()), None)
    if warehouse is None:
        print("  ! no SQL warehouse available - enable CDF manually:")
        print(f"    ALTER TABLE {table_fqn} SET TBLPROPERTIES (delta.enableChangeDataFeed = true)")
        return
    w.statement_execution.execute_statement(
        warehouse_id=warehouse.id,
        statement=f"ALTER TABLE {table_fqn} SET TBLPROPERTIES (delta.enableChangeDataFeed = true)",
        wait_timeout="30s",
    )
    print(f"  change data feed enabled on {table_fqn}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", default="retail-lakebase", help="Lakebase project name")
    ap.add_argument("--branch", default="production")
    ap.add_argument("--pg-database", default="retail", help="database name inside Lakebase")
    ap.add_argument("--mode", default="CONTINUOUS",
                    choices=["SNAPSHOT", "TRIGGERED", "CONTINUOUS"],
                    help="CONTINUOUS gives ~15s lag - best for a live demo")
    ap.add_argument("--tables", nargs="*", default=config.TABLES)
    ap.add_argument("--status", action="store_true", help="just report current sync state")
    args = ap.parse_args()

    w = WorkspaceClient(host=config.DATABRICKS_HOST, token=config.DATABRICKS_TOKEN)
    branch = f"projects/{args.project}/branches/{args.branch}"

    if args.status:
        for t in args.tables:
            fqn = f"{config.UC_CATALOG}.{config.UC_SCHEMA}.{t}_synced"
            try:
                st = w.postgres.get_synced_table(fqn)
                detail = getattr(st, "data_synchronization_status", None)
                print(f"  {fqn:<50} {getattr(detail, 'detailed_state', 'unknown')}")
            except Exception as e:
                print(f"  {fqn:<50} not found ({str(e)[:60]})")
        return 0

    print(f"Creating synced tables in {args.mode} mode")
    print(f"  branch          : {branch}")
    print(f"  lakebase db     : {args.pg_database}")
    print(f"  postgres schema : {config.UC_SCHEMA}\n")

    for t in args.tables:
        source = f"{config.UC_CATALOG}.{config.UC_SCHEMA}.{t}"
        target = f"{config.UC_CATALOG}.{config.UC_SCHEMA}.{t}_synced"
        pk = PRIMARY_KEYS.get(t)
        if not pk:
            print(f"  skip {t}: no primary key mapping")
            continue

        print(f"  {t}")
        if args.mode in ("TRIGGERED", "CONTINUOUS"):
            try:
                enable_cdf(w, source)
            except Exception as e:
                print(f"    ! could not enable CDF: {str(e)[:100]}")

        try:
            st = w.postgres.create_synced_table(
                synced_table=SyncedTable(spec=SyncedTableSyncedTableSpec(
                    source_table_full_name=source,
                    branch=branch,
                    primary_key_columns=pk,
                    scheduling_policy=Policy[args.mode],
                    postgres_database=args.pg_database,
                    create_database_objects_if_missing=True,
                )),
                synced_table_id=target,
            ).wait()
            print(f"    created -> postgres {config.UC_SCHEMA}.{t}_synced  ({st.name})")
        except Exception as e:
            msg = str(e)
            if "already exists" in msg.lower():
                print("    already exists")
            else:
                print(f"    FAILED: {msg[:160]}")

    print("\nQuery them from Lakebase with:")
    print(f"  psql \"host=<lakebase-host> dbname={args.pg_database} user=<you> sslmode=require\"")
    print(f"  SELECT count(*) FROM {config.UC_SCHEMA}.orders_synced;")
    return 0


if __name__ == "__main__":
    sys.exit(main())
