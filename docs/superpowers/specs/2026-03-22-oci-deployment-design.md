# OCI Deployment Design

**Date:** 2026-03-22
**Project:** shopping-agent
**Status:** Approved

## Overview

Deploy the shopping-agent FastAPI app to Oracle Cloud Infrastructure (OCI) Melbourne region on the Always Free ARM A1 tier. Infrastructure is defined as code using Terraform and provisioned via a GitHub Actions manual workflow. Code deploys happen automatically on push to `main` via SSH.

## Goals

- Fully reproducible VM: destroy and recreate with a single GitHub Actions trigger
- Zero ongoing cost (OCI Always Free tier)
- HTTPS at `shopping.aydho.com` via Cloudflare proxy + Origin Certificate
- Automatic Cloudflare DNS update when the VM IP changes after reprovisioning

## Non-Goals

- Multiple environments (no staging/prod split)
- Container-based deployment (direct Python install is simpler)
- Database hosting on OCI (Supabase remains the DB)

---

## Architecture

```
GitHub
  push to main     → deploy.yml    → SSH → git pull + restart
  workflow_dispatch → provision.yml → terraform apply

OCI Melbourne (ap-melbourne-1)
  Object Storage bucket  ← Terraform remote state (pre-created, NOT Terraform-managed)
  ARM A1 VM (4 OCPU, 24GB RAM)
    nginx (SSL termination, reverse proxy → 127.0.0.1:8000)
    uvicorn (port 8000, localhost only)
    systemd service: shopping-agent

Cloudflare
  shopping.aydho.com → proxy (orange cloud) → VM public IP
  Origin Certificate  → nginx (Full Strict SSL mode)
```

---

## OCI Resources (Terraform-managed)

| Resource | Details |
|----------|---------|
| VCN | `10.0.0.0/16`, region: `ap-melbourne-1` |
| Internet Gateway | Attached to VCN |
| Route Table | Default route `0.0.0.0/0` → Internet Gateway |
| Security List | Ingress: 22, 80, 443; Egress: all. Port 8000 is NOT opened. |
| Public Subnet | `10.0.1.0/24` |
| Compute Instance | `VM.Standard.A1.Flex`, 4 OCPUs, 24GB RAM, Ubuntu 22.04 ARM |
| Boot Volume | 50GB (within 200GB always-free allowance) |

**Not Terraform-managed:** The Object Storage bucket used for Terraform state must be created manually in the OCI console once before the first `terraform init`. It cannot be Terraform-managed because the backend must exist before Terraform can run.

Cloudflare DNS A record for `shopping.aydho.com` is managed by Terraform using the Cloudflare provider and updates automatically when the VM IP changes.

---

## cloud-init Bootstrap

Runs once on first VM boot. Leaves the VM fully ready to serve traffic.

1. Install Python 3.12, nginx, git, pip; disable `ufw` explicitly (prevent package installs from enabling it)
2. Create `deploy` OS user; install SSH public key
3. Write GitHub Deploy Key to `/home/deploy/.ssh/id_rsa` (injected via Terraform `templatefile`); configure `/home/deploy/.ssh/config` to use it for `github.com`
4. Clone repo via SSH: `git@github.com:[owner]/shopping-agent` → `/home/deploy/shopping-agent`
5. Create virtualenv at `/home/deploy/venv`
6. Write `.env` file to `/home/deploy/shopping-agent/.env` with: `DATABASE_URL`, `COLES_API_KEY`, `WOOLWORTHS_API_KEY`, `DEBUG=false` (injected via `templatefile`; systemd `EnvironmentFile` is canonical — app's internal `load_dotenv()` is a no-op when vars are already in the environment)
7. Write Cloudflare Origin Certificate to `/etc/ssl/cloudflare-origin.pem`
8. Write Cloudflare Origin Key to `/etc/ssl/cloudflare-origin-key.pem`
9. `/home/deploy/venv/bin/pip install -e .` in `/home/deploy/shopping-agent`
10. Write nginx config: HTTPS on 443 (Cloudflare Origin cert), HTTP 80 → 443 redirect, proxy to `127.0.0.1:8000` (not `0.0.0.0` — uvicorn must not be reachable from outside)
11. Write systemd unit `shopping-agent.service`:
    - `User=deploy`
    - `WorkingDirectory=/home/deploy/shopping-agent`
    - `EnvironmentFile=/home/deploy/shopping-agent/.env`
    - `ExecStart=/home/deploy/venv/bin/uvicorn shopping_agent.main:app --host 127.0.0.1 --port 8000`
    - `Restart=always`
12. Add passwordless sudoers rule: `deploy ALL=(ALL) NOPASSWD: /bin/systemctl restart shopping-agent`
13. Enable + start nginx and shopping-agent
14. Write sentinel file `/home/deploy/.cloud-init-done` (signals bootstrap complete)

---

## GitHub Actions Workflows

### `provision.yml` — Infrastructure (manual)

Trigger: `workflow_dispatch` with a boolean input `recreate` (default: `false`)

Steps:
1. Checkout repo
2. Install Terraform
3. `terraform init` with OCI Object Storage backend
4. If `recreate=true`: `terraform apply -replace=oci_core_instance.main -auto-approve` (forces VM recreation without touching networking)
5. If `recreate=false`: `terraform apply -auto-approve` (creates on first run, corrects drift on subsequent runs)
6. Output VM public IP to job summary

**Bootstrap prerequisite:** The OCI Object Storage bucket for Terraform state must be created manually in the OCI console once before this workflow can run for the first time.

**Timing:** After provisioning, cloud-init takes ~5 minutes. Do not push a code deploy during this window. The sentinel file `/home/deploy/.cloud-init-done` can be checked to confirm readiness.

### `deploy.yml` — Code Deploy (automatic)

Trigger: push to `main`

Steps:
1. SSH into VM as `deploy` user with `StrictHostKeyChecking=no` (handles new host key after reprovision; acceptable trade-off given `CF_API_TOKEN` is scoped to DNS-only, limiting blast radius of credential compromise)
2. `git pull origin main`
3. `/home/deploy/venv/bin/pip install -e .`
4. `/home/deploy/venv/bin/alembic upgrade head` (runs DB migrations; no-op if schema is current)
5. `sudo systemctl restart shopping-agent`

---

## GitHub Secrets

| Secret | Purpose |
|--------|---------|
| `OCI_USER_OCID` | Terraform OCI provider auth |
| `OCI_FINGERPRINT` | Terraform OCI provider auth |
| `OCI_TENANCY_OCID` | Terraform OCI provider auth |
| `OCI_REGION` | Terraform OCI provider auth |
| `OCI_PRIVATE_KEY` | Terraform OCI provider auth (API signing key) |
| `OCI_OBJECT_STORAGE_NAMESPACE` | Terraform remote state backend |
| `SSH_PRIVATE_KEY` | deploy.yml SSH access |
| `SSH_PUBLIC_KEY` | Terraform → cloud-init (installed on VM for `deploy` user) |
| `GH_DEPLOY_KEY` | Terraform → cloud-init → git clone (read-only deploy key for the repo) |
| `CF_API_TOKEN` | Terraform Cloudflare provider — scoped to DNS Edit on aydho.com only |
| `CF_ZONE_ID` | Terraform Cloudflare provider (identifies aydho.com zone) |
| `CF_ORIGIN_CERT` | Terraform → cloud-init → nginx SSL |
| `CF_ORIGIN_KEY` | Terraform → cloud-init → nginx SSL |
| `DATABASE_URL` | Terraform → cloud-init → .env |
| `COLES_API_KEY` | Terraform → cloud-init → .env |
| `WOOLWORTHS_API_KEY` | Terraform → cloud-init → .env |

---

## File Structure

```
terraform/
  main.tf                    # VCN, subnet, security list, compute instance, Cloudflare DNS
  providers.tf               # OCI + Cloudflare providers
  backend.tf                 # OCI Object Storage state backend
  variables.tf               # All input variables
  outputs.tf                 # VM public IP
  cloud-init.yaml.tpl        # Bootstrap script template (templatefile)
  terraform.tfvars.example   # Example values (committed)
  terraform.tfvars           # Actual values (gitignored)

.github/workflows/
  provision.yml              # workflow_dispatch: terraform apply
  deploy.yml                 # push to main: SSH deploy
```

---

## SSL / Cloudflare Setup

- Cloudflare proxy enabled (orange cloud) on `shopping.aydho.com`
- SSL mode: **Full (Strict)**
- Certificate: Cloudflare Origin CA (15-year, free) — generated once in Cloudflare dashboard, stored as GitHub secrets `CF_ORIGIN_CERT` + `CF_ORIGIN_KEY`
- nginx terminates TLS with this cert; uvicorn runs HTTP on `127.0.0.1:8000` only (not reachable from outside the VM)

---

## Free Tier Constraints

| Resource | Used | Limit |
|----------|------|-------|
| ARM OCPUs | 4 | 4 |
| ARM RAM | 24GB | 24GB |
| Block storage | 50GB | 200GB |
| Object Storage | ~1MB (state) | 20GB |
| Reserved public IPs | 1 | 2 |

The entire ARM pool is consumed by this one VM. Adding a second VM would require reducing OCPUs/RAM on this one.

---

## Reprovisioning Flow

When the VM needs to be recreated:

1. Go to GitHub → Actions → `provision.yml` → Run workflow → set `recreate=true`
2. Terraform replaces the VM in-place (networking unchanged)
3. cloud-init bootstraps the new VM (~5 min)
4. Terraform updates Cloudflare DNS A record to new IP automatically
5. Wait for cloud-init to complete (check for `/home/deploy/.cloud-init-done`) before pushing any code deploy
6. Site is live at `shopping.aydho.com`

---

## Known Limitations

- **cloud-init race:** `deploy.yml` has no automatic guard against deploying during the ~5-minute bootstrap window after `provision.yml` runs. This requires operator discipline.
- **StrictHostKeyChecking=no:** SSH host key verification is disabled in `deploy.yml` to handle new VM host keys after reprovision. Risk is mitigated by scoping `CF_API_TOKEN` to DNS-only.
- **Terraform state loss:** If the Object Storage bucket or state file is lost, Terraform loses awareness of existing resources. Recovery requires manual import. No automated state backup is configured.
- **Dependency pinning:** `pip install -e .` installs from `pyproject.toml` ranges, not a lockfile. Transitive dependency upgrades could break the app on deploy. Consider generating a `requirements.txt` lockfile if this becomes a problem.
