"""Kubecost HTTP API wrapper (per-cluster, optional).

When a cluster has no kubecost_endpoint, tools degrade gracefully:
usage-based anomaly detection still runs via metrics.k8s.io.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


class KubecostClient:
    """Thin client around a single cluster's Kubecost endpoint."""

    def __init__(
        self,
        endpoint: str,
        token: str | None = None,
        *,
        timeout: float = 30.0,
    ) -> None:
        self.endpoint = endpoint.rstrip("/")
        self.token = token
        self.timeout = timeout

    def fetch_allocation(
        self,
        *,
        window: str = "24h",
        namespace: str | None = None,
        aggregate: str = "namespace",
    ) -> dict[str, Any]:
        """
        Fetch cost allocation for the given window.

        Expected input:
            window:    Kubecost window string (e.g. "24h", "7d")
            namespace: optional filter
            aggregate: aggregation key (default "namespace")

        Expected output:
            {
              "status": "ok" | "todo" | "error",
              "window": str,
              "namespace": str | None,
              "allocations": [ { "name": str, "totalCost": float, ... }, ... ],
              "raw": optional raw API payload
            }

        TODO(Kubecost):
            GET {endpoint}/model/allocation?window=...&aggregate=...
            Header: Authorization: Bearer {token} when token is set
            Parse JSON into the shape above.
            Use httpx/requests with self.timeout.
        """
        logger.info(
            "TODO(Kubecost): fetch_allocation endpoint=%s window=%s namespace=%s",
            self.endpoint,
            window,
            namespace,
        )
        return {
            "status": "todo",
            "window": window,
            "namespace": namespace,
            "aggregate": aggregate,
            "allocations": [],
            "note": "Kubecost HTTP call not yet implemented",
        }
