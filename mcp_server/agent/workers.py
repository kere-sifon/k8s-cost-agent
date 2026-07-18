"""Worker nodes: usage-analyzer, baseline-comparator, explainer.

Each worker:
  1. Builds the documented INPUT JSON from live cluster data / prior state
  2. Calls Bedrock with the worker SYSTEM prompt (live Converse via bedrock_client)
  3. Falls back to a deterministic heuristic that emits the same OUTPUT shape
     on RuntimeError / other LLM failures
"""

from __future__ import annotations

import hashlib
import json
import logging
import operator
from datetime import datetime, timezone
from typing import Annotated, Any, TypedDict

from kubernetes.client.rest import ApiException

from mcp_server.agent.bedrock_client import invoke_haiku_json
from mcp_server.agent.kubecost_client import KubecostClient
from mcp_server.agent.prompts import (
    BASELINE_COMPARATOR_SYSTEM,
    EXPLAINER_SYSTEM,
    USAGE_ANALYZER_SYSTEM,
)
from mcp_server.cluster_resolver import ClusterUnreachableError

logger = logging.getLogger(__name__)

VALID_PATTERNS = frozenset(
    {
        "over_provisioned",
        "under_provisioned",
        "no_limits_set",
        "outlier_vs_peers",
        "orphaned_pvc",
    }
)


class GraphState(TypedDict):
    """Shared LangGraph state for one MCP tool invocation."""

    cluster_name: str
    namespace: str | None
    time_window: str
    anomaly_id: str | None
    operation: str  # list_anomalies | explain | remediate

    clients: dict[str, Any]

    # usage-analyzer I/O
    usage_input: dict[str, Any]
    candidates: Annotated[list[dict[str, Any]], operator.add]

    # baseline-comparator I/O
    peer_snapshot: list[dict[str, Any]]
    assessed_candidates: list[dict[str, Any]]
    baseline: dict[str, Any]

    # merged anomalies for MCP tools + explainer output
    anomalies: list[dict[str, Any]]
    explanation: str
    remediation: str
    confidence_note: str
    explainer_output: dict[str, Any]
    errors: Annotated[list[str], operator.add]

    usage_attempted: bool
    baseline_attempted: bool
    explain_attempted: bool
    next: str


# ── quantity helpers ──────────────────────────────────────────────────────────


def _parse_cpu(value: str | None) -> float:
    if not value:
        return 0.0
    if value.endswith("n"):
        return int(value[:-1]) / 1_000_000_000
    if value.endswith("u"):
        return int(value[:-1]) / 1_000_000
    if value.endswith("m"):
        return int(value[:-1]) / 1000
    return float(value)


def _parse_mem(value: str | None) -> float:
    if not value:
        return 0.0
    units = {
        "Ki": 1024,
        "Mi": 1024**2,
        "Gi": 1024**3,
        "Ti": 1024**4,
        "K": 1000,
        "M": 1000**2,
        "G": 1000**3,
    }
    for suffix, mult in units.items():
        if value.endswith(suffix):
            return float(value[: -len(suffix)]) * mult
    if value.endswith("i"):
        return float(value[:-1])
    return float(value)


def _fmt_cpu(cores: float) -> str:
    if cores <= 0:
        return "0"
    if cores < 1:
        return f"{int(round(cores * 1000))}m"
    return f"{cores:.3f}".rstrip("0").rstrip(".")


def _fmt_mem(nbytes: float) -> str:
    if nbytes <= 0:
        return "0"
    if nbytes >= 1024**3:
        return f"{nbytes / 1024**3:.2f}Gi"
    if nbytes >= 1024**2:
        return f"{nbytes / 1024**2:.0f}Mi"
    return f"{int(nbytes)}"


def _stable_id(namespace: str, resource: str, pattern: str) -> str:
    raw = f"{namespace}|{resource}|{pattern}"
    return hashlib.sha1(raw.encode()).hexdigest()[:12]


def _core(state: GraphState):
    return state["clients"]["core_v1"]


def _custom(state: GraphState):
    return state["clients"]["custom_objects"]


def _kubecost(state: GraphState) -> KubecostClient | None:
    return state["clients"].get("kubecost")


# ── live snapshot builders (read-only) ────────────────────────────────────────


def _owner_name(pod: Any) -> str | None:
    refs = pod.metadata.owner_references or []
    for ref in refs:
        if ref.controller:
            return f"{ref.kind}/{ref.name}"
    return None


def _sum_quantities(containers: list, field: str) -> tuple[str | None, str | None]:
    """Sum container requests or limits; return (cpu_str, mem_str) or (None, None)."""
    cpu_total = 0.0
    mem_total = 0.0
    any_set = False
    for c in containers or []:
        resources = c.resources
        if not resources:
            continue
        bag = getattr(resources, field, None) or {}
        if not bag:
            continue
        any_set = True
        cpu_total += _parse_cpu(bag.get("cpu"))
        mem_total += _parse_mem(bag.get("memory"))
    if not any_set:
        return None, None
    return _fmt_cpu(cpu_total), _fmt_mem(mem_total)


def _list_pod_metrics_map(state: GraphState, namespace: str | None) -> dict[tuple[str, str], dict[str, str]]:
    api = _custom(state)
    try:
        if namespace:
            raw = api.list_namespaced_custom_object(
                group="metrics.k8s.io",
                version="v1beta1",
                namespace=namespace,
                plural="pods",
            )
        else:
            raw = api.list_cluster_custom_object(
                group="metrics.k8s.io",
                version="v1beta1",
                plural="pods",
            )
    except ApiException as exc:
        logger.warning("metrics.k8s.io pods unavailable: %s", exc.reason)
        return {}
    except Exception as exc:  # noqa: BLE001
        logger.warning("metrics.k8s.io pods error: %s", exc)
        return {}

    out: dict[tuple[str, str], dict[str, str]] = {}
    for item in raw.get("items") or []:
        meta = item.get("metadata") or {}
        ns = meta.get("namespace") or ""
        name = meta.get("name") or ""
        cpu = 0.0
        mem = 0.0
        for c in item.get("containers") or []:
            usage = c.get("usage") or {}
            cpu += _parse_cpu(usage.get("cpu"))
            mem += _parse_mem(usage.get("memory"))
        out[(ns, name)] = {"cpu": _fmt_cpu(cpu), "memory": _fmt_mem(mem)}
    return out


def _list_unattached_pvcs(state: GraphState, namespace: str | None) -> list[dict[str, str]]:
    """PVCs not bound to a volume used by a running pod (best-effort)."""
    core = _core(state)
    try:
        if namespace:
            pvcs = core.list_namespaced_persistent_volume_claim(namespace)
            pods = core.list_namespaced_pod(namespace)
        else:
            pvcs = core.list_persistent_volume_claim_for_all_namespaces()
            pods = core.list_pod_for_all_namespaces()
    except ApiException as exc:
        logger.warning("list PVCs/pods for orphan check failed: %s", exc.reason)
        return []
    except Exception as exc:  # noqa: BLE001
        logger.warning("list PVCs/pods for orphan check error: %s", exc)
        return []

    claimed: set[tuple[str, str]] = set()
    for pod in pods.items:
        for vol in pod.spec.volumes or []:
            pvc = vol.persistent_volume_claim
            if pvc and pvc.claim_name:
                claimed.add((pod.metadata.namespace, pvc.claim_name))

    orphans: list[dict[str, str]] = []
    for pvc in pvcs.items:
        key = (pvc.metadata.namespace, pvc.metadata.name)
        if key in claimed:
            continue
        # Only flag Bound claims with no pod reference (orphaned storage cost)
        phase = (pvc.status.phase if pvc.status else None) or ""
        if phase and phase != "Bound":
            continue
        capacity = ""
        if pvc.status and pvc.status.capacity:
            capacity = pvc.status.capacity.get("storage", "") or ""
        orphans.append(
            {
                "namespace": pvc.metadata.namespace,
                "pvc_name": pvc.metadata.name,
                "capacity": capacity or "unknown",
            }
        )
    return orphans


def build_usage_analyzer_input(state: GraphState) -> dict[str, Any]:
    """
    Build the usage-analyzer INPUT JSON from live cluster reads.

    Shape:
      {cluster, namespace_filter, snapshot_timestamp, pods[], unattached_pvcs[]}
    """
    ns = state.get("namespace")
    core = _core(state)
    metrics = _list_pod_metrics_map(state, ns)

    pods_out: list[dict[str, Any]] = []
    cluster = state["cluster_name"]
    try:
        if ns:
            pod_list = core.list_namespaced_pod(ns)
        else:
            pod_list = core.list_pod_for_all_namespaces()
    except ApiException as exc:
        # Do NOT treat this as an empty cluster — that produces false "no anomalies".
        raise ClusterUnreachableError(
            f"Cluster '{cluster}' is registered but unreachable while listing pods: "
            f"HTTP {exc.status} {exc.reason}. Check API server connectivity and credentials."
        ) from exc
    except Exception as exc:  # noqa: BLE001
        raise ClusterUnreachableError(
            f"Cluster '{cluster}' is registered but unreachable while listing pods: {exc}. "
            "Check API server connectivity and credentials."
        ) from exc

    for pod in pod_list.items:
        req_cpu, req_mem = _sum_quantities(pod.spec.containers, "requests")
        lim_cpu, lim_mem = _sum_quantities(pod.spec.containers, "limits")
        usage = metrics.get((pod.metadata.namespace, pod.metadata.name), {})
        pods_out.append(
            {
                "namespace": pod.metadata.namespace,
                "pod_name": pod.metadata.name,
                "owner": _owner_name(pod),
                "requests": {
                    "cpu": req_cpu or "",
                    "memory": req_mem or "",
                },
                "limits": {
                    "cpu": lim_cpu or "",
                    "memory": lim_mem or "",
                },
                "usage": {
                    "cpu": usage.get("cpu", ""),
                    "memory": usage.get("memory", ""),
                },
            }
        )

    # PVC orphan check is best-effort (RBAC may deny PVCs); never mask a live cluster.
    try:
        unattached = _list_unattached_pvcs(state, ns)
    except Exception as exc:  # noqa: BLE001
        logger.warning("unattached PVC scan skipped: %s", exc)
        unattached = []

    return {
        "cluster": cluster,
        "namespace_filter": ns,
        "snapshot_timestamp": datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat(),
        "pods": pods_out,
        "unattached_pvcs": unattached,
    }


def build_peer_snapshot(usage_input: dict[str, Any]) -> list[dict[str, Any]]:
    """Group sibling pods by namespace+owner for baseline-comparator INPUT."""
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for pod in usage_input.get("pods") or []:
        owner = pod.get("owner") or f"Pod/{pod.get('pod_name')}"
        key = (pod.get("namespace") or "", owner)
        groups.setdefault(key, []).append(
            {
                "pod_name": pod.get("pod_name"),
                "usage": pod.get("usage") or {"cpu": "", "memory": ""},
            }
        )
    return [
        {"namespace": ns, "owner": owner, "sibling_pods": siblings}
        for (ns, owner), siblings in groups.items()
    ]


# ── heuristic fallbacks (same OUTPUT shapes as Bedrock) ───────────────────────


def _heuristic_usage_analyzer(usage_input: dict[str, Any]) -> dict[str, Any]:
    """Deterministic candidate detection when Bedrock is not wired."""
    candidates: list[dict[str, Any]] = []
    pods = usage_input.get("pods") or []

    # Peer CPU medians by owner for outlier detection
    by_owner: dict[tuple[str, str], list[float]] = {}
    for pod in pods:
        cpu = _parse_cpu((pod.get("usage") or {}).get("cpu"))
        owner = pod.get("owner") or ""
        if owner and cpu > 0:
            by_owner.setdefault((pod["namespace"], owner), []).append(cpu)

    for pod in pods:
        ns = pod["namespace"]
        name = pod["pod_name"]
        req_cpu = _parse_cpu((pod.get("requests") or {}).get("cpu"))
        lim_cpu = _parse_cpu((pod.get("limits") or {}).get("cpu"))
        use_cpu = _parse_cpu((pod.get("usage") or {}).get("cpu"))
        has_requests = bool(
            (pod.get("requests") or {}).get("cpu") or (pod.get("requests") or {}).get("memory")
        )
        has_limits = bool(
            (pod.get("limits") or {}).get("cpu") or (pod.get("limits") or {}).get("memory")
        )

        if not has_requests and not has_limits:
            pattern = "no_limits_set"
            candidates.append(
                {
                    "id": _stable_id(ns, name, pattern),
                    "namespace": ns,
                    "resource": name,
                    "pattern": pattern,
                    "severity": "medium",
                    "evidence": (
                        f"Pod {ns}/{name} has no CPU/memory requests or limits set "
                        "in the live snapshot."
                    ),
                }
            )
            continue

        if req_cpu > 0 and use_cpu > 0 and use_cpu / req_cpu < 0.2:
            pattern = "over_provisioned"
            candidates.append(
                {
                    "id": _stable_id(ns, name, pattern),
                    "namespace": ns,
                    "resource": name,
                    "pattern": pattern,
                    "severity": "high" if use_cpu / req_cpu < 0.1 else "medium",
                    "evidence": (
                        f"Pod {ns}/{name} requests {_fmt_cpu(req_cpu)} CPU but is using "
                        f"{_fmt_cpu(use_cpu)} ({use_cpu / req_cpu:.0%} of request)."
                    ),
                }
            )
        elif lim_cpu > 0 and use_cpu > 0 and use_cpu / lim_cpu >= 0.85:
            pattern = "under_provisioned"
            candidates.append(
                {
                    "id": _stable_id(ns, name, pattern),
                    "namespace": ns,
                    "resource": name,
                    "pattern": pattern,
                    "severity": "high",
                    "evidence": (
                        f"Pod {ns}/{name} is using {_fmt_cpu(use_cpu)} CPU against a "
                        f"limit of {_fmt_cpu(lim_cpu)} ({use_cpu / lim_cpu:.0%} of limit)."
                    ),
                }
            )
        elif req_cpu > 0 and use_cpu > 0 and use_cpu / req_cpu >= 2.0:
            pattern = "under_provisioned"
            candidates.append(
                {
                    "id": _stable_id(ns, name, pattern),
                    "namespace": ns,
                    "resource": name,
                    "pattern": pattern,
                    "severity": "medium",
                    "evidence": (
                        f"Pod {ns}/{name} usage {_fmt_cpu(use_cpu)} is well above request "
                        f"{_fmt_cpu(req_cpu)} ({use_cpu / req_cpu:.1f}x)."
                    ),
                }
            )

        owner = pod.get("owner") or ""
        peers = by_owner.get((ns, owner), [])
        if owner and len(peers) >= 3 and use_cpu > 0:
            sorted_peers = sorted(peers)
            median = sorted_peers[len(sorted_peers) // 2]
            if median > 0 and use_cpu / median >= 5.0:
                pattern = "outlier_vs_peers"
                candidates.append(
                    {
                        "id": _stable_id(ns, name, pattern),
                        "namespace": ns,
                        "resource": name,
                        "pattern": pattern,
                        "severity": "medium",
                        "evidence": (
                            f"Pod {ns}/{name} uses {_fmt_cpu(use_cpu)} CPU vs peer median "
                            f"{_fmt_cpu(median)} under {owner} (~{use_cpu / median:.1f}x)."
                        ),
                    }
                )

    for pvc in usage_input.get("unattached_pvcs") or []:
        pattern = "orphaned_pvc"
        ns = pvc["namespace"]
        name = pvc["pvc_name"]
        candidates.append(
            {
                "id": _stable_id(ns, name, pattern),
                "namespace": ns,
                "resource": name,
                "pattern": pattern,
                "severity": "low",
                "evidence": (
                    f"PVC {ns}/{name} (capacity {pvc.get('capacity', 'unknown')}) "
                    "has no referencing pod in the live snapshot."
                ),
            }
        )

    return {"candidates": candidates}


def _heuristic_baseline(
    candidates: list[dict[str, Any]],
    peer_snapshot: list[dict[str, Any]],
) -> dict[str, Any]:
    peers_by_ns_owner = {
        (p["namespace"], p["owner"]): p.get("sibling_pods") or [] for p in peer_snapshot
    }
    assessed: list[dict[str, Any]] = []
    for c in candidates:
        # Find peer group by matching resource name inside sibling lists
        note = "no peer data available for this workload"
        confidence = "medium"
        reasoning = (
            "Passed through without historical data; live peer-snapshot only "
            "(no persistent metrics store)."
        )
        for (ns, owner), siblings in peers_by_ns_owner.items():
            if ns != c.get("namespace"):
                continue
            names = {s.get("pod_name") for s in siblings}
            if c.get("resource") in names:
                if len(siblings) <= 1:
                    note = "no peer data available for this workload"
                    confidence = "low"
                    reasoning = (
                        "Singleton under its owner in this snapshot — cannot compare "
                        "to live peers; not dropped."
                    )
                else:
                    note = (
                        f"Compared against {len(siblings) - 1} sibling pod(s) under "
                        f"{owner} in the same live snapshot (not historical)."
                    )
                    if c.get("pattern") == "outlier_vs_peers":
                        confidence = "high"
                        reasoning = (
                            "Peer snapshot supports an outlier vs siblings right now; "
                            "still not a historical trend."
                        )
                    elif c.get("pattern") == "over_provisioned":
                        confidence = "medium"
                        reasoning = (
                            "Over-provisioning is based on request vs usage on this pod; "
                            "peers do not contradict it in the live snapshot."
                        )
                break
        assessed.append(
            {
                "id": c["id"],
                "confidence": confidence,
                "confidence_reasoning": reasoning,
                "peer_comparison_note": note,
            }
        )
    return {"assessed_candidates": assessed}


def _heuristic_explainer(
    *,
    cluster: str,
    candidate: dict[str, Any],
    kubecost_data: dict[str, Any] | None,
) -> dict[str, Any]:
    _ = kubecost_data  # do not invent dollar figures
    pattern = candidate.get("pattern", "unknown")
    ns = candidate.get("namespace", "")
    resource = candidate.get("resource", "")
    conf = candidate.get("confidence", "medium")
    evidence = candidate.get("evidence", "")
    conf_note = candidate.get("confidence_reasoning") or candidate.get(
        "peer_comparison_note", ""
    )

    conf_phrase = ""
    if conf == "low":
        conf_phrase = " Confidence is low given limited peer data — treat this as a lead, not a certainty."

    explanation = (
        f"On cluster {cluster}, {ns}/{resource} was flagged as {pattern}. "
        f"{evidence}.{conf_phrase}"
    )

    remediations = {
        "over_provisioned": (
            f"Review and lower CPU/memory requests on the owning workload for "
            f"{ns}/{resource} (e.g. edit the Deployment/StatefulSet resources.requests), "
            "then let a human apply the change after validating steady-state usage."
        ),
        "under_provisioned": (
            f"Raise resources.limits (and likely requests) on the owning workload for "
            f"{ns}/{resource} after confirming the spike is expected — suggestion only, "
            "do not apply automatically."
        ),
        "no_limits_set": (
            f"Add resources.requests and resources.limits to the container spec for "
            f"{ns}/{resource} (kubectl edit deploy/<owner> -n {ns} or patch the manifest)."
        ),
        "outlier_vs_peers": (
            f"Inspect {ns}/{resource} for hot shards or misconfig versus its replicas "
            f"(kubectl describe pod {resource} -n {ns}; compare logs/metrics to siblings)."
        ),
        "orphaned_pvc": (
            f"Confirm PVC {ns}/{resource} is unused, then delete it only after human review "
            f"(kubectl delete pvc {resource} -n {ns}) — advisory, not executed."
        ),
    }
    remediation = remediations.get(
        pattern,
        f"Inspect {ns}/{resource} on {cluster} and adjust resources after human review.",
    )

    return {
        "id": candidate["id"],
        "explanation": explanation.strip(),
        "remediation": remediation,
        "confidence_note": conf_note or f"confidence={conf}",
    }


# ── workers ───────────────────────────────────────────────────────────────────


def usage_analyzer(state: GraphState) -> dict[str, Any]:
    """
    Worker 1 — live snapshot → candidate anomalies.

    INPUT:  see prompts / build_usage_analyzer_input()
    OUTPUT: {"candidates": [{id, namespace, resource, pattern, severity, evidence}, ...]}
    """
    try:
        usage_input = build_usage_analyzer_input(state)
    except ClusterUnreachableError as exc:
        logger.error("usage_analyzer | snapshot failed: %s", exc)
        # Short-circuit the graph: do not invent an empty "no anomalies" result.
        return {
            "usage_input": {
                "cluster": state["cluster_name"],
                "namespace_filter": state.get("namespace"),
                "pods": [],
                "unattached_pvcs": [],
                "error": str(exc),
            },
            "candidates": [],
            "anomalies": [],
            "usage_attempted": True,
            "baseline_attempted": True,
            "explain_attempted": True,
            "errors": [str(exc)],
            "explanation": str(exc),
            "remediation": "",
        }

    logger.info(
        "usage_analyzer | cluster=%s pods=%d pvcs=%d",
        usage_input["cluster"],
        len(usage_input.get("pods") or []),
        len(usage_input.get("unattached_pvcs") or []),
    )

    user_payload = json.dumps(usage_input, indent=2)
    try:
        result = invoke_haiku_json(system=USAGE_ANALYZER_SYSTEM, user=user_payload)
        candidates = _normalize_candidates(result.get("candidates") or [])
        source = "bedrock"
    except (NotImplementedError, RuntimeError):
        result = _heuristic_usage_analyzer(usage_input)
        candidates = _normalize_candidates(result.get("candidates") or [])
        source = "heuristic"
        logger.info(
            "usage_analyzer | Bedrock unavailable — using heuristic (%d candidates)",
            len(candidates),
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("usage_analyzer Bedrock/parse failed; falling back to heuristic")
        result = _heuristic_usage_analyzer(usage_input)
        candidates = _normalize_candidates(result.get("candidates") or [])
        source = "heuristic_fallback"
        return {
            "usage_input": usage_input,
            "candidates": candidates,
            "usage_attempted": True,
            "errors": [f"usage_analyzer LLM error: {exc}"],
        }

    # Stash kubecost for later explainer (optional; never invent $)
    kc = _kubecost(state)
    if kc is not None:
        usage_input = {
            **usage_input,
            "_kubecost": kc.fetch_allocation(
                window=state.get("time_window") or "24h",
                namespace=state.get("namespace"),
            ),
        }
    else:
        usage_input = {**usage_input, "_kubecost": None}

    logger.info("usage_analyzer | source=%s candidates=%d", source, len(candidates))
    return {
        "usage_input": usage_input,
        "candidates": candidates,
        "usage_attempted": True,
    }


def _normalize_candidates(raw: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for c in raw:
        pattern = c.get("pattern") or "over_provisioned"
        if pattern not in VALID_PATTERNS:
            pattern = "over_provisioned"
        ns = c.get("namespace") or ""
        resource = c.get("resource") or ""
        # Always deterministic — never trust Bedrock's invented "id" field.
        cid = _stable_id(ns, resource, pattern)
        out.append(
            {
                "id": cid,
                "namespace": ns,
                "resource": resource,
                "pattern": pattern,
                "severity": c.get("severity") or "medium",
                "evidence": c.get("evidence") or "",
            }
        )
    return out


def baseline_comparator(state: GraphState) -> dict[str, Any]:
    """
    Worker 2 — peer-snapshot confidence assessment (not historical).

    INPUT:  {cluster, candidates, peer_snapshot}
    OUTPUT: {"assessed_candidates": [{id, confidence, confidence_reasoning, peer_comparison_note}]}
    """
    candidates = list(state.get("candidates") or [])
    usage_input = state.get("usage_input") or {}
    peer_snapshot = build_peer_snapshot(usage_input)
    payload = {
        "cluster": state["cluster_name"],
        "candidates": candidates,
        "peer_snapshot": peer_snapshot,
    }
    logger.info(
        "baseline_comparator | candidates=%d peer_groups=%d",
        len(candidates),
        len(peer_snapshot),
    )

    try:
        result = invoke_haiku_json(
            system=BASELINE_COMPARATOR_SYSTEM,
            user=json.dumps(payload, indent=2),
        )
        assessed = result.get("assessed_candidates") or []
    except (NotImplementedError, RuntimeError):
        result = _heuristic_baseline(candidates, peer_snapshot)
        assessed = result.get("assessed_candidates") or []
        logger.info("baseline_comparator | Bedrock unavailable — using heuristic")
    except Exception as exc:  # noqa: BLE001
        logger.exception("baseline_comparator failed; heuristic fallback")
        result = _heuristic_baseline(candidates, peer_snapshot)
        assessed = result.get("assessed_candidates") or []
        return {
            "peer_snapshot": peer_snapshot,
            "assessed_candidates": assessed,
            "baseline": {
                "method": "peer-snapshot-live",
                "limitation": (
                    "No persistent metrics between queries — peer-snapshot approximation only."
                ),
                "assessed_candidates": assessed,
            },
            "anomalies": _merge_anomalies(candidates, assessed),
            "baseline_attempted": True,
            "errors": [f"baseline_comparator LLM error: {exc}"],
        }

    assessed = _normalize_assessed(assessed, candidates)
    anomalies = _merge_anomalies(candidates, assessed)
    baseline = {
        "method": "peer-snapshot-live",
        "limitation": (
            "No persistent metrics storage / no scheduled jobs — baseline is a live "
            "peer-snapshot approximation, not a historical time series."
        ),
        "assessed_candidates": assessed,
    }
    return {
        "peer_snapshot": peer_snapshot,
        "assessed_candidates": assessed,
        "baseline": baseline,
        "anomalies": anomalies,
        "baseline_attempted": True,
    }


def _normalize_assessed(
    assessed: list[dict[str, Any]], candidates: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    by_id = {c["id"]: c for c in assessed if c.get("id")}
    out: list[dict[str, Any]] = []
    for c in candidates:
        a = by_id.get(c["id"], {})
        out.append(
            {
                "id": c["id"],
                "confidence": a.get("confidence") or "medium",
                "confidence_reasoning": a.get("confidence_reasoning")
                or "Passed through (no historical baseline available).",
                "peer_comparison_note": a.get("peer_comparison_note")
                or "no peer data available for this workload",
            }
        )
    return out


def _merge_anomalies(
    candidates: list[dict[str, Any]], assessed: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """MCP-facing anomaly list: analyzer fields + confidence."""
    by_id = {a["id"]: a for a in assessed}
    merged: list[dict[str, Any]] = []
    for c in candidates:
        a = by_id.get(c["id"], {})
        merged.append(
            {
                "id": c["id"],
                "namespace": c.get("namespace"),
                "resource": c.get("resource"),
                "severity": c.get("severity"),
                "description": c.get("evidence") or "",
                "pattern": c.get("pattern"),
                "evidence": c.get("evidence") or "",
                "confidence": a.get("confidence"),
                "confidence_reasoning": a.get("confidence_reasoning"),
                "peer_comparison_note": a.get("peer_comparison_note"),
            }
        )
    return merged


def _find_merged_candidate(state: GraphState, anomaly_id: str) -> dict[str, Any] | None:
    for a in state.get("anomalies") or []:
        if a.get("id") == anomaly_id:
            return a
    # Fallback: merge on the fly from candidates + assessed
    cand = next((c for c in (state.get("candidates") or []) if c.get("id") == anomaly_id), None)
    if not cand:
        return None
    assessed = next(
        (a for a in (state.get("assessed_candidates") or []) if a.get("id") == anomaly_id),
        {},
    )
    return {**cand, **assessed, "description": cand.get("evidence", "")}


def explainer(state: GraphState) -> dict[str, Any]:
    """
    Worker 3 — plain-English explanation + single remediation suggestion.

    INPUT:  {cluster, candidate (merged), kubecost_data|null}
    OUTPUT: {id, explanation, remediation, confidence_note}

    Never mutates cluster state. Never invents dollar costs.
    """
    operation = state.get("operation", "list_anomalies")
    anomaly_id = state.get("anomaly_id")
    logger.info("explainer | op=%s anomaly_id=%s", operation, anomaly_id)

    # list_anomalies: short rollup without per-item Bedrock calls
    if operation == "list_anomalies" and not anomaly_id:
        anomalies = state.get("anomalies") or []
        n = len(anomalies)
        limitation = (state.get("baseline") or {}).get("limitation", "")
        explanation = (
            f"Found {n} candidate anomal{'y' if n == 1 else 'ies'} on "
            f"{state['cluster_name']} from the live usage snapshot. "
            f"{limitation}"
        )
        return {
            "explanation": explanation,
            "remediation": "",
            "confidence_note": "",
            "explainer_output": {"rollup": True, "count": n},
            "explain_attempted": True,
        }

    if not anomaly_id:
        return {
            "explanation": "",
            "remediation": "",
            "confidence_note": "",
            "explainer_output": {},
            "explain_attempted": True,
            "errors": ["anomaly_id is required for explain/remediate"],
        }

    candidate = _find_merged_candidate(state, anomaly_id)
    if candidate is None:
        return {
            "explanation": "",
            "remediation": "",
            "confidence_note": "",
            "explainer_output": {},
            "explain_attempted": True,
            "errors": [f"anomaly_id not found: {anomaly_id}"],
        }

    usage_input = state.get("usage_input") or {}
    kubecost_data = usage_input.get("_kubecost")
    payload = {
        "cluster": state["cluster_name"],
        "candidate": candidate,
        "kubecost_data": kubecost_data,
    }

    try:
        result = invoke_haiku_json(
            system=EXPLAINER_SYSTEM,
            user=json.dumps(payload, indent=2),
        )
    except (NotImplementedError, RuntimeError):
        result = _heuristic_explainer(
            cluster=state["cluster_name"],
            candidate=candidate,
            kubecost_data=kubecost_data if isinstance(kubecost_data, dict) else None,
        )
        logger.info("explainer | Bedrock unavailable — using heuristic")
    except Exception as exc:  # noqa: BLE001
        logger.exception("explainer failed; heuristic fallback")
        result = _heuristic_explainer(
            cluster=state["cluster_name"],
            candidate=candidate,
            kubecost_data=kubecost_data if isinstance(kubecost_data, dict) else None,
        )
        return {
            "explanation": result.get("explanation", ""),
            "remediation": result.get("remediation", ""),
            "confidence_note": result.get("confidence_note", ""),
            "explainer_output": result,
            "explain_attempted": True,
            "errors": [f"explainer LLM error: {exc}"],
        }

    result = {
        "id": result.get("id") or anomaly_id,
        "explanation": result.get("explanation") or "",
        "remediation": result.get("remediation") or "",
        "confidence_note": result.get("confidence_note")
        or candidate.get("confidence_reasoning")
        or "",
    }
    return {
        "explanation": result["explanation"],
        "remediation": result["remediation"],
        "confidence_note": result["confidence_note"],
        "explainer_output": result,
        "explain_attempted": True,
    }
