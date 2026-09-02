"""Shared configuration. Reads a gitignored .env - nothing here is committed."""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load_env():
    env = ROOT / ".env"
    if not env.exists():
        return
    for line in env.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_env()


def _dsn(prefix, default_db):
    return dict(
        host=os.getenv(f"{prefix}_HOST", "localhost"),
        port=int(os.getenv(f"{prefix}_PORT", "5432")),
        dbname=os.getenv(f"{prefix}_DB", default_db),
        user=os.getenv(f"{prefix}_USER", "postgres"),
        password=os.getenv(f"{prefix}_PASSWORD", ""),
    )


# Tier 1 - the "on-premises" estate (local PostgreSQL 17)
ONPREM = _dsn("ONPREM", "retail_onprem")
ONPREM_ADMIN_DB = os.getenv("ONPREM_ADMIN_DB", "postgres")

# Tier 2 - Azure Database for PostgreSQL Flexible Server (Azure DMS target)
AZURE_PG = _dsn("AZPG", "retail_onprem")

# Tier 4 - Databricks Lakebase Postgres
LAKEBASE = _dsn("LAKEBASE", "retail")

# Databricks control plane
DATABRICKS_HOST = os.getenv("DATABRICKS_HOST", "").rstrip("/")
DATABRICKS_TOKEN = os.getenv("DATABRICKS_TOKEN", "")
UC_CATALOG = os.getenv("UC_CATALOG", "retail_demo")
UC_SCHEMA = os.getenv("UC_SCHEMA", "onprem")

TABLES = ["customer", "product", "orders", "order_item", "payment", "inventory_movement"]
