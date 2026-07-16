"""SQLite datastore for registered clusters (admin-ui process).

Schema and connection settings MUST stay in lockstep with mcp_server —
the two processes share only the SQLite file (K8S_COST_AGENT_DB_PATH), never
imports or in-memory state.
"""

from __future__ import annotations

import os
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Literal

DB_PATH_ENV = "K8S_COST_AGENT_DB_PATH"

RbacStatus = Literal["verified", "not_verified", "error"]


@dataclass
class Cluster:
    """Cluster row as exposed to templates / callers.

    Tokens are NEVER included here once persisted — write-only field semantics.
    ``ca_cert_pem`` is public (CA material) and may be rendered for edit.
    Use ``get_cluster_secrets`` only inside verify / client-construction paths.
    """

    id: int
    name: str
    api_server_url: str
    kubecost_url: str | None
    ca_cert_pem: str | None
    rbac_status: RbacStatus
    rbac_last_checked: str | None
    rbac_detail: str | None
    created_at: str
    updated_at: str

    @property
    def has_ca_cert(self) -> bool:
        return bool(self.ca_cert_pem and self.ca_cert_pem.strip())


@dataclass
class ClusterSecrets:
    """Secret + client-construction material — never pass tokens into templates."""

    id: int
    name: str
    api_server_url: str
    sa_token: str
    kubecost_url: str | None
    kubecost_token: str | None
    ca_cert_pem: str | None


def get_db_path() -> Path:
    raw = os.environ.get(DB_PATH_ENV)
    if not raw:
        raise RuntimeError(
            f"{DB_PATH_ENV} is not set. Both admin-ui and mcp-server must "
            f"resolve the datastore from this single env var "
            f"(e.g. export {DB_PATH_ENV}=./data/clusters.db)."
        )
    return Path(raw).expanduser().resolve()


def _connect() -> sqlite3.Connection:
    path = get_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def db_conn() -> Iterator[sqlite3.Connection]:
    conn = _connect()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _migrate(conn: sqlite3.Connection) -> None:
    cols = {row[1] for row in conn.execute("PRAGMA table_info(clusters)").fetchall()}
    if cols and "ca_cert_pem" not in cols:
        conn.execute("ALTER TABLE clusters ADD COLUMN ca_cert_pem TEXT")


def init_db() -> None:
    with db_conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS clusters (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                name            TEXT NOT NULL UNIQUE,
                api_server_url  TEXT NOT NULL,
                sa_token        TEXT NOT NULL,
                kubecost_url    TEXT,
                kubecost_token  TEXT,
                ca_cert_pem     TEXT,
                rbac_status     TEXT NOT NULL DEFAULT 'not_verified'
                                CHECK (rbac_status IN ('verified','not_verified','error')),
                rbac_last_checked TEXT,
                rbac_detail     TEXT,
                created_at      TEXT NOT NULL,
                updated_at      TEXT NOT NULL
            )
            """
        )
        _migrate(conn)


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _normalize_pem(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _row_to_cluster(row: sqlite3.Row) -> Cluster:
    keys = row.keys()
    return Cluster(
        id=row["id"],
        name=row["name"],
        api_server_url=row["api_server_url"],
        kubecost_url=row["kubecost_url"],
        ca_cert_pem=row["ca_cert_pem"] if "ca_cert_pem" in keys else None,
        rbac_status=row["rbac_status"],
        rbac_last_checked=row["rbac_last_checked"],
        rbac_detail=row["rbac_detail"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def list_clusters() -> list[Cluster]:
    with db_conn() as conn:
        rows = conn.execute(
            "SELECT * FROM clusters ORDER BY name COLLATE NOCASE"
        ).fetchall()
    return [_row_to_cluster(r) for r in rows]


def get_cluster(cluster_id: int) -> Cluster | None:
    with db_conn() as conn:
        row = conn.execute(
            "SELECT * FROM clusters WHERE id = ?", (cluster_id,)
        ).fetchone()
    return _row_to_cluster(row) if row else None


def get_cluster_by_name(name: str) -> Cluster | None:
    with db_conn() as conn:
        row = conn.execute(
            "SELECT * FROM clusters WHERE name = ?", (name,)
        ).fetchone()
    return _row_to_cluster(row) if row else None


def get_cluster_secrets(cluster_id: int) -> ClusterSecrets | None:
    """Return secret + CA columns. Callers must not render tokens."""
    with db_conn() as conn:
        row = conn.execute(
            "SELECT id, name, api_server_url, sa_token, kubecost_url, kubecost_token, "
            "ca_cert_pem FROM clusters WHERE id = ?",
            (cluster_id,),
        ).fetchone()
    if not row:
        return None
    return ClusterSecrets(
        id=row["id"],
        name=row["name"],
        api_server_url=row["api_server_url"],
        sa_token=row["sa_token"],
        kubecost_url=row["kubecost_url"],
        kubecost_token=row["kubecost_token"],
        ca_cert_pem=row["ca_cert_pem"],
    )


def create_cluster(
    *,
    name: str,
    api_server_url: str,
    sa_token: str,
    kubecost_url: str | None = None,
    kubecost_token: str | None = None,
    ca_cert_pem: str | None = None,
) -> Cluster:
    now = _now()
    with db_conn() as conn:
        cur = conn.execute(
            """
            INSERT INTO clusters (
                name, api_server_url, sa_token, kubecost_url, kubecost_token,
                ca_cert_pem, rbac_status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, 'not_verified', ?, ?)
            """,
            (
                name.strip(),
                api_server_url.strip(),
                sa_token.strip(),
                (kubecost_url or "").strip() or None,
                (kubecost_token or "").strip() or None,
                _normalize_pem(ca_cert_pem),
                now,
                now,
            ),
        )
        cluster_id = int(cur.lastrowid)
    cluster = get_cluster(cluster_id)
    assert cluster is not None
    return cluster


def update_cluster(
    cluster_id: int,
    *,
    name: str,
    api_server_url: str,
    sa_token: str | None = None,
    kubecost_url: str | None = None,
    kubecost_token: str | None = None,
    ca_cert_pem: str | None = None,
    clear_kubecost_token: bool = False,
) -> Cluster | None:
    """Update a cluster. Blank ``sa_token`` means keep the existing token.

    ``ca_cert_pem`` is always written (blank clears the stored CA).
    """
    existing = get_cluster_secrets(cluster_id)
    if existing is None:
        return None

    token = sa_token.strip() if sa_token and sa_token.strip() else existing.sa_token
    if clear_kubecost_token:
        kc_token: str | None = None
    elif kubecost_token is not None and kubecost_token.strip():
        kc_token = kubecost_token.strip()
    else:
        kc_token = existing.kubecost_token

    now = _now()
    with db_conn() as conn:
        conn.execute(
            """
            UPDATE clusters SET
                name = ?,
                api_server_url = ?,
                sa_token = ?,
                kubecost_url = ?,
                kubecost_token = ?,
                ca_cert_pem = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (
                name.strip(),
                api_server_url.strip(),
                token,
                (kubecost_url or "").strip() or None,
                kc_token,
                _normalize_pem(ca_cert_pem),
                now,
                cluster_id,
            ),
        )
    return get_cluster(cluster_id)


def delete_cluster(cluster_id: int) -> bool:
    with db_conn() as conn:
        cur = conn.execute("DELETE FROM clusters WHERE id = ?", (cluster_id,))
        return cur.rowcount > 0


def update_rbac_status(
    cluster_id: int,
    *,
    status: RbacStatus,
    detail: str | None = None,
) -> Cluster | None:
    now = _now()
    with db_conn() as conn:
        cur = conn.execute(
            """
            UPDATE clusters SET
                rbac_status = ?,
                rbac_last_checked = ?,
                rbac_detail = ?,
                updated_at = ?
            WHERE id = ?
            """,
            (status, now, detail, now, cluster_id),
        )
        if cur.rowcount == 0:
            return None
    return get_cluster(cluster_id)
