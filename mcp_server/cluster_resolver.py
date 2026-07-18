"""Per-request cluster resolution for the MCP server.

Read-only SQLite access (WAL + busy_timeout). Builds a fresh kubernetes
Configuration per call — never a module-level/global client.
Refuses clusters that have not passed RBAC verification.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator

from kubernetes import client

from mcp_server.agent.kubecost_client import KubecostClient

logger = logging.getLogger(__name__)

DB_PATH_ENV = "K8S_COST_AGENT_DB_PATH"


class ClusterNotFoundError(ValueError):
    """Raised when the named cluster is not in the datastore."""


class ClusterNotVerifiedError(ValueError):
    """Raised when rbac_status is not 'verified' — do not query the cluster."""


class ClusterUnreachableError(RuntimeError):
    """Raised when the cluster API cannot be reached (down, network, auth, TLS)."""


@dataclass(frozen=True)
class ClusterPublicInfo:
    """Non-secret cluster metadata for list_registered_clusters."""

    name: str
    api_server_url: str
    rbac_verified: bool
    rbac_status: str
    last_checked: str | None
    kubecost_endpoint: str | None
    has_ca_cert: bool


@dataclass
class ResolvedCluster:
    """Per-request clients + metadata. Tokens never leave this process via MCP."""

    name: str
    api_server_url: str
    configuration: client.Configuration
    api_client: client.ApiClient
    core_v1: client.CoreV1Api
    custom_objects: client.CustomObjectsApi
    kubecost: KubecostClient | None
    rbac_status: str
    last_checked: str | None
    tls_verified: bool
    # Keep NamedTemporaryFile alive so ssl_ca_cert path remains valid for this request.
    _ca_tempfile: Any = field(default=None, repr=False, compare=False)


def get_db_path() -> Path:
    raw = os.environ.get(DB_PATH_ENV)
    if not raw:
        raise RuntimeError(
            f"{DB_PATH_ENV} is not set. The MCP server and admin UI must "
            f"share the same path (e.g. export {DB_PATH_ENV}=./data/clusters.db)."
        )
    return Path(raw).expanduser().resolve()


def _connect_readonly() -> sqlite3.Connection:
    """Open the shared DB for reads only. Never writes cluster rows."""
    path = get_db_path()
    if not path.exists():
        raise FileNotFoundError(
            f"Datastore not found at {path}. Register clusters via the admin UI "
            f"(or `make init-db`) and ensure {DB_PATH_ENV} matches."
        )
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


@contextmanager
def _db_conn() -> Iterator[sqlite3.Connection]:
    conn = _connect_readonly()
    try:
        yield conn
    finally:
        conn.close()


def _row_public(row: sqlite3.Row) -> ClusterPublicInfo:
    status = row["rbac_status"]
    keys = row.keys()
    ca = row["ca_cert_pem"] if "ca_cert_pem" in keys else None
    return ClusterPublicInfo(
        name=row["name"],
        api_server_url=row["api_server_url"],
        rbac_verified=(status == "verified"),
        rbac_status=status,
        last_checked=row["rbac_last_checked"],
        kubecost_endpoint=row["kubecost_url"],
        has_ca_cert=bool(ca and str(ca).strip()),
    )


def list_registered_clusters() -> list[ClusterPublicInfo]:
    """Return all registered clusters without tokens."""
    with _db_conn() as conn:
        rows = conn.execute(
            "SELECT name, api_server_url, kubecost_url, ca_cert_pem, rbac_status, "
            "rbac_last_checked FROM clusters ORDER BY name COLLATE NOCASE"
        ).fetchall()
    return [_row_public(r) for r in rows]


def _fetch_cluster_row(cluster_name: str) -> sqlite3.Row:
    with _db_conn() as conn:
        row = conn.execute(
            "SELECT name, api_server_url, sa_token, kubecost_url, kubecost_token, "
            "ca_cert_pem, rbac_status, rbac_last_checked FROM clusters WHERE name = ?",
            (cluster_name,),
        ).fetchone()
    if row is None:
        raise ClusterNotFoundError(
            f"Cluster {cluster_name!r} is not registered. "
            "Add it in the admin UI (or scripts/register_cluster.py), then "
            "verify RBAC before querying."
        )
    return row


def _build_configuration(
    *,
    cluster_name: str,
    api_server_url: str,
    sa_token: str,
    ca_cert_pem: str | None,
) -> tuple[client.Configuration, Any | None, bool]:
    """
    Fresh Configuration for this call only — never cache globally.

    Returns (configuration, ca_tempfile_or_None, tls_verified).
    """
    configuration = client.Configuration(
        host=api_server_url.rstrip("/"),
        api_key={"authorization": sa_token},
        api_key_prefix={"authorization": "Bearer"},
    )

    pem = (ca_cert_pem or "").strip()
    if pem:
        tmp = tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            suffix=".pem",
            prefix=f"k8s-ca-{cluster_name}-",
            delete=True,
        )
        tmp.write(pem)
        if not pem.endswith("\n"):
            tmp.write("\n")
        tmp.flush()
        configuration.ssl_ca_cert = tmp.name
        configuration.verify_ssl = True
        return configuration, tmp, True

    logger.warning(
        "cluster '%s' registered without a CA cert — TLS verification "
        "disabled, insecure. Add a CA cert in the admin UI to fix this.",
        cluster_name,
    )
    configuration.verify_ssl = False
    return configuration, None, False


def assert_cluster_reachable(
    resolved: ResolvedCluster,
    *,
    timeout_seconds: float = 10.0,
) -> None:
    """
    Live probe: confirm the API server answers a cheap read.

    Registration in SQLite is not enough — a spoke can be down while still
    listed. Raises ClusterUnreachableError instead of letting workers treat
    a failed list as "zero pods / no anomalies".
    """
    try:
        # limit=1 keeps the probe cheap; timeout avoids hanging the MCP tool.
        resolved.core_v1.list_namespace(limit=1, _request_timeout=timeout_seconds)
    except Exception as exc:  # noqa: BLE001 — any transport/API failure = unreachable
        logger.error(
            "cluster '%s' unreachable at %s: %s",
            resolved.name,
            resolved.api_server_url,
            exc,
        )
        raise ClusterUnreachableError(
            f"Cluster '{resolved.name}' is registered but unreachable at "
            f"{resolved.api_server_url}: {exc}. "
            "Check that the API server is up, the network path is open, "
            "and the ServiceAccount token / CA cert are still valid."
        ) from exc


def resolve_cluster_client(cluster_name: str) -> ResolvedCluster:
    """
    Resolve a registered, RBAC-verified cluster into per-request clients.

    Does not probe live connectivity — call ``assert_cluster_reachable``
    before running analysis tools.

    Raises:
        ClusterNotFoundError: name missing from SQLite.
        ClusterNotVerifiedError: rbac_status != 'verified'.
    """
    row = _fetch_cluster_row(cluster_name)
    status = row["rbac_status"]
    if status != "verified":
        raise ClusterNotVerifiedError(
            f"Cluster '{cluster_name}' has not passed RBAC verification "
            f"(status={status!r}). Verify it in the admin UI before querying."
        )

    keys = row.keys()
    ca_cert_pem = row["ca_cert_pem"] if "ca_cert_pem" in keys else None
    configuration, ca_tmp, tls_verified = _build_configuration(
        cluster_name=row["name"],
        api_server_url=row["api_server_url"],
        sa_token=row["sa_token"],
        ca_cert_pem=ca_cert_pem,
    )
    api_client = client.ApiClient(configuration)
    core_v1 = client.CoreV1Api(api_client)
    custom_objects = client.CustomObjectsApi(api_client)

    kubecost: KubecostClient | None = None
    if row["kubecost_url"]:
        kubecost = KubecostClient(
            endpoint=row["kubecost_url"],
            token=row["kubecost_token"],
        )
    else:
        logger.info(
            "cluster=%s has no kubecost_endpoint — cost data unavailable; "
            "usage-based anomaly detection will still run",
            cluster_name,
        )

    return ResolvedCluster(
        name=row["name"],
        api_server_url=row["api_server_url"],
        configuration=configuration,
        api_client=api_client,
        core_v1=core_v1,
        custom_objects=custom_objects,
        kubecost=kubecost,
        rbac_status=status,
        last_checked=row["rbac_last_checked"],
        tls_verified=tls_verified,
        _ca_tempfile=ca_tmp,
    )


def resolved_to_public(resolved: ResolvedCluster) -> dict[str, Any]:
    """Safe metadata dict (no tokens) for logging / tool envelopes."""
    return {
        "name": resolved.name,
        "api_server_url": resolved.api_server_url,
        "rbac_verified": True,
        "rbac_status": resolved.rbac_status,
        "last_checked": resolved.last_checked,
        "kubecost_configured": resolved.kubecost is not None,
        "tls_verified": resolved.tls_verified,
    }
