-- ============================================================================
-- Phase 0 - "On-premises" retail OLTP schema (PostgreSQL 17)
--
-- Deliberately shaped for a migration demo:
--   * every table has a single-column primary key, so logical replication /
--     CDC has a stable REPLICA IDENTITY without extra configuration
--   * updated_at columns give Lakeflow Connect and the parity checker an
--     obvious high-water mark
--   * no PostGIS / exotic extensions, so it lifts cleanly to Azure Database
--     for PostgreSQL Flexible Server and stays Lakebase-compatible
-- ============================================================================

CREATE SCHEMA IF NOT EXISTS retail;
SET search_path TO retail, public;

DROP TABLE IF EXISTS retail.payment CASCADE;
DROP TABLE IF EXISTS retail.inventory_movement CASCADE;
DROP TABLE IF EXISTS retail.order_item CASCADE;
DROP TABLE IF EXISTS retail.orders CASCADE;
DROP TABLE IF EXISTS retail.product CASCADE;
DROP TABLE IF EXISTS retail.customer CASCADE;

-- ---------------------------------------------------------------- dimensions
CREATE TABLE retail.customer (
    customer_id   BIGSERIAL PRIMARY KEY,
    email         TEXT        NOT NULL UNIQUE,
    full_name     TEXT        NOT NULL,
    country       TEXT        NOT NULL,
    city          TEXT        NOT NULL,
    loyalty_tier  TEXT        NOT NULL DEFAULT 'bronze'
                  CHECK (loyalty_tier IN ('bronze','silver','gold','platinum')),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE retail.product (
    product_id    BIGSERIAL PRIMARY KEY,
    sku           TEXT        NOT NULL UNIQUE,
    name          TEXT        NOT NULL,
    category      TEXT        NOT NULL,
    unit_price    NUMERIC(10,2) NOT NULL CHECK (unit_price >= 0),
    stock_qty     INTEGER     NOT NULL DEFAULT 0 CHECK (stock_qty >= 0),
    is_active     BOOLEAN     NOT NULL DEFAULT true,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------- facts
CREATE TABLE retail.orders (
    order_id      BIGSERIAL PRIMARY KEY,
    customer_id   BIGINT      NOT NULL REFERENCES retail.customer(customer_id),
    order_status  TEXT        NOT NULL DEFAULT 'placed'
                  CHECK (order_status IN ('placed','picking','shipped','delivered','cancelled')),
    channel       TEXT        NOT NULL CHECK (channel IN ('web','mobile','store','partner')),
    order_total   NUMERIC(12,2) NOT NULL DEFAULT 0,
    currency      TEXT        NOT NULL DEFAULT 'EUR',
    -- free-text tag the workload generator stamps with --marker, so a specific
    -- transaction can be followed all the way through to Lakebase on stage
    order_ref     TEXT,
    placed_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE retail.order_item (
    order_item_id BIGSERIAL PRIMARY KEY,
    order_id      BIGINT      NOT NULL REFERENCES retail.orders(order_id) ON DELETE CASCADE,
    product_id    BIGINT      NOT NULL REFERENCES retail.product(product_id),
    quantity      INTEGER     NOT NULL CHECK (quantity > 0),
    unit_price    NUMERIC(10,2) NOT NULL,
    line_total    NUMERIC(12,2) GENERATED ALWAYS AS (quantity * unit_price) STORED,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE retail.payment (
    payment_id    BIGSERIAL PRIMARY KEY,
    order_id      BIGINT      NOT NULL REFERENCES retail.orders(order_id) ON DELETE CASCADE,
    method        TEXT        NOT NULL CHECK (method IN ('card','ideal','paypal','invoice')),
    amount        NUMERIC(12,2) NOT NULL,
    payment_status TEXT       NOT NULL DEFAULT 'pending'
                  CHECK (payment_status IN ('pending','authorized','captured','failed','refunded')),
    authorized_at TIMESTAMPTZ,
    captured_at   TIMESTAMPTZ,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE retail.inventory_movement (
    movement_id   BIGSERIAL PRIMARY KEY,
    product_id    BIGINT      NOT NULL REFERENCES retail.product(product_id),
    order_id      BIGINT      REFERENCES retail.orders(order_id) ON DELETE SET NULL,
    delta_qty     INTEGER     NOT NULL,
    reason        TEXT        NOT NULL CHECK (reason IN ('sale','restock','adjustment','return')),
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- --------------------------------------------------------------------- indexes
CREATE INDEX idx_orders_customer   ON retail.orders(customer_id);
CREATE INDEX idx_orders_status     ON retail.orders(order_status);
CREATE INDEX idx_orders_updated    ON retail.orders(updated_at);
CREATE INDEX idx_order_item_order  ON retail.order_item(order_id);
CREATE INDEX idx_payment_order     ON retail.payment(order_id);
CREATE INDEX idx_payment_status    ON retail.payment(payment_status);
CREATE INDEX idx_invmove_product   ON retail.inventory_movement(product_id);

-- ------------------------------------------------------- updated_at triggers
CREATE OR REPLACE FUNCTION retail.touch_updated_at() RETURNS trigger AS $$
BEGIN
    NEW.updated_at := now();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER trg_customer_touch BEFORE UPDATE ON retail.customer
    FOR EACH ROW EXECUTE FUNCTION retail.touch_updated_at();
CREATE TRIGGER trg_product_touch  BEFORE UPDATE ON retail.product
    FOR EACH ROW EXECUTE FUNCTION retail.touch_updated_at();
CREATE TRIGGER trg_orders_touch   BEFORE UPDATE ON retail.orders
    FOR EACH ROW EXECUTE FUNCTION retail.touch_updated_at();
CREATE TRIGGER trg_payment_touch  BEFORE UPDATE ON retail.payment
    FOR EACH ROW EXECUTE FUNCTION retail.touch_updated_at();
