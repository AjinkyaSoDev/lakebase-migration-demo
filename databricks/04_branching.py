"""Phase 3 - Lakebase branching: cutover rehearsal, PITR recovery, dev/test fleet.

Branching is the capability that has no equivalent in a managed Postgres
service, and it is the reason the migration story in this repo ends somewhere
better than "the same database, in the cloud".

Branches are copy-on-write against the shared storage layer. Creating one does
not copy data, so a branch of a 2 TB database is created in about the same time
as a branch of a 2 MB one, and it only accrues storage for pages it changes.

Three acts, each mapped to a thing a DBA actually has to do:

  rehearse   cutover / schema-change rehearsal against real production data,
             thrown away afterwards          -> "migrations"
  recover    point-in-time recovery: branch from a timestamp BEFORE a bad
             write, compare, then recover    -> "backup and recovery"
  fleet      N developer environments off one production dataset, showing
             logical size rather than N full copies -> "scale-out / cost"

Usage:
    pip install databricks-sdk psycopg2-binary
    python databricks/04_branching.py list
    python databricks/04_branching.py rehearse --ttl-hours 2
    python databricks/04_branching.py recover --minutes-ago 15
    python databricks/04_branching.py fleet --devs alice bob carol
    python databricks/04_branching.py cleanup

Every branch this script creates is tagged with a name prefix (default
"demo-") and given a TTL, so `cleanup` can find them and nothing is left
running after the demo.
"""
import argparse
import datetime as dt
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import config  # noqa: E402

from databricks.sdk import WorkspaceClient  # noqa: E402
from databricks.sdk.service.postgres import (  # noqa: E402
    Branch,
    BranchSpec,
    Duration,
    Endpoint,
    EndpointSpec,
    EndpointType,
    Timestamp,
)

PREFIX = "demo-"


# ---------------------------------------------------------------- helpers

def _ts(when: dt.datetime) -> Timestamp:
    """protobuf Timestamp from an aware datetime.

    Gotcha: BranchSpec.source_branch_time and .ttl are google.protobuf
    Timestamp/Duration objects, not strings. Passing an ISO string silently
    fails at serialization time with an unhelpful error.
    """
    if when.tzinfo is None:
        when = when.replace(tzinfo=dt.timezone.utc)
    t = Timestamp()
    t.FromDatetime(when.astimezone(dt.timezone.utc))
    return t


def _ttl(hours: float) -> Duration:
    d = Duration()
    d.FromSeconds(int(hours * 3600))
    return d


def _branch_path(project: str, branch: str) -> str:
    return f"projects/{project}/branches/{branch}"


def _gib(n):
    return "-" if not n else f"{n / 1024 ** 3:.3f} GiB"


def _is_root(br):
    """Root branches have no parent branch. PITR branches are roots."""
    return not getattr(br.status, "source_branch", None)


def _root_branch_count(w, project):
    return sum(1 for br in w.postgres.list_branches(parent=f"projects/{project}")
               if _is_root(br))


def make_branch(w, project, branch_id, *, source="production",
                at=None, ttl_hours=None):
    """Create a copy-on-write branch. Returns the created Branch."""
    spec = BranchSpec(source_branch=_branch_path(project, source))
    if at is not None:
        spec.source_branch_time = _ts(at)
    if ttl_hours:
        spec.ttl = _ttl(ttl_hours)
    else:
        spec.no_expiry = True

    t0 = time.time()
    br = w.postgres.create_branch(
        parent=f"projects/{project}",
        branch=Branch(spec=spec),
        branch_id=branch_id,
    ).wait()
    elapsed = time.time() - t0

    at_note = f" as of {at.isoformat(timespec='seconds')}" if at else ""
    print(f"  branch '{branch_id}' created in {elapsed:.1f}s "
          f"(from {source}{at_note})")
    return br


def make_endpoint(w, project, branch_id, endpoint_id=None,
                  min_cu=0.5, max_cu=2.0, read_only=False):
    """A branch is only queryable once it has a compute endpoint.

    The endpoint is what costs money, not the branch. Suspend timeout means it
    scales to zero when the demo stops touching it.
    """
    endpoint_id = endpoint_id or f"{branch_id}-ep"
    suspend = Duration()
    suspend.FromSeconds(300)
    spec = EndpointSpec(
        endpoint_type=(EndpointType.ENDPOINT_TYPE_READ_ONLY if read_only
                       else EndpointType.ENDPOINT_TYPE_READ_WRITE),
        autoscaling_limit_min_cu=min_cu,
        autoscaling_limit_max_cu=max_cu,
        suspend_timeout_duration=suspend,
    )
    ep = w.postgres.create_endpoint(
        parent=_branch_path(project, branch_id),
        endpoint=Endpoint(spec=spec),
        endpoint_id=endpoint_id,
    ).wait()
    host = getattr(getattr(ep.status, "hosts", None), "host", None)
    print(f"  endpoint '{endpoint_id}' ready  host={host}")
    return ep


def connect(w, endpoint_name, host, dbname, user):
    """Open a psycopg2 connection to a branch endpoint.

    Auth is an OAuth token used as the Postgres password - there is no static
    password to leak into a repo. Tokens are short-lived, so generate one per
    connection rather than caching it.
    """
    import psycopg2
    cred = w.postgres.generate_database_credential(endpoint=endpoint_name)
    return psycopg2.connect(host=host, port=5432, dbname=dbname,
                            user=user, password=cred.token, sslmode="require")


def _one(conn, sql):
    with conn.cursor() as cur:
        cur.execute(sql)
        row = cur.fetchone()
    return row[0] if row else None


# ---------------------------------------------------------------- act 1

def cmd_rehearse(w, args):
    """Rehearse the cutover against real data, then throw the branch away.

    This is the answer to "how do you de-risk the migration cutover?" that
    doesn't involve buying a second production-sized environment.
    """
    name = f"{PREFIX}rehearsal-{dt.datetime.now():%Y%m%d-%H%M}"
    print(f"\n[1/4] branching production -> {name}")
    make_branch(w, args.project, name, source=args.source,
                ttl_hours=args.ttl_hours)

    print("\n[2/4] attaching compute")
    ep = make_endpoint(w, args.project, name, min_cu=args.min_cu,
                       max_cu=args.max_cu)
    host = ep.status.hosts.host

    print("\n[3/4] running the cutover change on the branch")
    print(f"      (production is untouched and still serving traffic)")
    try:
        conn = connect(w, ep.name, host, args.pg_database, args.pg_user)
        conn.autocommit = True
        schema = config.UC_SCHEMA
        before = _one(conn, f"SELECT count(*) FROM {schema}.orders_synced")
        print(f"      rows visible on branch : {before}")

        # The kind of change you would never test in production first.
        with conn.cursor() as cur:
            cur.execute(f"ALTER TABLE {schema}.orders_synced "
                        f"ADD COLUMN IF NOT EXISTS migration_batch int")
            cur.execute(f"CREATE INDEX IF NOT EXISTS ix_rehearsal_status "
                        f"ON {schema}.orders_synced (order_status)")
        print("      schema change + index applied on branch: OK")

        after = _one(conn, f"SELECT count(*) FROM {schema}.orders_synced")
        print(f"      rows after change      : {after}  "
              f"({'parity holds' if after == before else 'PARITY BROKEN'})")
        conn.close()
    except Exception as e:
        print(f"      ! could not connect/run: {str(e)[:160]}")
        print("        (branch still created - inspect it in the UI)")

    print("\n[4/4] rehearsal complete")
    br = w.postgres.get_branch(name=_branch_path(args.project, name))
    print(f"      branch logical size : {_gib(br.status.logical_size_bytes)}")
    print(f"      TTL                 : {args.ttl_hours}h, then auto-expires")
    print("\n      Talk track: that rehearsal used real production data, cost")
    print("      the delta only, and is discarded. Do it nightly until the")
    print("      cutover is boring.")
    print("\n      Be accurate about the limit: there is no promote-child-to-")
    print("      parent operation. Branch reset only flows parent -> child.")
    print("      Once the rehearsal passes you re-run the same migration")
    print("      against production with your normal tooling - the branch")
    print("      proves the change is safe, it does not ship it.")
    return 0


# ---------------------------------------------------------------- act 2

def cmd_recover(w, args):
    """Point-in-time recovery: branch from BEFORE a bad write and compare.

    Restore is a metadata operation on versioned storage, so recovery time is
    largely independent of database size - the opposite of a physical restore.

    Two things to know before demoing this:
      * A timestamp branch is created as a NEW ROOT BRANCH, not a child. A
        project is capped at 3 root branches, so clean up between runs.
      * How far back you can go is the project's restore window: 2-30 days,
        default 7, set per project and applying to every branch in it.
    """
    target = dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=args.minutes_ago)
    name = f"{PREFIX}pitr-{dt.datetime.now():%Y%m%d-%H%M}"

    roots = _root_branch_count(w, args.project)
    if roots >= 3:
        print(f"\n  ! project already has {roots} root branches (max 3).")
        print("    run `python databricks/04_branching.py cleanup` first.")
        return 1

    print(f"\n[1/3] branching {args.source} as of {target.isoformat(timespec='seconds')}")
    print(f"      (that is {args.minutes_ago} minutes ago - before the bad write)")
    print(f"      this becomes root branch {roots + 1} of 3")
    make_branch(w, args.project, name, source=args.source, at=target,
                ttl_hours=args.ttl_hours)

    # Must be READ_WRITE: a branch is reachable only through its R/W compute.
    print("\n[2/3] attaching compute")
    ep = make_endpoint(w, args.project, name, min_cu=args.min_cu,
                       max_cu=args.max_cu)

    print("\n[3/3] compare past vs present")
    try:
        schema = config.UC_SCHEMA
        past = connect(w, ep.name, ep.status.hosts.host,
                       args.pg_database, args.pg_user)
        n_past = _one(past, f"SELECT count(*) FROM {schema}.orders_synced")
        past.close()
        print(f"      rows as of T-{args.minutes_ago}m : {n_past}")
        print("      diff the two, copy back what you need, or repoint the app")
        print("      at this branch. No restore window, no tape, no downtime.")
    except Exception as e:
        print(f"      ! could not connect: {str(e)[:160]}")

    print("\n      Note: how far back you can branch is bounded by the")
    print("      project's restore window - 2 to 30 days, default 7. Check it")
    print("      before you promise an RPO to anyone.")
    return 0


# ---------------------------------------------------------------- act 3

def cmd_fleet(w, args):
    """One production dataset, N developer environments, ~no extra storage."""
    print(f"\nCreating {len(args.devs)} dev branches from {args.source}\n")
    created = []
    for d in args.devs:
        name = f"{PREFIX}dev-{d}"
        try:
            make_branch(w, args.project, name, source=args.source,
                        ttl_hours=args.ttl_hours)
            created.append(name)
        except Exception as e:
            msg = str(e)
            if "already exists" in msg.lower():
                print(f"  branch '{name}' already exists")
                created.append(name)
            else:
                print(f"  FAILED {name}: {msg[:120]}")

    print("\nLogical size per branch:")
    total = 0
    for name in created:
        try:
            br = w.postgres.get_branch(name=_branch_path(args.project, name))
            size = br.status.logical_size_bytes or 0
            total += size
            print(f"  {name:<28} {_gib(size)}")
        except Exception as e:
            print(f"  {name:<28} ? ({str(e)[:60]})")

    print(f"\n  {len(created)} full-fidelity environments, {_gib(total)} of")
    print("  branch storage. The traditional answer is N restores of a")
    print("  production backup, N times the storage, and a day of waiting.")
    print("\n  No compute was attached - branches with no endpoint cost")
    print("  storage only, and are archived automatically after inactivity")
    print("  (connecting unarchives them). Attach compute when a developer")
    print("  actually connects. Note the project cap: 20 concurrently active")
    print("  computes, so it is the endpoints you ration, not the branches.")
    return 0


# ---------------------------------------------------------------- utility

def cmd_list(w, args):
    print(f"\nBranches in project '{args.project}':\n")
    print(f"  {'BRANCH':<34}{'STATE':<12}{'ROOT':<7}{'SIZE':<12}EXPIRES")
    roots = 0
    for br in w.postgres.list_branches(parent=f"projects/{args.project}"):
        st = br.status
        state = getattr(getattr(st, "current_state", None), "name", "?")
        exp = st.expire_time.ToJsonString() if getattr(st, "expire_time", None) else "-"
        root = _is_root(br)
        roots += root
        print(f"  {br.branch_id:<34}{state:<12}{str(root):<7}"
              f"{_gib(getattr(st, 'logical_size_bytes', 0)):<12}{exp}")
    print(f"\n  root branches: {roots}/3 (PITR restores create root branches)")
    return 0


def cmd_cleanup(w, args):
    """Delete every branch this script created. Never touches production."""
    print(f"\nDeleting branches prefixed '{PREFIX}' in '{args.project}'\n")
    n = 0
    for br in w.postgres.list_branches(parent=f"projects/{args.project}"):
        bid = br.branch_id or ""
        if not bid.startswith(PREFIX):
            continue
        # Belt and braces: production is the root branch and cannot be deleted.
        if bid == "production" or getattr(br.status, "default", False):
            print(f"  skip {bid} (default/root branch)")
            continue
        try:
            w.postgres.delete_branch(name=br.name, purge=True).wait()
            print(f"  deleted {bid}")
            n += 1
        except Exception as e:
            print(f"  FAILED {bid}: {str(e)[:120]}")
    print(f"\n  {n} branch(es) removed. Endpoints go with them.")
    print(f"  root branches now in use: {_root_branch_count(w, args.project)}/3")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("command",
                    choices=["rehearse", "recover", "fleet", "list", "cleanup"])
    ap.add_argument("--project", default="retail-lakebase")
    ap.add_argument("--source", default="production", help="branch to fork from")
    ap.add_argument("--pg-database", default="retail")
    ap.add_argument("--pg-user", default=None,
                    help="Postgres role; defaults to the caller's identity")
    ap.add_argument("--ttl-hours", type=float, default=4.0)
    ap.add_argument("--min-cu", type=float, default=0.5)
    ap.add_argument("--max-cu", type=float, default=2.0)
    ap.add_argument("--minutes-ago", type=int, default=15,
                    help="recover: how far back to branch")
    ap.add_argument("--devs", nargs="*", default=["alice", "bob", "carol"])
    args = ap.parse_args()

    if not config.DATABRICKS_HOST:
        print("DATABRICKS_HOST is not set - see .env.example")
        return 1

    w = WorkspaceClient(host=config.DATABRICKS_HOST, token=config.DATABRICKS_TOKEN)
    if args.pg_user is None:
        try:
            args.pg_user = w.current_user.me().user_name
        except Exception:
            args.pg_user = config.LAKEBASE.get("user", "postgres")

    return {
        "rehearse": cmd_rehearse,
        "recover": cmd_recover,
        "fleet": cmd_fleet,
        "list": cmd_list,
        "cleanup": cmd_cleanup,
    }[args.command](w, args)


if __name__ == "__main__":
    sys.exit(main())
