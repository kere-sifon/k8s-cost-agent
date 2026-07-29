# Snyk learning guide (k8s-cost-agent)

This repo wires **Snyk** three ways so you can see how each product differs:

| Command | What it scans | What you learn |
|---------|---------------|----------------|
| `snyk test` | Open-source deps in `requirements.txt` | Known CVEs / upgrade paths in libraries you import |
| `snyk code test` | **Your** Python source (SAST) | Injection, secrets, insecure patterns in code you wrote |
| `snyk iac test` | `rbac/*.yaml` | Misconfigurations in Kubernetes manifests |

They are separate engines. A clean `snyk test` does **not** mean your app code is safe.

---

## 1. One-time setup

### Install the CLI (pick one)

```bash
# Homebrew
brew install snyk/tap/snyk

# or npm (no global install needed later — Makefile can use npx)
npm install -g snyk
```

### Authenticate

```bash
make snyk-auth
# opens browser / pastes a token from https://app.snyk.io/account
```

Or set a token (CI / non-interactive):

```bash
export SNYK_TOKEN=...   # from https://app.snyk.io/account
```

For GitHub Actions, add the same value as a repo secret named `SNYK_TOKEN`.

### Enable Snyk Code (required for `snyk code test`)

Snyk Code is **off by default**. Until you turn it on, CI fails with `SNYK-CODE-0005` / 403:

1. Open [app.snyk.io](https://app.snyk.io) and select your **Organization** (Org Admin role).
2. Go to **Settings → Snyk Code**.
3. Set **Enable Snyk Code** to **Enabled** → **Save changes**.
4. Re-run the workflow (or `make snyk-code`).

CLI/GitHub Actions do **not** need a GitHub integration for `snyk code test`; that integration is only for UI-imported repos. Enabling the product in Settings is enough.

If the error shows an empty org name (`organization: ```), also confirm the token belongs to an org:

```bash
snyk config get org          # may be empty
snyk orgs                   # list orgs for this token
snyk config set org=<ORG_ID>
```

---

## 2. Local workflow (Makefile)

From the repo root (venv installed so Python deps exist for accurate OS scans):

```bash
make install          # ensure .venv + requirements are present
make snyk-test        # open-source dependency vulns
make snyk-code        # SAST on your Python
make snyk-iac         # Kubernetes YAML in rbac/
make snyk             # all three
```

Useful flags (already used in Make where noted):

- `--severity-threshold=high` — only fail the command on high/critical (good while learning)
- `--json-file-output=...` / `--sarif-file-output=...` — machine-readable results

Scan artifacts (`*.sarif`, `snyk-*.json`) are gitignored.

---

## 3. CI workflow

[`.github/workflows/snyk.yml`](../.github/workflows/snyk.yml) runs on push/PR to `main`:

1. **Open Source** — `snyk/actions/python` against `requirements.txt`
2. **Code** — `snyk code test`
3. **IaC** — `snyk iac test rbac/`

`continue-on-error: true` is intentional for learning: you still get results/SARIF uploads without blocking merges on every finding. Tighten later by removing that and choosing a severity threshold.

---

## 4. How to read results

1. Run `make snyk-test` and note a vulnerable package + **Fixed in** version.
2. Run `make snyk-code` and open the file:line Snyk cites — compare to a dependency CVE.
3. Run `make snyk-iac` on `rbac/cost-agent-readonly.yaml` — see how RBAC/securityContext rules are judged.
4. Optional: `snyk monitor` (or `make snyk-monitor`) snapshots the dependency graph to the Snyk UI for ongoing tracking.

---

## 5. What this is *not*

- Not a substitute for RBAC verify / cluster probes in this agent
- Not container image scanning (add `snyk container test` later if you build an image)
- Does not read `.env` or `data/clusters.db` when configured correctly — keep those gitignored
