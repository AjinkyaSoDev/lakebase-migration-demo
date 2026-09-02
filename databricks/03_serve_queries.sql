-- Phase 3 - prove the point: serve the migrated data from Lakebase Postgres.
--
-- Run these with psql against the Lakebase endpoint, NOT against a SQL
-- warehouse. That distinction is the whole demo: same standard Postgres wire
-- protocol the on-prem app used, now backed by the lakehouse.
--
--   $env:PGPASSWORD = "<oauth-token-or-password>"
--   psql "host=<instance>.database.cloud.databricks.com port=5432 \
--         dbname=retail user=<you> sslmode=require"

\timing on

-- 1. It really is Postgres --------------------------------------------------
SELECT version();
\dt onprem.*

-- 2. Row counts match the source (compare with verify_parity.py) -----------
SELECT 'customer'           AS table_name, count(*) FROM onprem.customer_synced
UNION ALL SELECT 'product',            count(*) FROM onprem.product_synced
UNION ALL SELECT 'orders',             count(*) FROM onprem.orders_synced
UNION ALL SELECT 'order_item',         count(*) FROM onprem.order_item_synced
UNION ALL SELECT 'payment',            count(*) FROM onprem.payment_synced
UNION ALL SELECT 'inventory_movement', count(*) FROM onprem.inventory_movement_synced
ORDER BY 1;

-- 3. The lookup the old on-prem app used to do ------------------------------
--    Single-row point read on the primary key. This is what Lakebase is for;
--    the same query on a SQL warehouse would be seconds, not milliseconds.
SELECT o.order_id, o.order_ref, o.order_status, o.order_total, o.placed_at,
       c.full_name, c.email
FROM   onprem.orders_synced o
JOIN   onprem.customer_synced c USING (customer_id)
WHERE  o.order_id = 42;

-- 4. Rows written by the live workload generator ---------------------------
--    Run workload.py with --marker LIVE-DEMO, then re-run this every few
--    seconds. In CONTINUOUS sync mode new rows appear within ~15-30s.
SELECT order_id, order_ref, order_status, order_total, placed_at
FROM   onprem.orders_synced
WHERE  order_ref LIKE 'LIVE-DEMO%'
ORDER BY placed_at DESC
LIMIT 20;

SELECT count(*) AS live_rows_landed
FROM   onprem.orders_synced
WHERE  order_ref LIKE 'LIVE-DEMO%';

-- 5. Operational aggregate an app dashboard would run ----------------------
SELECT order_status, count(*) AS orders, round(sum(order_total), 2) AS revenue
FROM   onprem.orders_synced
GROUP  BY order_status
ORDER  BY orders DESC;

-- 6. Freshness - how far behind the source are we? -------------------------
SELECT max(updated_at)                              AS newest_row,
       now() - max(updated_at)                      AS sync_lag
FROM   onprem.orders_synced;

-- 7. Synced tables are read-only -------------------------------------------
--    Uncomment to show the guard rail. It fails, and that is correct:
--    the lakehouse is the system of record for these tables.
-- UPDATE onprem.orders_synced SET order_status = 'hacked' WHERE order_id = 42;

-- 8. But the database itself is fully transactional ------------------------
--    Application state lives alongside the synced data in the same database,
--    in the same transaction. This is why it beats a read replica.
CREATE SCHEMA IF NOT EXISTS app;
CREATE TABLE IF NOT EXISTS app.order_review (
    order_id    BIGINT PRIMARY KEY,
    reviewed_by TEXT        NOT NULL,
    note        TEXT,
    reviewed_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

BEGIN;
INSERT INTO app.order_review (order_id, reviewed_by, note)
VALUES (42, 'demo', 'flagged during migration walkthrough')
ON CONFLICT (order_id) DO UPDATE SET note = EXCLUDED.note, reviewed_at = now();
COMMIT;

SELECT o.order_id, o.order_status, o.order_total, r.reviewed_by, r.note
FROM   onprem.orders_synced o
JOIN   app.order_review     r USING (order_id);

\timing off
