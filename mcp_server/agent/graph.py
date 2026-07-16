"""LangGraph supervisor-worker graph for cost anomaly analysis.

Flow:
  START → supervisor → usage_analyzer → supervisor
                     → baseline_comparator → supervisor
                     → explainer → supervisor   (explain / remediate always;
                                                list_anomalies gets a short rollup)
                     → END
"""

from __future__ import annotations

import logging
from typing import Any

from langgraph.graph import END, StateGraph

from mcp_server.agent.workers import (
    GraphState,
    baseline_comparator,
    explainer,
    usage_analyzer,
)
from mcp_server.cluster_resolver import ResolvedCluster

logger = logging.getLogger(__name__)


def supervisor(state: GraphState) -> dict[str, str]:
    """Route to the next worker based on attempted flags and operation."""
    op = state.get("operation", "list_anomalies")
    usage_attempted = state.get("usage_attempted", False)
    baseline_attempted = state.get("baseline_attempted", False)
    explain_attempted = state.get("explain_attempted", False)

    if not usage_attempted:
        decision = "analyze_usage"
    elif not baseline_attempted:
        decision = "compare_baseline"
    elif not explain_attempted:
        decision = "explain"
    else:
        decision = "END"

    logger.info(
        "supervisor | op=%s decision=%s usage=%s baseline=%s explain=%s",
        op,
        decision,
        usage_attempted,
        baseline_attempted,
        explain_attempted,
    )
    return {"next": decision}


def _route(state: GraphState) -> str:
    return state.get("next", "END")


def build_graph():
    graph = StateGraph(GraphState)
    graph.add_node("supervisor", supervisor)
    graph.add_node("analyze_usage", usage_analyzer)
    graph.add_node("compare_baseline", baseline_comparator)
    graph.add_node("explain", explainer)

    graph.set_entry_point("supervisor")
    graph.add_conditional_edges(
        "supervisor",
        _route,
        {
            "analyze_usage": "analyze_usage",
            "compare_baseline": "compare_baseline",
            "explain": "explain",
            "END": END,
        },
    )
    graph.add_edge("analyze_usage", "supervisor")
    graph.add_edge("compare_baseline", "supervisor")
    graph.add_edge("explain", "supervisor")
    return graph.compile()


def _clients_dict(resolved: ResolvedCluster) -> dict[str, Any]:
    return {
        "core_v1": resolved.core_v1,
        "custom_objects": resolved.custom_objects,
        "api_client": resolved.api_client,
        "configuration": resolved.configuration,
        "kubecost": resolved.kubecost,
    }


def run_analysis(
    *,
    resolved: ResolvedCluster,
    operation: str,
    namespace: str | None = None,
    time_window: str = "24h",
    anomaly_id: str | None = None,
) -> dict[str, Any]:
    """
    Run one analysis against a resolved (verified) cluster.

    ``resolved`` must come from ``resolve_cluster_client`` for this request.
    """
    logger.info(
        "run_analysis | cluster=%s op=%s ns=%s window=%s anomaly_id=%s",
        resolved.name,
        operation,
        namespace,
        time_window,
        anomaly_id,
    )
    app = build_graph()
    initial: GraphState = {
        "cluster_name": resolved.name,
        "namespace": namespace,
        "time_window": time_window or "24h",
        "anomaly_id": anomaly_id,
        "operation": operation,
        "clients": _clients_dict(resolved),
        "usage_input": {},
        "candidates": [],
        "peer_snapshot": [],
        "assessed_candidates": [],
        "baseline": {},
        "anomalies": [],
        "explanation": "",
        "remediation": "",
        "confidence_note": "",
        "explainer_output": {},
        "errors": [],
        "usage_attempted": False,
        "baseline_attempted": False,
        "explain_attempted": False,
        "next": "",
    }
    return app.invoke(initial, config={"recursion_limit": 25})
