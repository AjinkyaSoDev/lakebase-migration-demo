-- ============================================================================
-- Phase 0b - Enable logical decoding so both Azure DMS (online mode) and
-- Lakeflow Connect can read change events from this "on-prem" instance.
--
-- wal_level=logical requires a server restart and therefore cannot be set from
-- here; src/setup_onprem.py checks it and prints the exact ALTER SYSTEM +
-- restart commands if it is not already set. Run this file afterwards.
-- ============================================================================

-- A dedicated replication login. Azure DMS and Lakeflow Connect both need
-- REPLICATION plus SELECT on the published tables - nothing more.
--
-- The password below is a throwaway placeholder for the local demo cluster.
-- Change it before pointing anything reachable at this database:
--   ALTER ROLE replicator PASSWORD '<something-real>';
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'replicator') THEN
        CREATE ROLE replicator WITH LOGIN REPLICATION PASSWORD 'replicator_pw';
    END IF;
END $$;

GRANT CONNECT ON DATABASE retail_onprem TO replicator;
GRANT USAGE  ON SCHEMA retail TO replicator;
GRANT SELECT ON ALL TABLES IN SCHEMA retail TO replicator;
ALTER DEFAULT PRIVILEGES IN SCHEMA retail GRANT SELECT ON TABLES TO replicator;

-- One publication covering the whole retail schema. Azure DMS creates its own
-- slot against this; Lakeflow Connect can reuse the same publication.
DROP PUBLICATION IF EXISTS retail_pub;
CREATE PUBLICATION retail_pub FOR TABLES IN SCHEMA retail;

-- REPLICA IDENTITY DEFAULT uses the primary key, which every table has, so
-- UPDATE and DELETE events carry a usable key. Made explicit for clarity.
ALTER TABLE retail.customer            REPLICA IDENTITY DEFAULT;
ALTER TABLE retail.product             REPLICA IDENTITY DEFAULT;
ALTER TABLE retail.orders              REPLICA IDENTITY DEFAULT;
ALTER TABLE retail.order_item          REPLICA IDENTITY DEFAULT;
ALTER TABLE retail.payment             REPLICA IDENTITY DEFAULT;
ALTER TABLE retail.inventory_movement  REPLICA IDENTITY DEFAULT;

SELECT 'publication' AS object, pubname AS name FROM pg_publication WHERE pubname = 'retail_pub'
UNION ALL
SELECT 'published table', schemaname || '.' || tablename FROM pg_publication_tables WHERE pubname = 'retail_pub';
