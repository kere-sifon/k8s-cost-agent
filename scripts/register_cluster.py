#!/usr/bin/env python3
"""CLI: register a cluster (alternative to the admin UI).

Usage:
  python scripts/register_cluster.py \\
    --name prod-us-east \\
    --server https://api.example.com:6443 \\
    --token "$SA_TOKEN" \\
    --kubecost-url http://kubecost.example:9090

  make register-cluster NAME=... SERVER=... TOKEN=...
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

from admin_ui import db  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--name", required=True, help="Unique cluster name")
    parser.add_argument("--server", required=True, help="Kubernetes API server URL")
    parser.add_argument("--token", required=True, help="ServiceAccount bearer token")
    parser.add_argument("--kubecost-url", default=None, help="Kubecost base URL")
    parser.add_argument("--kubecost-token", default=None, help="Kubecost API token")
    parser.add_argument(
        "--ca-cert-file",
        default=None,
        help="Path to cluster CA certificate PEM (enables TLS verification)",
    )
    args = parser.parse_args()

    db.init_db()
    if db.get_cluster_by_name(args.name):
        print(f"error: cluster name {args.name!r} already exists", file=sys.stderr)
        return 1

    ca_cert_pem = None
    if args.ca_cert_file:
        ca_cert_pem = Path(args.ca_cert_file).read_text(encoding="utf-8")

    cluster = db.create_cluster(
        name=args.name,
        api_server_url=args.server,
        sa_token=args.token,
        kubecost_url=args.kubecost_url,
        kubecost_token=args.kubecost_token,
        ca_cert_pem=ca_cert_pem,
    )
    print(f"registered id={cluster.id} name={cluster.name}")
    print(f"rbac_status={cluster.rbac_status} (run: make verify-rbac CLUSTER={cluster.name})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
