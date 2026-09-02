"""Simulate a live on-premises OLTP workload against retail_onprem.

This is what makes the migration demo convincing: while Azure DMS is running an
*online* migration and Lakeflow Connect is tailing CDC, real transactions keep
arriving, so you can show inserts, updates and deletes landing downstream.

  python src/workload.py                       # 5 tps until Ctrl+C
  python src/workload.py --tps 20 --duration 300
  python src/workload.py --marker DEMO-1       # tag rows so you can find them

Transaction mix (roughly what a retail OLTP system looks like):
  50%  new order  - INSERT across orders/order_item/inventory_movement/payment
  20%  payment state transition        (UPDATE)
  20%  order fulfilment progression    (UPDATE)
   5%  restock                         (INSERT + UPDATE)
   3%  customer detail change          (UPDATE)
   2%  order cancellation              (UPDATE + DELETE)
"""
import argparse
import random
import signal
import sys
import time
from datetime import datetime, timezone

import psycopg2

import config

RUNNING = True
STATS = {"new_order": 0, "payment": 0, "fulfilment": 0,
         "restock": 0, "customer": 0, "cancel": 0, "errors": 0}


def _stop(_sig, _frm):
    global RUNNING
    RUNNING = False
    print("\n  stopping after current transaction...")


class Workload:
    def __init__(self, conn, marker=None):
        self.conn = conn
        self.marker = marker
        cur = conn.cursor()
        cur.execute("SELECT customer_id FROM retail.customer")
        self.customers = [r[0] for r in cur.fetchall()]
        cur.execute("SELECT product_id, unit_price FROM retail.product WHERE is_active")
        self.products = cur.fetchall()
        cur.close()
        if not self.customers or not self.products:
            raise SystemExit("No seed data found - run src/setup_onprem.py first.")

    # ---------------------------------------------------------------- helpers
    def _pick_order(self, cur, statuses):
        cur.execute(
            "SELECT order_id FROM retail.orders WHERE order_status = ANY(%s) "
            "ORDER BY random() LIMIT 1", (statuses,))
        row = cur.fetchone()
        return row[0] if row else None

    # ----------------------------------------------------------- transactions
    def new_order(self, cur):
        channel = random.choices(["web", "mobile", "store", "partner"],
                                 weights=[45, 35, 15, 5])[0]
        cur.execute(
            "INSERT INTO retail.orders (customer_id, channel, order_status, currency) "
            "VALUES (%s,%s,'placed','EUR') RETURNING order_id",
            (random.choice(self.customers), channel))
        oid = cur.fetchone()[0]

        total = 0.0
        for pid, price in random.sample(self.products, random.randint(1, 5)):
            qty = random.randint(1, 4)
            cur.execute(
                "INSERT INTO retail.order_item (order_id, product_id, quantity, unit_price) "
                "VALUES (%s,%s,%s,%s)", (oid, pid, qty, price))
            cur.execute(
                "INSERT INTO retail.inventory_movement (product_id, order_id, delta_qty, reason) "
                "VALUES (%s,%s,%s,'sale')", (pid, oid, -qty))
            cur.execute(
                "UPDATE retail.product SET stock_qty = GREATEST(stock_qty - %s, 0) "
                "WHERE product_id = %s", (qty, pid))
            total += qty * float(price)

        cur.execute("UPDATE retail.orders SET order_total = %s WHERE order_id = %s",
                    (round(total, 2), oid))
        cur.execute(
            "INSERT INTO retail.payment (order_id, method, amount, payment_status) "
            "VALUES (%s,%s,%s,'pending')",
            (oid, random.choices(["card", "ideal", "paypal", "invoice"],
                                 weights=[50, 30, 15, 5])[0], round(total, 2)))
        if self.marker:
            cur.execute(
                "UPDATE retail.orders SET order_ref = %s WHERE order_id = %s",
                (f"{self.marker}-{oid}", oid))
        return "new_order"

    def payment_transition(self, cur):
        cur.execute(
            "SELECT payment_id, payment_status FROM retail.payment "
            "WHERE payment_status IN ('pending','authorized') ORDER BY random() LIMIT 1")
        row = cur.fetchone()
        if not row:
            return None
        pid, status = row
        if status == "pending":
            nxt = random.choices(["authorized", "failed"], weights=[92, 8])[0]
            cur.execute(
                "UPDATE retail.payment SET payment_status = %s, authorized_at = now() "
                "WHERE payment_id = %s", (nxt, pid))
        else:
            cur.execute(
                "UPDATE retail.payment SET payment_status = 'captured', captured_at = now() "
                "WHERE payment_id = %s", (pid,))
        return "payment"

    def fulfilment(self, cur):
        nxt = {"placed": "picking", "picking": "shipped", "shipped": "delivered"}
        oid = self._pick_order(cur, list(nxt.keys()))
        if not oid:
            return None
        status = self._status(cur, oid)
        cur.execute("UPDATE retail.orders SET order_status = %s WHERE order_id = %s",
                    (nxt[status], oid))
        return "fulfilment"

    def _status(self, cur, oid):
        cur.execute("SELECT order_status FROM retail.orders WHERE order_id = %s", (oid,))
        return cur.fetchone()[0]

    def restock(self, cur):
        pid, _ = random.choice(self.products)
        qty = random.randint(50, 500)
        cur.execute(
            "INSERT INTO retail.inventory_movement (product_id, delta_qty, reason) "
            "VALUES (%s,%s,'restock')", (pid, qty))
        cur.execute("UPDATE retail.product SET stock_qty = stock_qty + %s WHERE product_id = %s",
                    (qty, pid))
        return "restock"

    def customer_change(self, cur):
        cur.execute(
            "UPDATE retail.customer SET loyalty_tier = %s WHERE customer_id = %s",
            (random.choice(["bronze", "silver", "gold", "platinum"]),
             random.choice(self.customers)))
        return "customer"

    def cancel(self, cur):
        """Cancels an order and hard-deletes its items - exercises DELETE CDC events."""
        oid = self._pick_order(cur, ["placed", "picking"])
        if not oid:
            return None
        cur.execute("UPDATE retail.orders SET order_status = 'cancelled' WHERE order_id = %s", (oid,))
        cur.execute("UPDATE retail.payment SET payment_status = 'refunded' WHERE order_id = %s", (oid,))
        cur.execute("DELETE FROM retail.order_item WHERE order_id = %s", (oid,))
        return "cancel"

    def step(self):
        roll = random.random()
        cur = self.conn.cursor()
        try:
            if roll < 0.50:
                kind = self.new_order(cur)
            elif roll < 0.70:
                kind = self.payment_transition(cur)
            elif roll < 0.90:
                kind = self.fulfilment(cur)
            elif roll < 0.95:
                kind = self.restock(cur)
            elif roll < 0.98:
                kind = self.customer_change(cur)
            else:
                kind = self.cancel(cur)
            self.conn.commit()
            if kind:
                STATS[kind] += 1
        except psycopg2.Error as e:
            self.conn.rollback()
            STATS["errors"] += 1
            if STATS["errors"] <= 3:
                print(f"  tx error: {str(e).strip()[:120]}")
        finally:
            cur.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tps", type=float, default=5.0, help="target transactions per second")
    ap.add_argument("--duration", type=int, default=0, help="seconds to run (0 = until Ctrl+C)")
    ap.add_argument("--marker", help="tag new orders so you can find them downstream")
    args = ap.parse_args()

    signal.signal(signal.SIGINT, _stop)

    conn = psycopg2.connect(**config.ONPREM)
    wl = Workload(conn, marker=args.marker)

    delay = 1.0 / args.tps if args.tps > 0 else 0
    started = time.time()
    last_report = started
    print(f"Workload running at ~{args.tps} tps against "
          f"{config.ONPREM['host']}:{config.ONPREM['port']}/{config.ONPREM['dbname']}")
    if args.marker:
        print(f"  marker: {args.marker}")
    print("  Ctrl+C to stop\n")

    while RUNNING:
        if args.duration and (time.time() - started) >= args.duration:
            break
        wl.step()
        now = time.time()
        if now - last_report >= 5:
            elapsed = now - started
            done = sum(v for k, v in STATS.items() if k != "errors")
            print(f"  [{datetime.now(timezone.utc):%H:%M:%S}] {done:>6,} tx "
                  f"({done / elapsed:5.1f} tps)  " +
                  "  ".join(f"{k}={v}" for k, v in STATS.items() if v))
            last_report = now
        if delay:
            time.sleep(delay)

    conn.close()
    elapsed = time.time() - started
    done = sum(v for k, v in STATS.items() if k != "errors")
    print(f"\nStopped after {elapsed:,.0f}s - {done:,} transactions "
          f"({done / max(elapsed, 1):.1f} tps average)")
    for k, v in STATS.items():
        print(f"  {k:<12} {v:>7,}")


if __name__ == "__main__":
    main()
