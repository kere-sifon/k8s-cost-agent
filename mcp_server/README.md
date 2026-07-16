# k8s-cost MCP server

On-demand cost anomaly tools for clusters registered in the admin UI.
Stdio transport for Claude Desktop.

## Prerequisites

1. **Shared SQLite path** — must match the admin UI:

```bash
export K8S_COST_AGENT_DB_PATH=/absolute/path/to/k8s-cost-agent/data/clusters.db
```

2. **Clusters registered + RBAC-verified** in the admin UI (`make admin-ui`).
   This server **refuses** to query a cluster whose `rbac_status` is not
   `verified`. Tokens are read from SQLite here and are never exposed via
   MCP tool responses.

3. **AWS credentials** for Claude Haiku (Bedrock Converse):

```bash
export AWS_REGION=us-east-1
export AWS_PROFILE=default   # or ambient IAM creds
export BEDROCK_MODEL_ID=us.anthropic.claude-haiku-4-5-20251001-v1:0
```

## Run

From the repo root (so `mcp_server` is importable):

```bash
export PYTHONPATH=/absolute/path/to/k8s-cost-agent
export K8S_COST_AGENT_DB_PATH=/absolute/path/to/k8s-cost-agent/data/clusters.db
.venv/bin/python mcp_server/server.py
# or: make mcp-server
```

## Claude Desktop

```json
{
  "mcpServers": {
    "k8s-cost": {
      "command": "/absolute/path/to/k8s-cost-agent/.venv/bin/python",
      "args": ["/absolute/path/to/k8s-cost-agent/mcp_server/server.py"],
      "env": {
        "PYTHONPATH": "/absolute/path/to/k8s-cost-agent",
        "K8S_COST_AGENT_DB_PATH": "/absolute/path/to/k8s-cost-agent/data/clusters.db",
        "AWS_REGION": "us-east-1",
        "AWS_PROFILE": "default"
      }
    }
  }
}
```

Fully quit and restart Claude Desktop after editing the config.

## Tools

| Tool | Notes |
|------|--------|
| `list_registered_clusters` | name, api_server_url, rbac_verified, last_checked — **no tokens** |
| `list_cost_anomalies` | Requires verified cluster; live metrics + optional Kubecost |
| `explain_anomaly` | Plain-English explanation (Bedrock TODO → stub text today) |
| `suggest_remediation` | Advisory text only — never applied |

## Layout

```
mcp_server/
  server.py              # MCP entrypoint
  cluster_resolver.py    # SQLite read + RBAC gate + per-request kube client
  agent/
    graph.py             # LangGraph supervisor-worker
    workers.py           # usage_analyzer, baseline_comparator, explainer
    bedrock_client.py    # Claude Haiku via boto3 Converse API
    kubecost_client.py   # Kubecost HTTP (TODO)
```

## Constraints

- Read-only against every cluster (no create/update/delete/exec).
- No scheduled jobs — every tool call resolves live.
- No shared/global kubernetes client — always `resolve_cluster_client()`.
- This process never writes to the `clusters` table.
