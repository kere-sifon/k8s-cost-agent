#!/usr/bin/env python3
"""MCP server: multi-cluster Kubernetes cost anomaly tools.

Resolves clusters via ``cluster_resolver.resolve_cluster_client`` (RBAC gate,
per-request kube Configuration) and runs the LangGraph supervisor-worker graph.
Never mutates cluster state. Never writes to the shared SQLite datastore.
"""

from __future__ import annotations

import logging
import os
import sys
import time
from functools import wraps
from typing import Any, Callable, TypeVar

import sentry_sdk
from dotenv import load_dotenv
from mcp.server.fastmcp import FastMCP
from sentry_sdk.integrations.mcp import MCPIntegration

from mcp_server.agent.graph import run_analysis
from mcp_server.cluster_resolver import (
    ClusterNotFoundError,
    ClusterNotVerifiedError,
    ClusterUnreachableError,
    assert_cluster_reachable,
    get_db_path,
    list_registered_clusters as _list_clusters,
    resolve_cluster_client,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    stream=sys.stderr,
    force=True,
)
logger = logging.getLogger("k8s-cost-mcp")

load_dotenv()


def _is_expected_client_error(exc: BaseException) -> bool:
    """True for registration/RBAC gate errors that should not page Sentry."""
    seen: BaseException | None = exc
    visited: set[int] = set()
    while seen is not None and id(seen) not in visited:
        visited.add(id(seen))
        if isinstance(seen, (ClusterNotFoundError, ClusterNotVerifiedError)):
            return True
        if isinstance(seen, ValueError):
            msg = str(seen)
            if "RBAC verification" in msg or "not registered" in msg:
                return True
        seen = seen.__cause__ or seen.__context__
    return False


def _sentry_before_send(event: dict[str, Any], hint: dict[str, Any]) -> dict[str, Any] | None:
    exc_info = hint.get("exc_info")
    if exc_info and _is_expected_client_error(exc_info[1]):
        return None
    return event


sentry_sdk.init(
    dsn=os.getenv("SENTRY_DSN") or None,
    environment=os.getenv("SENTRY_ENVIRONMENT", "local"),
    traces_sample_rate=float(os.getenv("SENTRY_TRACES_SAMPLE_RATE", "1.0")),
    integrations=[MCPIntegration()],
    # Keep False: tool args/results can include cluster/namespace metadata.
    # Tokens are never returned by tools, but we still avoid shipping payloads.
    send_default_pii=False,
    before_send=_sentry_before_send,
)

mcp = FastMCP(
    "k8s-cost",
    instructions=(
        "Kubernetes cost anomaly detection across registered clusters. "
        "Clusters must be registered AND RBAC-verified in the admin UI "
        "before they can be queried. Call list_registered_clusters first. "
        "Remediation is advisory text only — this server never mutates "
        "cluster state."
    ),
)

F = TypeVar("F", bound=Callable[..., Any])


def _log_tool(tool_name: str) -> Callable[[F], F]:
    def decorator(fn: F) -> F:
        @wraps(fn)
        def wrapper(*args: Any, **kwargs: Any) -> Any:
            cluster = kwargs.get("cluster", "-")
            started = time.perf_counter()
            sentry_sdk.set_tag("mcp.tool", tool_name)
            if cluster != "-":
                sentry_sdk.set_tag("cluster", str(cluster))
            logger.info("tool=%s cluster=%s event=started", tool_name, cluster)
            try:
                result = fn(*args, **kwargs)
            except Exception as exc:
                elapsed_ms = (time.perf_counter() - started) * 1000
                logger.error(
                    "tool=%s cluster=%s event=failed duration_ms=%.1f error=%s",
                    tool_name,
                    cluster,
                    elapsed_ms,
                    exc,
                )
                raise
            elapsed_ms = (time.perf_counter() - started) * 1000
            logger.info(
                "tool=%s cluster=%s event=succeeded duration_ms=%.1f",
                tool_name,
                cluster,
                elapsed_ms,
            )
            return result

        return wrapper  # type: ignore[return-value]

    return decorator


def _require_resolved(cluster: str):
    """
    Resolve + RBAC gate + live reachability probe.

    Map domain errors to ValueError for MCP clients. A registered-but-down
    cluster must fail loudly — never look like "no anomalies".
    """
    try:
        resolved = resolve_cluster_client(cluster)
        assert_cluster_reachable(resolved)
        return resolved
    except (
        ClusterNotFoundError,
        ClusterNotVerifiedError,
        ClusterUnreachableError,
    ) as exc:
        raise ValueError(str(exc)) from exc


def _raise_if_unreachable(result: dict[str, Any]) -> None:
    """Surface mid-run connectivity failures instead of an empty anomaly list."""
    for err in result.get("errors") or []:
        if "unreachable" in str(err).lower():
            raise ValueError(str(err))


@mcp.tool()
@_log_tool("list_registered_clusters")
def list_registered_clusters() -> list[dict[str, Any]]:
    """
    List clusters from the shared SQLite datastore.

    Returns name, api_server_url, rbac_verified, last_checked.
    Never returns ServiceAccount or Kubecost tokens.
    """
    return [
        {
            "name": c.name,
            "api_server_url": c.api_server_url,
            "rbac_verified": c.rbac_verified,
            "rbac_status": c.rbac_status,
            "last_checked": c.last_checked,
            "tls_verified": c.has_ca_cert,
        }
        for c in _list_clusters()
    ]


@mcp.tool()
@_log_tool("list_cost_anomalies")
def list_cost_anomalies(
    cluster: str,
    namespace: str | None = None,
    time_window: str = "24h",
) -> dict[str, Any]:
    """
    Detect cost anomalies on a registered, RBAC-verified cluster (live query).

    Builds a fresh kubernetes client for this request, runs the LangGraph
    supervisor → usage-analyzer → baseline-comparator → explainer pipeline.

    Args:
        cluster: Registered cluster name.
        namespace: Optional namespace filter.
        time_window: Window string (default "24h").

    Returns:
        anomalies: list of {id, namespace, resource, severity, description}
        (plus pattern/confidence fields when available)
    """
    resolved = _require_resolved(cluster)
    result = run_analysis(
        resolved=resolved,
        operation="list_anomalies",
        namespace=namespace,
        time_window=time_window or "24h",
    )
    _raise_if_unreachable(result)
    anomalies = []
    for a in result.get("anomalies") or []:
        anomalies.append(
            {
                "id": a.get("id"),
                "namespace": a.get("namespace"),
                "resource": a.get("resource"),
                "severity": a.get("severity"),
                "description": a.get("description") or a.get("evidence") or "",
                "pattern": a.get("pattern"),
                "confidence": a.get("confidence"),
            }
        )
    return {
        "cluster": resolved.name,
        "namespace": namespace,
        "time_window": time_window or "24h",
        "anomalies": anomalies,
        "summary": result.get("explanation") or "",
        "baseline_limitation": (result.get("baseline") or {}).get("limitation"),
        "errors": result.get("errors") or [],
    }


@mcp.tool()
@_log_tool("explain_anomaly")
def explain_anomaly(cluster: str, anomaly_id: str) -> dict[str, Any]:
    """
    Return the explainer worker's full plain-English explanation for an anomaly.

    Re-runs the live analysis graph targeted at anomaly_id. Does not mutate
    cluster state.

    Args:
        cluster: Registered cluster name.
        anomaly_id: Id from list_cost_anomalies.
    """
    resolved = _require_resolved(cluster)
    result = run_analysis(
        resolved=resolved,
        operation="explain",
        anomaly_id=anomaly_id,
    )
    _raise_if_unreachable(result)
    return {
        "cluster": resolved.name,
        "anomaly_id": anomaly_id,
        "explanation": result.get("explanation") or "",
        "confidence_note": result.get("confidence_note") or "",
        "errors": result.get("errors") or [],
    }


@mcp.tool()
@_log_tool("suggest_remediation")
def suggest_remediation(cluster: str, anomaly_id: str) -> dict[str, Any]:
    """
    Return advisory remediation text for an anomaly (never applied).

    Read-only / advisory only — no create, update, delete, or exec.

    Args:
        cluster: Registered cluster name.
        anomaly_id: Id from list_cost_anomalies.
    """
    resolved = _require_resolved(cluster)
    result = run_analysis(
        resolved=resolved,
        operation="remediate",
        anomaly_id=anomaly_id,
    )
    _raise_if_unreachable(result)
    return {
        "cluster": resolved.name,
        "anomaly_id": anomaly_id,
        "remediation": result.get("remediation") or "",
        "explanation": result.get("explanation") or "",
        "confidence_note": result.get("confidence_note") or "",
        "errors": result.get("errors") or [],
        "note": "Advisory only — no cluster mutation performed.",
    }


def main() -> None:
    try:
        logger.info("k8s-cost-mcp starting | db=%s", get_db_path())
    except Exception as exc:  # noqa: BLE001
        logger.error("Datastore configuration error: %s", exc)
        raise
    if os.getenv("SENTRY_DSN"):
        logger.info(
            "Sentry enabled | environment=%s",
            os.getenv("SENTRY_ENVIRONMENT", "local"),
        )
    else:
        logger.info("Sentry disabled (SENTRY_DSN unset)")
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
