#!/usr/bin/env python3
"""CLI: verify read-only RBAC for a registered cluster.

Usage:
  python scripts/verify_rbac.py <name-or-id>
  make verify-rbac CLUSTER=my-cluster

Uses the same verify path as the admin UI POST /clusters/{id}/verify.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Repo root on sys.path when invoked as a script
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv

load_dotenv(ROOT / ".env")

from admin_ui import db  # noqa: E402
from admin_ui.rbac_verify import verify_and_persist  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "cluster",
        help="Registered cluster name or numeric id",
    )
    args = parser.parse_args()

    db.init_db()
    if args.cluster.isdigit():
        cluster = db.get_cluster(int(args.cluster))
    else:
        cluster = db.get_cluster_by_name(args.cluster)

    if cluster is None:
        print(f"error: cluster {args.cluster!r} not found", file=sys.stderr)
        return 1

    result = verify_and_persist(cluster.id)
    print(f"cluster: {cluster.name} (id={cluster.id})")
    print(f"status:  {result.status}")
    print(f"detail:  {result.detail}")
    if result.required:
        print("required checks:")
        for c in result.required:
            mark = "OK" if c.allowed else "MISSING"
            print(f"  [{mark}] {c.verb} {c.group or 'core'}/{c.resource}")
    if result.forbidden:
        print("forbidden checks (must be denied):")
        for c in result.forbidden:
            mark = "DENIED" if not c.allowed else "LEAK"
            print(f"  [{mark}] {c.verb} {c.group or 'core'}/{c.resource}")
    return 0 if result.ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
