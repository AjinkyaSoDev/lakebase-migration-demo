"""Build the "on-premises" retail estate on the local PostgreSQL 17 instance.

  python src/setup_onprem.py            # create DB, schema, seed, enable CDC
  python src/setup_onprem.py --drop     # start clean first

Checks wal_level and prints the exact remediation if logical decoding is off,
because Azure DMS online migration and Lakeflow Connect both depend on it.
"""
import argparse
import random
import sys

import psycopg2
from psycopg2 import sql
from faker import Faker

import config

fake = Faker("nl_NL")
Faker.seed(42)
random.seed(42)

CATEGORIES = ["Coffee", "Tea", "Bakery", "Dairy", "Snacks", "Household", "Beverages"]
COUNTRIES = [("NL", "Amsterdam"), ("NL", "Rotterdam"), ("NL", "Utrecht"),
             ("BE", "Antwerp"), ("BE", "Brussels"), ("DE", "Cologne"), ("DE", "Berlin")]
TIERS = ["bronze", "silver", "gold", "platinum"]

N_CUSTOMERS = 500
N_PRODUCTS = 120
N_SEED_ORDERS = 400


def connect(dsn, autocommit=False):
    c = psycopg2.connect(**dsn)
    c.autocommit = autocommit
    return c


def ensure_database(drop=False):
    admin = dict(config.ONPREM, dbname=config.ONPREM_ADMIN_DB)
    target = config.ONPREM["dbname"]
    # NB: do not use `with connect(...) as conn` here. psycopg2's connection
    # context manager opens a transaction on __enter__ even when autocommit is
    # True, and CREATE/DROP DATABASE cannot run inside a transaction block.
    conn = connect(admin, autocommit=True)
    try:
        cur = conn.cursor()
        if drop:
            cur.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname = %s AND pid <> pg_backend_pid()", (target,))
            cur.execute(sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(target)))
            print(f"  dropped database {target}")
        cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (target,))
        if cur.fetchone():
            print(f"  database {target} already exists")
        else:
            cur.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(target)))
            print(f"  created database {target}")
        cur.close()
    finally:
        conn.close()


def check_wal_level(conn):
    """Report every server setting the two CDC consumers depend on.

    Azure's migration service and Lakeflow Connect both use native logical
    replication with the built-in pgoutput plugin - no pglogical, no wal2json,
    no CREATE EXTENSION of any kind.
    """
    wanted = {
        "wal_level": "logical",
        "max_replication_slots": ">= 2 (one slot per consumer)",
        "max_wal_senders": ">= max_replication_slots",
        "wal_sender_timeout": "0 (Azure recommends disabling the 60s timeout)",
    }
    got = {}
    with conn.cursor() as cur:
        for p in wanted:
            cur.execute(f"SHOW {p}")
            got[p] = cur.fetchone()[0]
        cur.execute("SELECT rolsuper FROM pg_roles WHERE rolname = current_user")
        row = cur.fetchone()
        is_super = bool(row and row[0])

    for p, v in got.items():
        print(f"  {p:<24} = {v:<10} want {wanted[p]}")

    ok = got["wal_level"] == "logical"
    if not ok:
        print("\n  !! Logical decoding is OFF. The Azure migration service (Online mode)")
        print("     and Lakeflow Connect both need it. Run these, then RESTART:\n")
        print("       ALTER SYSTEM SET wal_level = 'logical';")
        print("       ALTER SYSTEM SET max_replication_slots = 10;")
        print("       ALTER SYSTEM SET max_wal_senders = 10;")
        print("       ALTER SYSTEM SET wal_sender_timeout = 0;")
        print("\n       Restart-Service postgresql-x64-17")
        print("       (or: pg_ctl -D <datadir> restart)\n")
    elif got["wal_sender_timeout"] not in ("0", "0ms"):
        print("\n  note: wal_sender_timeout is not 0. Azure recommends 0 for long")
        print("        online migrations so the sender is not dropped mid-copy:")
        print("          ALTER SYSTEM SET wal_sender_timeout = 0; SELECT pg_reload_conf();\n")
    if not is_super:
        print("  note: current user is not a superuser; CREATE ROLE ... REPLICATION may fail")
    return ok


def run_sql_file(conn, path):
    ddl = (config.ROOT / path).read_text(encoding="utf-8")
    with conn.cursor() as cur:
        cur.execute(ddl)
    conn.commit()
    print(f"  applied {path}")


def seed(conn):
    cur = conn.cursor()

    customers = []
    seen = set()
    while len(customers) < N_CUSTOMERS:
        email = fake.unique.email()
        if email in seen:
            continue
        seen.add(email)
        country, city = random.choice(COUNTRIES)
        customers.append((email, fake.name(), country, city,
                          random.choices(TIERS, weights=[50, 30, 15, 5])[0]))
    cur.executemany(
        "INSERT INTO retail.customer (email, full_name, country, city, loyalty_tier) "
        "VALUES (%s,%s,%s,%s,%s)", customers)

    products = []
    for i in range(N_PRODUCTS):
        cat = random.choice(CATEGORIES)
        products.append((f"SKU-{i + 1000}", f"{cat} {fake.word().title()}", cat,
                         round(random.uniform(1.5, 89.0), 2), random.randint(50, 5000)))
    cur.executemany(
        "INSERT INTO retail.product (sku, name, category, unit_price, stock_qty) "
        "VALUES (%s,%s,%s,%s,%s)", products)
    conn.commit()

    cur.execute("SELECT customer_id FROM retail.customer")
    cust_ids = [r[0] for r in cur.fetchall()]
    cur.execute("SELECT product_id, unit_price FROM retail.product")
    prods = cur.fetchall()

    for _ in range(N_SEED_ORDERS):
        cur.execute(
            "INSERT INTO retail.orders (customer_id, channel, order_status, currency) "
            "VALUES (%s,%s,%s,'EUR') RETURNING order_id",
            (random.choice(cust_ids),
             random.choices(["web", "mobile", "store", "partner"], weights=[45, 35, 15, 5])[0],
             random.choices(["placed", "picking", "shipped", "delivered"],
                            weights=[20, 15, 25, 40])[0]))
        oid = cur.fetchone()[0]
        total = 0
        for pid, price in random.sample(prods, random.randint(1, 5)):
            qty = random.randint(1, 4)
            cur.execute(
                "INSERT INTO retail.order_item (order_id, product_id, quantity, unit_price) "
                "VALUES (%s,%s,%s,%s)", (oid, pid, qty, price))
            cur.execute(
                "INSERT INTO retail.inventory_movement (product_id, order_id, delta_qty, reason) "
                "VALUES (%s,%s,%s,'sale')", (pid, oid, -qty))
            total += qty * float(price)
        cur.execute("UPDATE retail.orders SET order_total = %s WHERE order_id = %s",
                    (round(total, 2), oid))
        cur.execute(
            "INSERT INTO retail.payment (order_id, method, amount, payment_status, "
            "authorized_at, captured_at) VALUES (%s,%s,%s,%s, now(), now())",
            (oid, random.choice(["card", "ideal", "paypal", "invoice"]),
             round(total, 2), random.choices(["captured", "authorized", "failed"],
                                             weights=[80, 15, 5])[0]))
    conn.commit()

    for t in config.TABLES:
        cur.execute(f"SELECT count(*) FROM retail.{t}")
        print(f"    {t:<20} {cur.fetchone()[0]:>7,} rows")
    cur.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--drop", action="store_true", help="drop and recreate the database")
    args = ap.parse_args()

    print(f"[1/4] Database {config.ONPREM['dbname']} on "
          f"{config.ONPREM['host']}:{config.ONPREM['port']}")
    ensure_database(drop=args.drop)

    conn = connect(config.ONPREM)
    print("[2/4] Server settings")
    logical_ok = check_wal_level(conn)

    print("[3/4] Schema + seed")
    run_sql_file(conn, "sql/01_onprem_schema.sql")
    seed(conn)

    print("[4/4] Publication for CDC")
    try:
        run_sql_file(conn, "sql/02_enable_cdc.sql")
    except psycopg2.Error as e:
        conn.rollback()
        print(f"  skipped: {str(e).strip()}")
        print("  (fix the wal_level / privilege issue above, then re-run just this file)")
    conn.close()

    print("\nOn-prem estate ready.")
    if not logical_ok:
        print("Set wal_level=logical and restart before running the DMS online migration.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
