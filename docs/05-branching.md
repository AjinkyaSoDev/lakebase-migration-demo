# 05 - Branching: rehearsal, recovery and dev/test

The migration gets the data into Lakebase. Branching is the reason you would
*want* it there rather than in any other managed Postgres.

A branch is a copy-on-write fork of the storage layer. Databricks is explicit
about what that means:

> "Your branches appear instantly; the size of your database has no impact on
> branch creation time."

> "If you create a development branch and modify 1GB of data in a 100GB
> database, you pay for approximately 1GB of storage, not 200GB. The unchanged
> 99GB is shared between branches."

> "Creating branches has no performance impact on your production workload."

That combination — instant, cheap, isolated, from real data — is what makes the
three workflows below practical rather than aspirational.

---

## Object model

```
Project                         projects/{project}
└── Branch                      projects/{project}/branches/{branch}
    ├── Endpoint (compute)      .../endpoints/{endpoint}
    ├── Database                max 500 per branch
    └── Role                    max 500 per branch, branch-scoped
```

- The root branch is called **`production`** and **cannot be deleted**.
- Branch trees nest arbitrarily (`production → staging → feature-test`); no
  documented depth limit.
- **A branch is only reachable through its own R/W compute endpoint.** The
  branch is the data; the endpoint is the thing that costs money.
- Hostnames are **per endpoint**, not per branch — `ep-xxxx.databricks.com`,
  port 5432, `sslmode=require` always.

---

## Running it

```bash
pip install databricks-sdk psycopg2-binary

python databricks/04_branching.py list
python databricks/04_branching.py rehearse --ttl-hours 2
python databricks/04_branching.py recover  --minutes-ago 15
python databricks/04_branching.py fleet    --devs alice bob carol
python databricks/04_branching.py cleanup
```

Every branch the script creates is prefixed `demo-` and given a TTL, so
`cleanup` can find them and nothing keeps running after the demo. `cleanup`
refuses to touch `production` or the default branch.

---

## Act 1 — `rehearse`: cutover and schema-change rehearsal

The question this answers: *"how do you de-risk the cutover without buying a
second production-sized environment?"*

1. Branch `production` — instant, no copy.
2. Attach a small compute (0.5–2 CU, suspends after 5 min).
3. Run the cutover change (an `ALTER TABLE` and an index build) **on the
   branch**, against real production data, while production keeps serving.
4. Check row parity, then throw the branch away.

Do it nightly on a schedule until the cutover is boring.

**Be accurate about the limit.** There is no promote-child-to-parent operation.
The docs are explicit:

> "Branch reset only works one direction (parent → child). To move changes from
> child to parent, use your standard migration tools to apply schema changes."

So the branch *proves the change is safe*; it does not ship it. You still re-run
the same migration against production with Flyway/Alembic/whatever you already
use. Saying this unprompted is more credible than implying a merge button exists.

---

## Act 2 — `recover`: point-in-time recovery

Databricks' own example: *"if a critical table was dropped yesterday at 10:23
AM, you can create a branch set to 10:22 AM to extract the missing data."*

The script branches from a timestamp N minutes ago, attaches compute, and
compares row counts past-vs-present. You then diff, copy back what you need, or
repoint the application at the recovered branch.

Because it is a metadata operation over versioned storage, **recovery time is
largely independent of database size** — the opposite of a physical restore.

### Two constraints that will bite you in a live demo

| Constraint | Detail |
| --- | --- |
| **PITR creates a new *root* branch** | Not a child. A project is capped at **3 root branches**, so the script counts them first and refuses rather than failing halfway. Run `cleanup` between takes. |
| **Restore window bounds how far back you can go** | **2–30 days, default 7**, configured **per project** and applying to every branch in it. PITR history does not count against the storage quota. |

That second row is the one to volunteer in an architecture conversation: know
the window before anyone states an RPO.

Also note the original branch is **unchanged** by a restore, and **no automatic
backup branch is created**.

---

## Act 3 — `fleet`: dev/test without the sprawl

N developer environments from one production dataset. The script prints
`status.logical_size_bytes` per branch so the cost argument is measured, not
asserted.

The traditional alternative is N restores of a production backup, N times the
storage, and a day of waiting — which is why most teams give developers a stale
subset instead, and find the bugs in production.

Branches with no endpoint cost **storage only**, and are archived automatically
after inactivity (connecting unarchives them). The cap to plan against is
**20 concurrently active computes per project** — you ration endpoints, not
branches.

Documented patterns worth quoting: per-developer branches named `dev/<name>`,
ephemeral CI branches that "clean up after pipeline completion", and time-boxed
feature branches.

---

## Implementation notes

Things that cost time when writing this, and are worth knowing.

**`ttl` and `source_branch_time` are protobuf types, not strings.**
`BranchSpec.ttl` is a `google.protobuf.Duration` and `source_branch_time` is a
`google.protobuf.Timestamp`. Passing an ISO string fails at serialization with
an unhelpful error. Build them properly:

```python
from databricks.sdk.service.postgres import Duration, Timestamp

ttl = Duration(); ttl.FromSeconds(4 * 3600)
at  = Timestamp(); at.FromDatetime(when_utc)      # must be tz-aware
```

They serialize to `"14400s"` and `"2026-09-03T10:31:48Z"` respectively.

**`ttl`, `expire_time` and `no_expiry` are mutually exclusive.** Set exactly one.

**Auth is an OAuth token used as the Postgres password**, valid ~1 hour, so
generate one per physical connection rather than caching it:

```python
cred = w.postgres.generate_database_credential(endpoint=endpoint.name)
psycopg2.connect(host=..., user=..., password=cred.token, sslmode="require")
```

There is no static password to leak into a repo. The trade-off: **OAuth auth
does not support connection pooling** — for PgBouncer you need a native Postgres
role password instead.

**There is no `reset_branch` in the SDK.** Reset-from-parent is documented as a
UI operation only. `BranchStatusState.RESETTING` exists as an internal state,
but no programmatic call is published. Don't claim otherwise.

**Synced tables on child branches are not documented.** A child inherits the
synced table's *data* copy-on-write, but the sync pipeline targets a specific
branch and will not follow the child — so the child's copy is effectively a
static snapshot. The Databricks docs do not state this explicitly, so treat it
as untested inference, and say so rather than asserting it.

---

## Status

Core branching (create, delete, reset, PITR) carries no Preview or Beta label in
the docs and reads as generally available, but there is **no explicit GA badge
or announcement date** on those pages. Adjacent features that *are* labelled:
**LTAP Direct Writes — Beta**; **automatic change data feed for synced tables —
Public Preview**.

The honest formulation: *"core branching is documented as a production feature
with no Preview label; LTAP Direct Writes is Beta and automatic CDF is Public
Preview."*

---

## Sources

- [Branches](https://docs.databricks.com/aws/en/oltp/projects/branches)
- [Manage branches](https://docs.databricks.com/aws/en/oltp/projects/manage-branches)
- [Point-in-time restore](https://docs.databricks.com/aws/en/oltp/projects/point-in-time-restore)
- [Point-in-time branching](https://docs.databricks.com/aws/en/oltp/projects/point-in-time-branching)
- [Manage projects (limits)](https://docs.databricks.com/aws/en/oltp/projects/manage-projects)
- [Manage computes](https://docs.databricks.com/aws/en/oltp/projects/manage-computes)
- [Connection strings](https://docs.databricks.com/aws/en/oltp/projects/connection-strings)
- [Authentication](https://docs.databricks.com/aws/en/oltp/projects/authentication)
- [Dev workflow tutorial](https://docs.databricks.com/aws/en/oltp/projects/dev-workflow-tutorial)
- [Python SDK - PostgresAPI](https://databricks-sdk-py.readthedocs.io/en/latest/workspace/postgres/postgres.html)
