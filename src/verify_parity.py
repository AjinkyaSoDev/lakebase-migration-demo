"""Prove the migration actually worked.

Compares row counts and a content checksum for every table across the tiers you
have configured:

  1. on-prem     local PostgreSQL 17          (always)
  2. azure-pg    Azure DB for PostgreSQL Flex (if AZPG_HOST is set)  <- Azure DMS target
  3. lakebase    Databricks Lakebase Postgres (if LAKEBASE_HOST set) <- synced table

  python src/verify_parity.py
  python src/verify_parity.py --watch 10     # re-check every 10s while the
                                             # workload runs, to show lag closing

The checksum is an order-independent XOR-free sum of per-row md5 hashes, so it
is stable regardless of physical row order and cheap enough to run mid-demo.
"""
import argparse
import sys
import time

import psycopg2

import config

# Columns deliberately excluded from the checksum: they are rewritten by
# triggers/defaults on insert into a target and would never match.
VOLATILE = {"updated_at"}

KEYS = {
    "customer": "customer_id",
    "product": "product_id",
    "orders": "order_id",
    "order_item": "order_item_id",
    "payment": "payment_id",
    "inventory_movement": "movement_id",
}


def columns(conn, table, schema):
    with conn.cursor() as cur:
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = %s AND table_name = %s ORDER BY ordinal_position",
            (schema, table))
        return [r[0] for r in cur.fetchall() if r[0] not in VOLATILE]


def snapshot(conn, schema):
    """Returns {table: (row_count, checksum)}."""
    out = {}
    for t in config.TABLES:
        cols = columns(conn, t, schema)
        if not cols:
            out[t] = (None, None)
            continue
        expr = " || '|' || ".join(f"coalesce({c}::text,'~')" for c in cols)
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT count(*), coalesce(sum(('x'||substr(md5({expr}),1,8))::bit(32)::bigint),0) "
                f"FROM {schema}.{t}")
            n, chk = cur.fetchone()
        out[t] = (n, chk)
    return out


def try_connect(label, dsn):
    if not dsn.get("host"):
        return None
    try:
        c = psycopg2.connect(connect_timeout=10, **dsn)
        print(f"  connected: {label} ({dsn['host']})")
        return c
    except psycopg2.Error as e:
        print(f"  skipped {label}: {str(e).strip()[:110]}")
        return None


def report(snaps):
    labels = list(snaps.keys())
    base = labels[0]
    w = 20
    print()
    print("  table".ljust(w) + "".join(l.rjust(16) for l in labels) + "   status")
    print("  " + "-" * (w + 16 * len(labels) + 11))
    all_ok = True
    for t in config.TABLES:
        row = f"  {t}".ljust(w)
        for l in labels:
            n, _ = snaps[l].get(t, (None, None))
            row += (f"{n:,}" if n is not None else "-").rjust(16)
        b_n, b_c = snaps[base].get(t, (None, None))
        status = "  (source)"
        for l in labels[1:]:
            n, c = snaps[l].get(t, (None, None))
            if n is None:
                status = "  n/a"
            elif (n, c) == (b_n, b_c):
                status = "  MATCH"
            elif n == b_n:
                status = "  count ok / checksum differs"
                all_ok = False
            else:
                status = f"  DRIFT {n - b_n:+,} rows"
                all_ok = False
        print(row + status)
    return all_ok


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--watch", type=int, default=0, help="re-check every N seconds")
    args = ap.parse_args()

    print("Connecting to tiers")
    conns = {}
    c = try_connect("on-prem", config.ONPREM)
    if not c:
        sys.exit("Cannot reach the on-prem source - nothing to compare.")
    conns["on-prem"] = (c, "retail")

    c = try_connect("azure-pg", config.AZURE_PG)
    if c:
        conns["azure-pg"] = (c, "retail")
    c = try_connect("lakebase", config.LAKEBASE)
    if c:
        conns["lakebase"] = (c, config.UC_SCHEMA)

    if len(conns) == 1:
        print("\n  Only the source is reachable. Set AZPG_* / LAKEBASE_* in .env")
        print("  to compare against the Azure DMS target and Lakebase.")

    try:
        while True:
            snaps = {label: snapshot(conn, schema) for label, (conn, schema) in conns.items()}
            ok = report(snaps)
            if not args.watch:
                return 0 if ok else 1
            print(f"\n  next check in {args.watch}s (Ctrl+C to stop)\n")
            time.sleep(args.watch)
    except KeyboardInterrupt:
        print("\nstopped")
        return 0
    finally:
        for conn, _ in conns.values():
            conn.close()


if __name__ == "__main__":
    sys.exit(main())
