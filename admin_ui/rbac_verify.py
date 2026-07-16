"""Read-only RBAC verification against a registered cluster.

Used by the admin UI verify action and by ``scripts/verify_rbac.py``.
Performs SelfSubjectAccessReview-style checks via the Authorization API
for the verbs/resources granted in rbac/cost-agent-readonly.yaml.

Never attempts create/update/delete/exec — those are out of scope and
must remain denied.
"""

from __future__ import annotations

import logging
import tempfile
from dataclasses import dataclass, field

from kubernetes import client
from kubernetes.client.rest import ApiException

from admin_ui.db import ClusterSecrets, get_cluster_secrets, update_rbac_status

logger = logging.getLogger(__name__)

# Mirrors rbac/cost-agent-readonly.yaml — keep in sync.
REQUIRED_CHECKS: list[tuple[str, str, str]] = [
    # (group, resource, verb)
    ("", "pods", "get"),
    ("", "pods", "list"),
    ("", "nodes", "get"),
    ("", "nodes", "list"),
    ("", "namespaces", "get"),
    ("", "namespaces", "list"),
    ("", "resourcequotas", "list"),
    ("", "limitranges", "list"),
    ("metrics.k8s.io", "pods", "list"),
    ("metrics.k8s.io", "nodes", "list"),
]

# Sanity: these MUST remain denied. A "allowed" result here is a hard failure.
FORBIDDEN_CHECKS: list[tuple[str, str, str]] = [
    ("", "pods", "create"),
    ("", "pods", "delete"),
    ("", "pods", "update"),
    ("", "pods/exec", "create"),
    ("", "secrets", "get"),
    ("", "secrets", "list"),
]


@dataclass
class CheckResult:
    group: str
    resource: str
    verb: str
    allowed: bool
    reason: str = ""


@dataclass
class VerifyResult:
    ok: bool
    status: str  # verified | error
    detail: str
    required: list[CheckResult] = field(default_factory=list)
    forbidden: list[CheckResult] = field(default_factory=list)


def build_api_client(
    api_server_url: str,
    token: str,
    *,
    ca_cert_pem: str | None = None,
    cluster_name: str = "unknown",
) -> client.ApiClient:
    """Build a per-request kubernetes Configuration — never a shared global client."""
    configuration = client.Configuration(
        host=api_server_url.rstrip("/"),
        api_key={"authorization": token},
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
        api_client = client.ApiClient(configuration)
        # Keep tempfile open for the lifetime of this ApiClient usage in-process.
        api_client._ca_tempfile = tmp  # type: ignore[attr-defined]
        return api_client

    logger.warning(
        "cluster '%s' registered without a CA cert — TLS verification "
        "disabled, insecure. Add a CA cert in the admin UI to fix this.",
        cluster_name,
    )
    configuration.verify_ssl = False
    return client.ApiClient(configuration)


def _ssar(
    auth_api: client.AuthorizationV1Api,
    group: str,
    resource: str,
    verb: str,
) -> CheckResult:
    body = client.V1SelfSubjectAccessReview(
        spec=client.V1SelfSubjectAccessReviewSpec(
            resource_attributes=client.V1ResourceAttributes(
                group=group,
                resource=resource,
                verb=verb,
            )
        )
    )
    try:
        resp = auth_api.create_self_subject_access_review(body)
        allowed = bool(resp.status.allowed)
        reason = (resp.status.reason or "") if resp.status else ""
    except ApiException as exc:
        return CheckResult(
            group=group,
            resource=resource,
            verb=verb,
            allowed=False,
            reason=f"API error: {exc.status} {exc.reason}",
        )
    except Exception as exc:  # noqa: BLE001 — surface any client/network failure
        return CheckResult(
            group=group,
            resource=resource,
            verb=verb,
            allowed=False,
            reason=f"client error: {exc}",
        )
    return CheckResult(
        group=group, resource=resource, verb=verb, allowed=allowed, reason=reason
    )


def verify_cluster_credentials(secrets: ClusterSecrets) -> VerifyResult:
    """Run required + forbidden SelfSubjectAccessReviews for one cluster."""
    api_client = build_api_client(
        secrets.api_server_url,
        secrets.sa_token,
        ca_cert_pem=secrets.ca_cert_pem,
        cluster_name=secrets.name,
    )
    auth_api = client.AuthorizationV1Api(api_client)

    required = [_ssar(auth_api, g, r, v) for g, r, v in REQUIRED_CHECKS]
    forbidden = [_ssar(auth_api, g, r, v) for g, r, v in FORBIDDEN_CHECKS]

    missing = [c for c in required if not c.allowed]
    overly_permissive = [c for c in forbidden if c.allowed]

    if missing or overly_permissive:
        parts: list[str] = []
        if missing:
            parts.append(
                "missing required: "
                + ", ".join(f"{c.verb} {c.group or 'core'}/{c.resource}" for c in missing)
            )
        if overly_permissive:
            parts.append(
                "unexpectedly allowed (must be denied): "
                + ", ".join(
                    f"{c.verb} {c.group or 'core'}/{c.resource}" for c in overly_permissive
                )
            )
        detail = "; ".join(parts)
        return VerifyResult(
            ok=False,
            status="error",
            detail=detail,
            required=required,
            forbidden=forbidden,
        )

    return VerifyResult(
        ok=True,
        status="verified",
        detail="All required read verbs allowed; write/exec/secrets denied.",
        required=required,
        forbidden=forbidden,
    )


def verify_and_persist(cluster_id: int) -> VerifyResult:
    """Verify RBAC for a stored cluster and update rbac_status in SQLite."""
    secrets = get_cluster_secrets(cluster_id)
    if secrets is None:
        return VerifyResult(
            ok=False,
            status="error",
            detail=f"Cluster id={cluster_id} not found",
        )

    try:
        result = verify_cluster_credentials(secrets)
    except Exception as exc:  # noqa: BLE001
        logger.exception("RBAC verify failed for cluster_id=%s", cluster_id)
        update_rbac_status(cluster_id, status="error", detail=str(exc))
        return VerifyResult(ok=False, status="error", detail=str(exc))

    update_rbac_status(cluster_id, status=result.status, detail=result.detail)  # type: ignore[arg-type]
    return result
