"""Generate the LakebaseMigrationDemo architecture diagram as .excalidraw."""
import json
import random

random.seed(7)
els = []


def base(eid, **kw):
    d = dict(
        id=eid, angle=0, opacity=100, groupIds=[], frameId=None,
        roundness=None, seed=random.randint(1, 2**31), version=1,
        versionNonce=random.randint(1, 2**31), isDeleted=False,
        boundElements=None, updated=1, link=None, locked=False,
        strokeStyle="solid", roughness=1, fillStyle="hachure",
        strokeWidth=1, backgroundColor="transparent", strokeColor="#1e1e1e",
    )
    d.update(kw)
    return d


def rect(eid, x, y, w, h, stroke, fill="transparent", sw=1, style="solid"):
    els.append(base(eid, type="rectangle", x=x, y=y, width=w, height=h,
                    strokeColor=stroke, backgroundColor=fill, strokeWidth=sw,
                    strokeStyle=style, roundness={"type": 3},
                    fillStyle="hachure" if fill != "transparent" else "solid"))


def text(eid, x, y, w, txt, size=14, align="left", bold_font=1):
    lines = txt.count("\n") + 1
    els.append(base(eid, type="text", x=x, y=y, width=w,
                    height=int(size * 2.5 * lines), text=txt, fontSize=size,
                    fontFamily=bold_font, strokeColor="#000000",
                    textAlign=align, verticalAlign="top", containerId=None,
                    originalText=txt, lineHeight=1.25))


def boxed(eid, x, y, w, h, stroke, fill, txt, size=14):
    """Leaf box with bound, centred text."""
    tid = eid + "_t"
    lines = txt.count("\n") + 1
    th = int(size * 2.5 * lines)
    els.append(base(eid, type="rectangle", x=x, y=y, width=w, height=h,
                    strokeColor=stroke, backgroundColor=fill, strokeWidth=1,
                    roundness={"type": 3}, fillStyle="hachure",
                    boundElements=[{"type": "text", "id": tid}]))
    els.append(base(tid, type="text", x=x + 14, y=y + (h - th) // 2,
                    width=w - 28, height=th, text=txt, fontSize=size,
                    fontFamily=1, strokeColor="#000000", textAlign="center",
                    verticalAlign="middle", containerId=eid,
                    originalText=txt, lineHeight=1.25))


def arrow(eid, x, y, pts, stroke, sw=2, style="solid", start=None, end=None):
    w = max(p[0] for p in pts) - min(p[0] for p in pts)
    h = max(p[1] for p in pts) - min(p[1] for p in pts)
    els.append(base(eid, type="arrow", x=x, y=y, width=w, height=h,
                    points=pts, strokeColor=stroke, strokeWidth=sw,
                    strokeStyle=style, roundness={"type": 2},
                    startArrowhead=None, endArrowhead="arrow",
                    startBinding=({"elementId": start, "focus": 0, "gap": 4}
                                  if start else None),
                    endBinding=({"elementId": end, "focus": 0, "gap": 4}
                                if end else None)))


# ---------------------------------------------------------------- palette
BLUE = ("#1864ab", "#a5d8ff")
ORANGE = ("#e67700", "#fff3bf")
PURPLE = ("#862e9c", "#f3d9fa")
TEAL = ("#0c8599", "#99e9f2")
RED = ("#D13438", "#FDE7E9")
GRAY = ("#495057", "#dee2e6")

CW, GAP, CY, CH = 540, 220, 300, 660
XS = [60, 60 + CW + GAP, 60 + 2 * (CW + GAP), 60 + 3 * (CW + GAP)]  # 60,820,1580,2340
RIGHT = XS[3] + CW  # 2880

# ---------------------------------------------------------------- title
text("title", 60, 30, 1800,
     "On-premises PostgreSQL  ->  Databricks Lakebase", 30)
text("subtitle", 60, 82, 2200,
     "Four tiers, two phases. The workload never stops writing - cutover is a decision, not an outage.", 16)

# ---------------------------------------------------------------- phase bands
# stacked on two rows so the overlapping span (tier 2) stays readable
rect("phase1", XS[0], 150, XS[1] + CW - XS[0], 48, GRAY[0], "transparent", 2, "dashed")
text("phase1_t", XS[0] + 20, 163, XS[1] + CW - XS[0] - 40,
     "PHASE 1  -  LIFT      full load + CDC tail, the source stays online throughout", 15)

rect("phase2", XS[1], 212, RIGHT - XS[1], 48, GRAY[0], "transparent", 2, "dashed")
text("phase2_t", XS[1] + 20, 225, RIGHT - XS[1] - 40,
     "PHASE 2  -  MODERNISE      land it in the lakehouse, then serve it back as Postgres", 15)

# ---------------------------------------------------------------- tiers
tiers = [
    (XS[0], BLUE, "1  ON-PREMISES     the source system", [
        ("PostgreSQL 17  -  schema 'retail'\ncustomer, product, orders, order_item,\npayment, inventory_movement\n>> system of record today",),
        ("workload.py\ninserts, updates and hard DELETEs\n~8 tps, --marker tags rows live\n>> keeps the source under load on stage",),
        ("retail_pub  +  role 'replicator'\nwal_level = logical\n>> exposes the WAL as a CDC stream\n   that two consumers can read",),
    ]),
    (XS[1], ORANGE, "2  AZURE     phase 1 landing zone", [
        ("Flexible Server migration service\naz postgres flexible-server migration\nMode = Online\n>> snapshot, then tail the slot",),
        ("Azure DB for PostgreSQL Flex v17\nwal_level = logical (re-published)\n>> the migration target, and the\n   source for Lakeflow Connect",),
        ("verify_parity.py\nrow counts + md5 checksums per tier\n>> proves the copy is correct while\n   the source is still taking writes",),
    ]),
    (XS[2], PURPLE, "3  UNITY CATALOG     the lakehouse", [
        ("Lakeflow Connect - Postgres connector\nreads the second replication slot\n>> streams CDC in, deletes included",),
        ("Delta tables  retail_demo.onprem.*\ndelta.enableChangeDataFeed = true\n>> governed analytical copy: lineage,\n   permissions, joinable, ML-ready",),
        ("01b_jdbc_ingest.py   (fallback)\nbatch MERGE on a watermark column\n>> safety net - but it CANNOT see\n   deleted rows. That is the point.",),
    ]),
    (XS[3], TEAL, "4  LAKEBASE     serving layer", [
        ("Synced tables   *_synced\nCONTINUOUS mode, ~15s lag\n>> reverse ETL: Delta -> Postgres,\n   read-only, lakehouse stays master",),
        ("Lakebase Postgres 17\nstandard wire protocol, sslmode=require\n>> millisecond point reads on the PK,\n   the app driver notices nothing",),
        ("app.order_review  (your tables)\nfully transactional, same database\n>> app state JOINs synced data in ONE\n   transaction - a read replica can't",),
    ]),
]

for ti, (x, (stroke, fill), heading, items) in enumerate(tiers):
    rect(f"c{ti}", x, CY, CW, CH, stroke, "transparent", 2)
    text(f"c{ti}_h", x + 22, CY + 18, CW - 40, heading, 17)
    for bi, (txt,) in enumerate(items):
        boxed(f"b{ti}_{bi}", x + 25, CY + 70 + bi * 195, CW - 50, 175,
              stroke, fill, txt, 13)

# ---------------------------------------------------------------- flow arrows
mid = CY + CH // 2
flows = [
    (0, "logical slot #1\nfull load, then\ncontinuous CDC", ORANGE[0]),
    (1, "logical slot #2\ncontinuous\ningest to Delta", PURPLE[0]),
    (2, "sync pipeline\nDelta -> Postgres\nevery ~15s", TEAL[0]),
]
for i, (idx, label, col) in enumerate(flows):
    gx = XS[idx] + CW
    arrow(f"fa{i}", gx + 18, mid, [[0, 0], [GAP - 36, 0]], col, 3,
          start=f"c{idx}", end=f"c{idx+1}")
    text(f"fl{i}", gx + 14, mid - 118, GAP - 20, label, 13, "center")

# consumers
boxed("apps", RIGHT + 90, mid - 62, 250, 124, GRAY[0], GRAY[1],
      "Applications,\nDatabricks Apps,\ndashboards, agents\n>> psql / JDBC", 13)
arrow("fa3", RIGHT + 14, mid, [[0, 0], [66, 0]], GRAY[0], 3, end="apps")

# ---------------------------------------------------------------- dead end
DY = CY + CH + 90
DX0, DX1 = XS[0] + 270, XS[3] + 270
arrow("dead", DX0, DY, [[0, 0], [DX1 - DX0, 0]], RED[0], 3, "dashed")
text("dead_x", (DX0 + DX1) // 2 - 500, DY - 52, 1000,
     "X    NOT POSSIBLE  -  there is no direct pipe from a Postgres server into Lakebase", 16, "center")

# ---------------------------------------------------------------- constraints
BY = DY + 60
rect("cons", 60, BY, RIGHT - 60 + 340, 210, RED[0], "transparent", 2)
text("cons_h", 90, BY + 20, 1400,
     "Why the shape is forced - two hard product limits", 18)
text("cons_b", 90, BY + 64, RIGHT - 180 + 340,
     "1.  Azure DMS has no Databricks target.  Supported targets are Azure SQL DB / Managed Instance, Azure DB for PostgreSQL and MySQL, and Cosmos DB.\n"
     "2.  Lakebase cannot be a logical replication subscriber.  Databricks: replicating to or from a Lakebase database with native Postgres logical replication is not yet available.\n"
     "\n"
     "=>  The only supported route into Lakebase is:   source  ->  CDC  ->  Delta in Unity Catalog  ->  synced table  ->  Lakebase.\n"
     "     The two-phase structure is a product constraint, not a teaching device.   (Note: DMS *Classic* is retired for PostgreSQL -> Flexible Server.)", 14)

doc = {"type": "excalidraw", "version": 2, "source": "copilot",
       "elements": els, "appState": {"viewBackgroundColor": "#ffffff",
                                     "gridSize": None}}
out = r"C:\Workshops\LakebaseMigrationDemo\docs\architecture.excalidraw"
with open(out, "w", encoding="utf-8") as f:
    json.dump(doc, f, indent=2)
print(f"wrote {out}  ({len(els)} elements)")
