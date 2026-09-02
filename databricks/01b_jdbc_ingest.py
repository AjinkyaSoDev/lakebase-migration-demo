# Databricks notebook source
# MAGIC %md
# MAGIC # Phase 2a fallback - JDBC ingest from Azure PostgreSQL into Delta
# MAGIC
# MAGIC Use this when the Lakeflow Connect managed connector is not available.
# MAGIC Same destination tables, so Phase 2b (synced tables) is unaffected.
# MAGIC
# MAGIC `full` truncates and reloads. `incremental` merges rows whose
# MAGIC `updated_at` is newer than the high-water mark already in Delta.

# COMMAND ----------

dbutils.widgets.text("pg_host", "", "Azure PG host")
dbutils.widgets.text("pg_db", "retail_onprem", "Database")
dbutils.widgets.text("pg_user", "pgadmin", "User")
dbutils.widgets.text("secret_scope", "lakebase-demo", "Secret scope")
dbutils.widgets.text("secret_key", "azpg-password", "Secret key")
dbutils.widgets.text("catalog", "retail_demo", "UC catalog")
dbutils.widgets.text("schema", "onprem", "UC schema")
dbutils.widgets.text("source_schema", "retail", "Source PG schema")
dbutils.widgets.dropdown("mode", "incremental", ["full", "incremental"], "Mode")

PG_HOST = dbutils.widgets.get("pg_host")
PG_DB = dbutils.widgets.get("pg_db")
PG_USER = dbutils.widgets.get("pg_user")
CATALOG = dbutils.widgets.get("catalog")
SCHEMA = dbutils.widgets.get("schema")
SRC_SCHEMA = dbutils.widgets.get("source_schema")
MODE = dbutils.widgets.get("mode")

# Never inline the password. Create the scope once with:
#   databricks secrets create-scope lakebase-demo
#   databricks secrets put-secret lakebase-demo azpg-password
PG_PASSWORD = dbutils.secrets.get(
    dbutils.widgets.get("secret_scope"), dbutils.widgets.get("secret_key")
)

JDBC_URL = f"jdbc:postgresql://{PG_HOST}:5432/{PG_DB}?sslmode=require"
JDBC_PROPS = {"user": PG_USER, "password": PG_PASSWORD, "driver": "org.postgresql.Driver"}

# table -> (primary key, watermark column).
# order_item and inventory_movement are append-only and have no updated_at
# column, so they watermark on created_at instead.
TABLES = {
    "customer": ("customer_id", "updated_at"),
    "product": ("product_id", "updated_at"),
    "orders": ("order_id", "updated_at"),
    "order_item": ("order_item_id", "created_at"),
    "payment": ("payment_id", "updated_at"),
    "inventory_movement": ("movement_id", "created_at"),
}

# COMMAND ----------

spark.sql(f"CREATE CATALOG IF NOT EXISTS {CATALOG}")
spark.sql(f"CREATE SCHEMA IF NOT EXISTS {CATALOG}.{SCHEMA}")

# COMMAND ----------

from delta.tables import DeltaTable


def high_water_mark(fqn, wm_col):
    """Latest watermark value already landed, or None if the table is new."""
    if not spark.catalog.tableExists(fqn):
        return None
    row = spark.sql(f"SELECT max({wm_col}) AS hwm FROM {fqn}").first()
    return row["hwm"] if row and row["hwm"] is not None else None


def ingest(table, pk, wm_col):
    fqn = f"{CATALOG}.{SCHEMA}.{table}"
    hwm = None if MODE == "full" else high_water_mark(fqn, wm_col)

    if hwm is None:
        query = f"(SELECT * FROM {SRC_SCHEMA}.{table}) AS t"
    else:
        # inclusive so we never miss rows written inside the same second
        query = f"(SELECT * FROM {SRC_SCHEMA}.{table} WHERE {wm_col} >= '{hwm}') AS t"

    src = spark.read.jdbc(url=JDBC_URL, table=query, properties=JDBC_PROPS)
    n = src.count()

    if MODE == "full" or not spark.catalog.tableExists(fqn):
        src.write.mode("overwrite").option("delta.enableChangeDataFeed", "true") \
            .saveAsTable(fqn)
        print(f"{table:<20} full load   {n:>8} rows")
        return

    if n == 0:
        print(f"{table:<20} no changes")
        return

    DeltaTable.forName(spark, fqn).alias("t") \
        .merge(src.alias("s"), f"t.{pk} = s.{pk}") \
        .whenMatchedUpdateAll() \
        .whenNotMatchedInsertAll() \
        .execute()
    print(f"{table:<20} merged      {n:>8} rows")


for t, (pk, wm) in TABLES.items():
    ingest(t, pk, wm)

# COMMAND ----------

# MAGIC %md
# MAGIC ### Caveat worth saying out loud
# MAGIC A timestamp-based MERGE cannot see hard `DELETE`s - the workload
# MAGIC generator issues a few (cancelled orders) precisely so this shows up.
# MAGIC That gap is the argument for Lakeflow Connect / logical decoding, which
# MAGIC does capture deletes. Good demo moment, don't skip it.

# COMMAND ----------

display(spark.sql(f"""
    SELECT 'customer' AS t, count(*) AS rows FROM {CATALOG}.{SCHEMA}.customer
    UNION ALL SELECT 'product', count(*) FROM {CATALOG}.{SCHEMA}.product
    UNION ALL SELECT 'orders', count(*) FROM {CATALOG}.{SCHEMA}.orders
    UNION ALL SELECT 'order_item', count(*) FROM {CATALOG}.{SCHEMA}.order_item
    UNION ALL SELECT 'payment', count(*) FROM {CATALOG}.{SCHEMA}.payment
    UNION ALL SELECT 'inventory_movement', count(*) FROM {CATALOG}.{SCHEMA}.inventory_movement
    ORDER BY 1
"""))
