# Changelog

All notable changes to this project will be documented in this file.

## [0.2.1] — 2026-07-28

### Added

- Snyk learning workflow: `docs/SNYK.md`, Makefile targets (`snyk-test` /
  `snyk-code` / `snyk-iac`), and `.github/workflows/snyk.yml` for CI.

## [0.2.0] — 2026-07-16

### Added

- Optional per-cluster `ca_cert_pem` in admin UI + SQLite; MCP resolver
  enables TLS verification when present and WARN-logs insecure fallback
  when absent. Cluster list shows TLS verified / insecure at a glance.
- Live AWS Bedrock Converse wiring in `mcp_server/agent/bedrock_client.py`
  (lazy boto3 client; workers fall back to heuristics on `RuntimeError`).
- Deterministic anomaly IDs via `_stable_id(namespace, resource, pattern)` —
  Bedrock-supplied ids are discarded so explain/remediate lookups stay stable.

### Changed

- Admin UI default port `8082`.

## [0.1.2] — 2026-07-14

### Added

- Worker system prompts and I/O contracts in `mcp_server/agent/prompts.py`
  (usage-analyzer, baseline-comparator, explainer).
- Live snapshot builder matching the documented analyzer INPUT shape
  (pods with requests/limits/usage, unattached PVCs).
- Heuristic fallbacks that emit the same OUTPUT JSON shapes when Bedrock
  is not wired; `tests/test_workers.py` covers shapes and no invented $.

### Changed

- Workers call `invoke_haiku_json` with the new prompts; baseline explicitly
  documents peer-snapshot limitation; explainer returns `confidence_note`.

## [0.1.1] — 2026-07-14

### Added

- `mcp_server/cluster_resolver.py` — full SQLite read, RBAC verification gate,
  per-request `kubernetes.client.Configuration` (never global), optional
  Kubecost client.
- `mcp_server/agent/` — LangGraph supervisor + usage_analyzer /
  baseline_comparator / explainer workers (Bedrock/Kubecost calls TODO).
- `mcp_server/README.md` — env vars, Claude Desktop config, verified-cluster
  prerequisite.
- `tests/test_cluster_resolver.py` — fake SQLite rows; verifies resolve +
  unverified gate.

### Changed

- MCP tools refuse clusters with `rbac_status != verified` with an actionable
  error pointing at the admin UI.
- Canonical graph lives under `mcp_server/agent/` (root `agent/` is a shim).

## [0.1.0] — 2026-07-14

### Added

- Repo scaffold for multi-cluster hub-and-spoke cost anomaly MVP.
- `rbac/cost-agent-readonly.yaml` — least-privilege ClusterRole / RoleBinding
  (get/list/watch only; no secrets; no write/exec).
- `admin_ui` — FastAPI + Jinja2 registration UI (list / add / edit / delete /
  verify RBAC) with SQLite datastore; tokens write-only after save;
  localhost bind by default.
- `mcp_server` — MCP tools: `list_cost_anomalies`, `explain_anomaly`,
  `suggest_remediation`, `list_registered_clusters`; resolves clusters from
  the shared SQLite file (`K8S_COST_AGENT_DB_PATH`).
- `agent` — LangGraph supervisor-worker graph (usage-analyzer,
  baseline-comparator, explainer) with Bedrock and Kubecost calls marked TODO.
- `scripts/verify_rbac.py` and `scripts/register_cluster.py` plus Makefile
  targets (CLI alternatives to the UI).
- README including architecture, two-process/shared-DB setup, localhost/no-auth
  tradeoff, and full **Production Evolution** (OIDC federation) section.
