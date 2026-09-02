# 30-minute demo script

Everything in the setup guide is done **before** the room fills up. The
migration is already sitting at `WaitingForCutoverTrigger`, the Lakeflow
pipeline is running, and the synced tables are online. You are demonstrating a
migration in flight, not provisioning infrastructure on stage.

Four terminals open and sized before you start:

| # | Contents |
| --- | --- |
| 1 | `psql` on the source (`retail_onprem`) |
| 2 | `workload.py` - not yet running |
| 3 | `verify_parity.py --watch 10` - not yet running |
| 4 | `psql` on Lakebase |

Plus a browser on the Databricks Catalog view of `retail_demo.onprem`.

---

## 0:00-0:03 - The problem

Terminal 1:

```sql
SELECT count(*) FROM retail.orders;
SELECT order_status, count(*) FROM retail.orders GROUP BY order_status;
```

> "This is order capture for a retailer. It runs in their data centre on
> PostgreSQL. It works fine. The problem isn't the database - it's that
> everything downstream of it is a nightly file, analytics is a day behind, and
> nobody will let a data scientist near it."

Show `docs/01-architecture.md`'s diagram. Name the two phases. **Say up front
that the workload never stops** - that is the promise you are making.

---

## 0:03-0:08 - Start the load, show CDC

Terminal 2:

```powershell
python src\workload.py --tps 10 --marker LIVE-DEMO
```

Let the live stats scroll. Point at the operation mix - orders, payments,
fulfilment, and a trickle of cancellations that issue real `DELETE`s.

Terminal 1:

```sql
SELECT slot_name, active, confirmed_flush_lsn FROM pg_replication_slots;
```

> "Two consumers on this one slot. The migration service is reading it to build
> the Azure copy. Lakeflow Connect is reading it to build the lakehouse. One
> CDC mechanism, two jobs."

---

## 0:08-0:14 - Phase 1: the lift

Show migration status:

```powershell
.\infra\02_run_migration.ps1 -ResourceGroup rg-lakebase-migration-demo `
    -TargetName <server> -SourceHost <ip> -MigrationName <name> -Watch
```

Substate is `WaitingForCutoverTrigger`.

> "The full load finished before you sat down. Since then it has been tailing
> the log. It is caught up, and it will stay caught up until I tell it to stop.
> Cutover is a decision, not an outage."

Terminal 3:

```powershell
python src\verify_parity.py --watch 10
```

Both tiers climbing together, checksums matching. Let it run a full cycle so
people see the numbers move.

> "That's the whole anxiety of a migration, answered by a row count and a
> checksum, while the source is still taking writes."

**Do not cut over.** Say why: with an online migration you would rehearse this
for days and cut over at 3am. The demo point is that you *can*.

---

## 0:14-0:22 - Phase 2: into the lakehouse

Browser, Databricks Catalog -> `retail_demo.onprem`:

```sql
SELECT count(*) FROM retail_demo.onprem.orders;
DESCRIBE HISTORY retail_demo.onprem.orders;
```

New versions appearing every few seconds.

> "Same rows, now Delta, now governed by Unity Catalog. Lineage, permissions,
> and it joins to everything else in the lakehouse."

Run one query the on-prem database could never have served - orders joined to
whatever else lives in the workspace, or just a window function over the full
history.

**The honest bit** (worth 30 seconds, it buys credibility):

> "If I'd done this with a timestamp-based nightly extract, the order lines
> from those cancellations would still be sitting there. A `MERGE` on a
> timestamp cannot see a row that no longer exists. That's why this is reading
> the replication log."

---

## 0:22-0:28 - Lakebase: serving it back

> "So the data is in the lakehouse. But the application still needs a
> hundred-microsecond lookup on a primary key. A warehouse won't do that."

Terminal 4, from `databricks/03_serve_queries.sql`:

```sql
SELECT version();
\dt onprem.*
```

> "That's Postgres 17. Standard wire protocol. The application's driver does
> not know anything changed."

Point read with `\timing on`:

```sql
SELECT o.order_id, o.order_status, o.order_total, c.full_name
FROM onprem.orders_synced o JOIN onprem.customer_synced c USING (customer_id)
WHERE o.order_id = 42;
```

Then the payoff - rows written on stage, minutes ago:

```sql
SELECT count(*) FROM onprem.orders_synced WHERE order_ref LIKE 'LIVE-DEMO%';
```

Run it twice, thirty seconds apart. The number goes up.

> "On-premises Postgres, to Azure, to Delta, to Lakebase, while the application
> kept writing the whole time."

Finish with section 8 - the app-owned table joined to synced data in one
transaction:

> "This is what a read replica can't give you. My application's own state and
> the lakehouse's output, in the same database, in the same transaction."

---

## 0:28-0:30 - Close

Three things to leave them with:

1. **Online migration means cutover is a decision.** The load never stopped.
2. **One CDC stream, two destinations.** The lift and the modernisation are the
   same mechanism.
3. **Lakebase closes the loop.** Analytics data goes back to the application as
   Postgres, not as an export.

Then say the constraint out loud, because someone will ask anyway:

> "There is no direct pipe from a Postgres server into Lakebase - it can't be a
> logical replication subscriber, and DMS has no Databricks target. Data lands
> in Unity Catalog first and gets synced out. That's not a workaround, it's the
> design: the lakehouse stays the system of record."

---

## If something breaks

| Symptom | Do this |
| --- | --- |
| Migration shows `Failed` | Show `verify_parity.py` on the already-loaded data; the story is CDC, not the control plane |
| Lakeflow pipeline red | Run `01b_jdbc_ingest.py`, say "batch instead of streaming, same outcome" |
| Synced table lagging | Switch to the freshness query (section 6) and talk about sync modes |
| Nothing works | Walk `docs/01-architecture.md` and `03_serve_queries.sql` as a code read. It still lands. |

## Afterwards

```powershell
# Ctrl+C the workload generator first
.\infra\teardown.ps1
```

Then `databricks/99_teardown.sql` and delete the Lakebase project. Check the
source for orphaned replication slots.
