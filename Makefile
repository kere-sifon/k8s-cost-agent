# k8s-cost-agent — local lab / MVP helpers
#
# Both admin-ui and mcp-server must see the same DB path:
#   export K8S_COST_AGENT_DB_PATH=./data/clusters.db

.PHONY: help install admin-ui mcp-server verify-rbac register-cluster init-db test

PYTHON ?= .venv/bin/python
export K8S_COST_AGENT_DB_PATH ?= ./data/clusters.db
export PYTHONPATH := $(CURDIR)

help:
	@echo "Targets:"
	@echo "  install            Create venv and install requirements"
	@echo "  init-db            Ensure data/ exists and SQLite schema is created"
	@echo "  admin-ui           Run the FastAPI registration UI (localhost)"
	@echo "  mcp-server         Run the MCP server on stdio"
	@echo "  test               Run pytest (cluster_resolver + RBAC gate)"
	@echo "  verify-rbac        Verify read-only RBAC for a registered cluster"
	@echo "  register-cluster   CLI alternative to the admin UI (see scripts/)"

install:
	python3 -m venv .venv
	.venv/bin/pip install -U pip
	.venv/bin/pip install -r requirements.txt
	.venv/bin/pip install -r requirements-dev.txt

test:
	$(PYTHON) -m pytest tests/ -q

init-db:
	mkdir -p data
	$(PYTHON) -c "from admin_ui.db import init_db; init_db()"

admin-ui: init-db
	$(PYTHON) -m uvicorn admin_ui.app:app --host $${ADMIN_UI_HOST:-127.0.0.1} --port $${ADMIN_UI_PORT:-8082} --reload

mcp-server: init-db
	$(PYTHON) mcp_server/server.py

# Usage: make verify-rbac CLUSTER=my-cluster
verify-rbac:
	@test -n "$(CLUSTER)" || (echo "Usage: make verify-rbac CLUSTER=<name-or-id>"; exit 1)
	$(PYTHON) scripts/verify_rbac.py "$(CLUSTER)"

# Usage: make register-cluster NAME=... SERVER=... TOKEN=... [KUBECOST_URL=...] [KUBECOST_TOKEN=...]
register-cluster:
	$(PYTHON) scripts/register_cluster.py \
		--name "$(NAME)" \
		--server "$(SERVER)" \
		--token "$(TOKEN)" \
		$(if $(KUBECOST_URL),--kubecost-url "$(KUBECOST_URL)") \
		$(if $(KUBECOST_TOKEN),--kubecost-token "$(KUBECOST_TOKEN)")
