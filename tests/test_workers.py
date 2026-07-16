"""Unit tests for worker I/O shapes and heuristic fallbacks (no Bedrock / no cluster)."""

from __future__ import annotations

from mcp_server.agent.bedrock_client import parse_json_object
from mcp_server.agent.prompts import (
    BASELINE_COMPARATOR_SYSTEM,
    EXPLAINER_SYSTEM,
    USAGE_ANALYZER_SYSTEM,
)
from mcp_server.agent.workers import (
    _heuristic_baseline,
    _heuristic_explainer,
    _heuristic_usage_analyzer,
    build_peer_snapshot,
)


def test_prompts_mention_key_contracts() -> None:
    assert "over_provisioned" in USAGE_ANALYZER_SYSTEM
    assert "peer-snapshot" in BASELINE_COMPARATOR_SYSTEM or "peer" in BASELINE_COMPARATOR_SYSTEM.lower()
    assert "Never suggest applying the change automatically" in EXPLAINER_SYSTEM


def test_parse_json_object_fenced() -> None:
    text = 'Here you go:\n```json\n{"candidates": []}\n```\n'
    assert parse_json_object(text) == {"candidates": []}


def test_normalize_candidates_discards_bedrock_id() -> None:
    """Ids must be a pure function of (namespace, resource, pattern)."""
    from mcp_server.agent.workers import _normalize_candidates, _stable_id

    invented_a = [
        {
            "id": "coredns-cpu-limit-zero",
            "namespace": "kube-system",
            "resource": "coredns-abc",
            "pattern": "under_provisioned",
            "severity": "high",
            "evidence": "first run prose",
        }
    ]
    invented_b = [
        {
            "id": "coredns-invalid-limit",
            "namespace": "kube-system",
            "resource": "coredns-abc",
            "pattern": "under_provisioned",
            "severity": "medium",
            "evidence": "second run different prose",
        }
    ]
    out_a = _normalize_candidates(invented_a)
    out_b = _normalize_candidates(invented_b)
    expected = _stable_id("kube-system", "coredns-abc", "under_provisioned")
    assert out_a[0]["id"] == expected
    assert out_b[0]["id"] == expected
    assert out_a[0]["id"] == out_b[0]["id"]
    assert out_a[0]["id"] not in {
        "coredns-cpu-limit-zero",
        "coredns-invalid-limit",
    }

    usage_input = {
        "cluster": "lab",
        "namespace_filter": "default",
        "snapshot_timestamp": "2026-07-14T00:00:00+00:00",
        "pods": [
            {
                "namespace": "default",
                "pod_name": "api-0",
                "owner": "Deployment/api",
                "requests": {"cpu": "2", "memory": "1Gi"},
                "limits": {"cpu": "2", "memory": "1Gi"},
                "usage": {"cpu": "100m", "memory": "100Mi"},
            },
            {
                "namespace": "default",
                "pod_name": "bare-pod",
                "owner": None,
                "requests": {"cpu": "", "memory": ""},
                "limits": {"cpu": "", "memory": ""},
                "usage": {"cpu": "50m", "memory": "50Mi"},
            },
        ],
        "unattached_pvcs": [
            {"namespace": "default", "pvc_name": "orphan-data", "capacity": "10Gi"}
        ],
    }
    result = _heuristic_usage_analyzer(usage_input)
    assert "candidates" in result
    patterns = {c["pattern"] for c in result["candidates"]}
    assert "over_provisioned" in patterns
    assert "no_limits_set" in patterns
    assert "orphaned_pvc" in patterns
    for c in result["candidates"]:
        assert set(c.keys()) >= {
            "id",
            "namespace",
            "resource",
            "pattern",
            "severity",
            "evidence",
        }


def test_heuristic_baseline_and_peer_snapshot() -> None:
    usage_input = {
        "pods": [
            {
                "namespace": "default",
                "pod_name": "api-0",
                "owner": "Deployment/api",
                "usage": {"cpu": "100m", "memory": "100Mi"},
            },
            {
                "namespace": "default",
                "pod_name": "api-1",
                "owner": "Deployment/api",
                "usage": {"cpu": "110m", "memory": "100Mi"},
            },
        ]
    }
    peers = build_peer_snapshot(usage_input)
    assert len(peers) == 1
    assert peers[0]["owner"] == "Deployment/api"
    assert len(peers[0]["sibling_pods"]) == 2

    candidates = [
        {
            "id": "abc",
            "namespace": "default",
            "resource": "api-0",
            "pattern": "over_provisioned",
            "severity": "medium",
            "evidence": "test",
        }
    ]
    assessed = _heuristic_baseline(candidates, peers)["assessed_candidates"]
    assert len(assessed) == 1
    assert assessed[0]["id"] == "abc"
    assert assessed[0]["confidence"] in {"low", "medium", "high"}
    assert "peer_comparison_note" in assessed[0]
    assert "historical" in assessed[0]["confidence_reasoning"].lower() or "live" in assessed[0][
        "confidence_reasoning"
    ].lower()


def test_heuristic_explainer_no_invented_dollars() -> None:
    candidate = {
        "id": "abc",
        "namespace": "default",
        "resource": "api-0",
        "pattern": "over_provisioned",
        "severity": "medium",
        "evidence": "requests 2 CPU, using 100m",
        "confidence": "low",
        "confidence_reasoning": "Singleton-ish",
        "peer_comparison_note": "no peer data available for this workload",
    }
    out = _heuristic_explainer(cluster="lab", candidate=candidate, kubecost_data=None)
    assert out["id"] == "abc"
    assert out["explanation"]
    assert out["remediation"]
    assert "confidence_note" in out
    assert "$" not in out["explanation"]
    assert "$" not in out["remediation"]
    assert "low" in out["explanation"].lower() or "Confidence is low" in out["explanation"]
