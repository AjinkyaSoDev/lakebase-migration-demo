# Architecture

## The scenario

A retailer runs order capture on PostgreSQL in their own data centre. It works,
but it is a bottleneck: analytics is a nightly dump, the app team is scared to
touch the schema, and nobody can put a model anywhere near live orders.

The demo moves that workload to Databricks in two phases, with the source
system under continuous write load the whole time.

## The tiers

```
  Tier 1                Tier 2                  Tier 3               Tier 4
 on-premises          Azure Database         Unity Catalog        Lakebase
 PostgreSQL 17  --->  for PostgreSQL   --->     Delta      --->   Postgres
                       Flexible Server        retail_demo         (synced)
     ^                                          .onprem               ^
     |                                                                |
 workload.py                                                    apps / psql
 (continuous OLTP)                                              point lookups

     |<-- Phase 1: lift ------>|<---------- Phase 2: modernise -------->|
        migration service          Lakeflow Connect    synced tables
        (online, CDC)              (logical decoding)  (CONTINUOUS)
```

## Phase 1 - lift

Azure Database for PostgreSQL Flexible Server has a built-in migration service.
In **Online** mode it takes a full snapshot and then tails the source's logical
replication slot, so the application keeps writing while the copy catches up.
You cut over when you choose.

`workload.py` runs throughout. That is the point: the audience should see row
counts moving on both sides at once, not a maintenance-window story.

## Phase 2 - modernise

Lakeflow Connect reads the *same* logical decoding stream from the Azure PG
server and lands Delta tables in `retail_demo.onprem`. Now the data is in the
lakehouse: joinable, governed by Unity Catalog, available to ML.

Then **synced tables** push selected tables back out into Lakebase Postgres, so
the operational app gets its low-latency point reads back - over the ordinary
Postgres wire protocol, with the lakehouse as the system of record.

## Two constraints that shape the design

These are real product limits, not simplifications. Know them before someone in
the audience asks.

**1. Azure DMS cannot target Lakebase.** The supported DMS targets are Azure SQL
Database / Managed Instance, Azure Database for PostgreSQL and MySQL, and
Cosmos DB. There is no Databricks target. Phase 1 therefore lands in Azure PG.

**2. Lakebase cannot be a logical replication subscriber.** Databricks state
that replicating data to or from a Lakebase database using native Postgres
logical replication is not yet available. So you cannot shortcut Phase 2 by
pointing pglogical or DMS straight at Lakebase.

The consequence: **the supported path into Lakebase is always
`source -> CDC -> Delta in Unity Catalog -> synced table -> Lakebase`.** The
two-phase shape of this demo is not a teaching device, it is the only route.

**Also note:** DMS *Classic* is retired for PostgreSQL to Flexible Server. The
current tool is `az postgres flexible-server migration`, which is what
`infra/02_run_migration.ps1` calls. If a slide says "Azure Database Migration
Service", that is now the umbrella brand, not the classic resource.

## Why not just use a read replica?

A read replica gives you the same rows and nothing else. Lakebase gives you the
synced rows *plus* your own transactional tables in the same database, in the
same transaction - see section 8 of `databricks/03_serve_queries.sql`, where an
app-owned `app.order_review` table joins directly to `onprem.orders_synced`.
That is the argument worth making on stage.

## What the workload generator does

`src/workload.py` produces a realistic mix so the CDC stream is not just
inserts:

| Operation | Share | Why it is there |
| --- | --- | --- |
| New order + items | 50% | inserts, multi-row transactions |
| Payment state change | 20% | updates on a second table |
| Fulfilment | 20% | updates plus inventory movement |
| Restock | 5% | inserts on a high-volume table |
| Customer detail change | 3% | updates on a slowly-changing dimension |
| Cancellation | 2% | order marked cancelled, payment refunded, **order lines hard-DELETEd** |

The deletes matter. They are what separates real CDC from a timestamp-based
incremental load - the JDBC fallback notebook cannot see them, and saying so out
loud is one of the better moments in the demo.
