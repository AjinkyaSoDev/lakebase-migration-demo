-- Clean up the Databricks side after the demo.
-- The Azure side is handled by infra/teardown.ps1.

-- 1. Drop the synced tables (this also removes the Postgres tables and stops
--    the sync pipelines - do NOT drop the Postgres tables directly).
DROP TABLE IF EXISTS retail_demo.onprem.customer_synced;
DROP TABLE IF EXISTS retail_demo.onprem.product_synced;
DROP TABLE IF EXISTS retail_demo.onprem.orders_synced;
DROP TABLE IF EXISTS retail_demo.onprem.order_item_synced;
DROP TABLE IF EXISTS retail_demo.onprem.payment_synced;
DROP TABLE IF EXISTS retail_demo.onprem.inventory_movement_synced;

-- 2. Drop the ingested Delta tables.
DROP SCHEMA IF EXISTS retail_demo.onprem CASCADE;

-- 3. Then, outside SQL:
--    - delete the Lakeflow Connect ingestion pipeline and its staging catalog
--    - delete the connection:   databricks connections delete azure_pg_retail
--    - delete the Lakebase project (Compute -> Lakebase) so it stops billing
--    - drop the app schema inside Lakebase if you created it:
--        DROP SCHEMA app CASCADE;
