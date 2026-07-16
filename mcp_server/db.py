"""Deprecated: use ``mcp_server.cluster_resolver`` instead."""

from mcp_server.cluster_resolver import (
    get_db_path,
    list_registered_clusters,
    resolve_cluster_client,
)

# Back-compat aliases used by earlier scaffold
list_clusters = list_registered_clusters


def resolve_cluster(cluster: str):
    """Legacy helper — prefer resolve_cluster_client (includes RBAC gate)."""
    return resolve_cluster_client(cluster)


__all__ = [
    "get_db_path",
    "list_clusters",
    "list_registered_clusters",
    "resolve_cluster",
    "resolve_cluster_client",
]
