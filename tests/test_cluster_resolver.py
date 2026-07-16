"""Tests for mcp_server.cluster_resolver.resolve_cluster_client."""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from mcp_server.cluster_resolver import (
    ClusterNotFoundError,
    ClusterNotVerifiedError,
    list_registered_clusters,
    resolve_cluster_client,
)

FAKE_CA_PEM = """-----BEGIN CERTIFICATE-----
MIIBkTCB+wIJAKHBfJYnEXAMPLE
-----END CERTIFICATE-----
"""

SCHEMA = """
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
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


@pytest.fixture()
def db_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "clusters.db"
    conn = sqlite3.connect(str(path))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.executescript(SCHEMA)
    now = _now()
    conn.execute(
        """
        INSERT INTO clusters (
            name, api_server_url, sa_token, kubecost_url, kubecost_token,
            ca_cert_pem, rbac_status, rbac_last_checked, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "prod-us-east",
            "https://kube.example.com:6443",
            "fake-sa-token-abc",
            "http://kubecost.example:9090",
            "fake-kc-token",
            None,
            "verified",
            now,
            now,
            now,
        ),
    )
    conn.execute(
        """
        INSERT INTO clusters (
            name, api_server_url, sa_token, kubecost_url, kubecost_token,
            ca_cert_pem, rbac_status, rbac_last_checked, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "unverified-lab",
            "https://lab.example.com:6443",
            "fake-sa-token-lab",
            None,
            None,
            None,
            "not_verified",
            None,
            now,
            now,
        ),
    )
    conn.execute(
        """
        INSERT INTO clusters (
            name, api_server_url, sa_token, kubecost_url, kubecost_token,
            ca_cert_pem, rbac_status, rbac_last_checked, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "secure-prod",
            "https://secure.example.com:6443",
            "fake-sa-token-secure",
            None,
            None,
            FAKE_CA_PEM,
            "verified",
            now,
            now,
            now,
        ),
    )
    conn.commit()
    conn.close()

    monkeypatch.setenv("K8S_COST_AGENT_DB_PATH", str(path))
    return path


def test_resolve_verified_cluster_builds_configuration(db_path: Path) -> None:
    resolved = resolve_cluster_client("prod-us-east")

    assert resolved.name == "prod-us-east"
    assert resolved.api_server_url == "https://kube.example.com:6443"
    assert resolved.configuration.host == "https://kube.example.com:6443"
    assert resolved.configuration.api_key["authorization"] == "fake-sa-token-abc"
    assert resolved.configuration.api_key_prefix["authorization"] == "Bearer"
    assert resolved.api_client is not None
    assert resolved.core_v1 is not None
    assert resolved.custom_objects is not None
    assert resolved.kubecost is not None
    assert resolved.kubecost.endpoint == "http://kubecost.example:9090"
    # No CA → insecure fallback
    assert resolved.configuration.verify_ssl is False
    assert resolved.tls_verified is False


def test_resolve_without_ca_logs_warning(
    db_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    with caplog.at_level(logging.WARNING, logger="mcp_server.cluster_resolver"):
        resolve_cluster_client("prod-us-east")
    assert any(
        "registered without a CA cert" in r.message and "prod-us-east" in r.message
        for r in caplog.records
    )
    assert any("TLS verification disabled" in r.message for r in caplog.records)


def test_resolve_with_ca_enables_tls_verification(db_path: Path) -> None:
    resolved = resolve_cluster_client("secure-prod")
    assert resolved.tls_verified is True
    assert resolved.configuration.verify_ssl is True
    assert resolved.configuration.ssl_ca_cert
    assert Path(resolved.configuration.ssl_ca_cert).is_file()
    # Tempfile kept alive on the resolved object
    assert resolved._ca_tempfile is not None


def test_resolve_refuses_unverified_cluster(db_path: Path) -> None:
    with pytest.raises(ClusterNotVerifiedError) as excinfo:
        resolve_cluster_client("unverified-lab")

    msg = str(excinfo.value)
    assert "unverified-lab" in msg
    assert "RBAC verification" in msg
    assert "admin UI" in msg


def test_resolve_missing_cluster(db_path: Path) -> None:
    with pytest.raises(ClusterNotFoundError) as excinfo:
        resolve_cluster_client("does-not-exist")
    assert "does-not-exist" in str(excinfo.value)


def test_list_registered_clusters_omits_tokens(db_path: Path) -> None:
    clusters = list_registered_clusters()
    names = {c.name for c in clusters}
    assert names == {"prod-us-east", "unverified-lab", "secure-prod"}

    verified = next(c for c in clusters if c.name == "prod-us-east")
    assert verified.rbac_verified is True
    assert verified.has_ca_cert is False

    secure = next(c for c in clusters if c.name == "secure-prod")
    assert secure.has_ca_cert is True

    for c in clusters:
        assert not hasattr(c, "sa_token")
        assert not hasattr(c, "kubecost_token")


def test_two_resolves_are_independent_clients(db_path: Path) -> None:
    a = resolve_cluster_client("prod-us-east")
    b = resolve_cluster_client("prod-us-east")
    assert a.api_client is not b.api_client
    assert a.configuration is not b.configuration


def test_db_path_required(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("K8S_COST_AGENT_DB_PATH", raising=False)
    with pytest.raises(RuntimeError, match="K8S_COST_AGENT_DB_PATH"):
        resolve_cluster_client("anything")
