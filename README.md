# k8s-cost-agent

Multi-cluster Kubernetes **cost anomaly detection** agent, exposed as an MCP
server, with a small local admin UI for registering and managing target
clusters.

Hub-and-spoke MVP: one central MCP process queries any registered spoke
cluster on demand via a LangGraph supervisor-worker pipeline. No persistent
metrics store, no scheduled jobs — all data is pulled live at request time.
The agent **never mutates cluster state**; remediation is advisory text only.

Pattern reuse: LangGraph supervisor-worker + AWS Bedrock (Claude Haiku),
exposed via MCP — same shape as [ci-triage-agent](https://github.com/kere-sifon/Ci-mvp)
/ [ci-triage-mcp](https://github.com/).

---

## Architecture (hub-and-spoke + admin UI)

```
                    ┌─────────────────────────────┐
                    │  Claude Desktop (MCP client) │
                    └──────────────┬──────────────┘
                                   │ stdio
                                   ▼
                    ┌─────────────────────────────┐
                    │  mcp_server (k8s-cost-mcp)  │
                    │  list_cost_anomalies        │
                    │  explain_anomaly            │
                    │  suggest_remediation        │
                    │  list_registered_clusters   │
                    └──────────────┬──────────────┘
                                   │ read
                                   ▼
                         shared SQLite file
                    (K8S_COST_AGENT_DB_PATH)
                                   ▲
                                   │ write / verify
                    ┌──────────────┴──────────────┐
                    │  admin_ui (FastAPI+Jinja2)  │
                    │  localhost-only by default  │
                    │  register / edit / delete   │
                    │  verify RBAC                │
                    └─────────────────────────────┘

Per MCP query (target cluster bound in):

  START → supervisor → usage-analyzer → supervisor
                     → baseline-comparator → supervisor
                     → explainer → supervisor
                     → END

  Data sources (live): metrics-server / Prometheus + Kubecost API
  LLM: AWS Bedrock (Claude Haiku) — stubbed as TODO in this scaffold
```

| Process | Role | Integration |
|---------|------|-------------|
| **admin_ui** | Server-rendered registration UI + RBAC verify | Writes SQLite |
| **mcp_server** | MCP tools → LangGraph graph | Reads SQLite |
| **agent** | Supervisor + workers | Imported by mcp_server only |

`admin_ui` and `mcp_server` are **two independent processes**. They share a
single SQLite file as their only integration point — no shared in-memory
state, no direct imports between the two. Both resolve the DB path from the
same env var (`K8S_COST_AGENT_DB_PATH`) and open connections with
`PRAGMA journal_mode=WAL` and `busy_timeout=5000` so occasional UI writes
do not contend with MCP reads.

---

## Repo layout

```
mcp_server/              MCP server (stdio)
  server.py              Four tools
  cluster_resolver.py    SQLite read + RBAC gate + per-request kube client
  agent/                 LangGraph supervisor + workers (Bedrock/Kubecost TODOs)
  README.md              How to run + Claude Desktop config
admin_ui/                FastAPI + Jinja2 registration UI, SQLite writes
rbac/                    ClusterRole / RoleBinding YAML (apply per spoke)
scripts/                 CLI: verify_rbac.py, register_cluster.py
tests/                   Resolver + RBAC-gate tests
data/                    Local SQLite file (gitignored)
```

See also [`mcp_server/README.md`](./mcp_server/README.md). Query tools **refuse**
clusters that are not RBAC-verified (`rbac_status != verified`).
---

## MCP tools

| Tool | Purpose |
|------|---------|
| `list_registered_clusters` | Names, API URLs, RBAC status (no tokens) |
| `list_cost_anomalies(cluster, namespace?, time_window?)` | Live anomaly detection |
| `explain_anomaly(cluster, anomaly_id)` | Natural-language explanation |
| `suggest_remediation(cluster, anomaly_id)` | Advisory remediation text only |

---

## Setup

### 1. Install

```bash
cd k8s-cost-agent
make install
cp .env.example .env
```

### 2. Shared datastore path (required)

Both processes **must** see the same path:

```bash
export K8S_COST_AGENT_DB_PATH=./data/clusters.db
```

Or set it in `.env` (loaded by both apps). There is no hardcoded default
that differs between processes — if the env var is missing, both refuse to
start/connect.

```bash
make init-db
```

### 3. Run the admin UI (localhost only)

```bash
make admin-ui
# → http://127.0.0.1:8082
```

**Localhost-only is a deliberate default**, not an oversight. The admin UI
has **no authentication layer** at this MVP scope. Binding to `127.0.0.1`
is the stated mitigation: the registration surface (including write-only
token fields) is not exposed beyond the local machine unless you explicitly
set `ADMIN_UI_HOST` to something else. Do not bind to `0.0.0.0` on a shared
or production host without adding real auth first.

The UI is **admin-only**: add / edit / delete / verify clusters. There is
no query or chat interface here — all cost querying happens via the MCP
client (Claude Desktop).

### 4. Register a spoke cluster

On each target cluster:

```bash
kubectl apply -f rbac/cost-agent-readonly.yaml
kubectl create token k8s-cost-agent -n k8s-cost-agent --duration=8760h
```

Then either:

- Use the UI at `/clusters/new`, or
- CLI: `make register-cluster NAME=prod SERVER=https://... TOKEN=...`

Verify RBAC (same check as the UI button):

```bash
make verify-rbac CLUSTER=prod
```

The ClusterRole grants **get/list/watch only** on pods, nodes, namespaces,
resourcequotas, limitranges, and `metrics.k8s.io`. No secrets access. No
create/update/delete/exec anywhere. Verification also asserts that write /
exec / secrets verbs remain **denied**.

### 5. Run the MCP server

```bash
# Same DB path as the admin UI
export K8S_COST_AGENT_DB_PATH=./data/clusters.db
make mcp-server
```

Claude Desktop config example:

```json
{
  "mcpServers": {
    "k8s-cost": {
      "command": "/absolute/path/to/k8s-cost-agent/.venv/bin/python",
      "args": ["/absolute/path/to/k8s-cost-agent/mcp_server/server.py"],
      "env": {
        "K8S_COST_AGENT_DB_PATH": "/absolute/path/to/k8s-cost-agent/data/clusters.db",
        "AWS_PROFILE": "default",
        "AWS_REGION": "us-east-1",
        "PYTHONPATH": "/absolute/path/to/k8s-cost-agent"
      }
    }
  }
}
```

### 6. Bedrock / Kubecost (next wiring step)

This scaffold marks Bedrock and Kubecost calls as explicit `TODO`s so the
graph and MCP tools are runnable offline with deterministic stubs. Wire:

- `agent/clients/bedrock.py` → `ChatBedrockConverse` (Claude Haiku)
- `agent/clients/kubecost_client.py` → live allocation API
- metrics-server reads via `agent/clients/k8s_client.py`

---

## Security boundaries (MVP)

| Rule | Enforcement |
|------|-------------|
| No write/exec RBAC verbs | `rbac/cost-agent-readonly.yaml` + verify checks forbidden verbs |
| No cluster mutation from tools | Remediation returned as text only |
| Tokens never re-rendered | Write-only form fields; secrets columns excluded from templates/MCP list |
| Admin UI not publicly exposed | Default bind `127.0.0.1`; no auth (documented tradeoff) |
| No standing shared k8s client | Fresh `Configuration` per request from resolved cluster row |

---

## Production Evolution

The current model — a local admin UI that stores **long-lived ServiceAccount
tokens** in SQLite, and an MCP server that presents those tokens to each
spoke API server — is a **deliberate, documented tradeoff for local/lab
use**. It optimizes for:

- Fast registration without standing up an identity broker
- A single laptop-hosted hub that can reach lab clusters
- Clear RBAC-as-code that is identical on every spoke

It is **not** the intended production credential model.

### Target: OIDC federation (no standing credentials)

The production evolution mirrors the **GitHub OIDC → AWS STS** flow used by
ci-triage-agent, adapted for **cluster-to-cluster trust**:

1. **Hub workload identity**  
   The MCP server (or a small hub-side broker) runs with a cloud workload
   identity (e.g. IRSA / EKS Pod Identity, GKE Workload Identity, or Azure
   Workload ID) — not a long-lived kubeconfig.

2. **Spoke trust of the hub IdP**  
   Each spoke cluster is configured to trust the hub’s OIDC issuer (or a
   shared org issuer). Projected service-account tokens or an identity
   provider plugin maps federated claims onto a spoke-local subject.

3. **Short-lived per-cluster tokens**  
   On each MCP tool invocation the hub:
   - authenticates to the spoke’s token endpoint / IAM path using federation
   - receives a **short-lived** audience-bound token for that spoke only
   - builds a per-request kubernetes `Configuration` (same as today)
   - discards the token when the request completes  

   No ServiceAccount bearer token is stored in SQLite. The datastore retains
   **non-secret** registration metadata (name, API URL, Kubecost URL,
   trust config, last RBAC verify status).

4. **Same least-privilege Role**  
   The spoke still applies `rbac/cost-agent-readonly.yaml` (or an equivalent
   Role bound to the federated subject). Verification stays: required read
   verbs allowed, write/exec/secrets denied.

5. **Admin UI becomes trust registration, not secret vault**  
   Operators register the spoke’s API endpoint and OIDC/trust parameters.
   Token paste fields go away. The localhost-only / no-auth posture can then
   be replaced with real SSO because the UI no longer handles standing
   secrets.

### Why not jump there in the MVP

Federation requires issuer setup on every spoke, hub IAM wiring, and
clock/audience hardening. For a single-operator lab, static tokens plus
localhost-bound registration are enough to prove the hub-and-spoke query
path, LangGraph routing, and RBAC boundary. The code is shaped so the
credential **resolution** step (`resolve_cluster` → build client) can swap
from “read token column” to “federate and mint” without changing the MCP
tool surface or the supervisor-worker graph.

### Migration sketch

| Today (lab) | Production |
|-------------|------------|
| SQLite `sa_token` column | Empty / removed; trust config columns |
| `kubectl create token … --duration=8760h` | On-demand STS / projected token (minutes) |
| Admin UI stores secrets | Admin UI stores API URL + OIDC audience/issuer |
| Localhost-only mitigation | SSO-authenticated admin + private network |
| Same ClusterRole YAML | Same ClusterRole YAML (stable contract) |

Until that migration lands, treat `data/clusters.db` as **secret material**:
keep it gitignored, filesystem-restricted to the operating user, and never
point `ADMIN_UI_HOST` at a shared interface.

---

## Changelog

See [CHANGELOG.md](./CHANGELOG.md).
