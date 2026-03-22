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
  Object Storage bucket  ← Terraform remote state
  ARM A1 VM (4 OCPU, 24GB RAM)
    nginx (SSL termination, reverse proxy)
    uvicorn (port 8000, internal only)
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
| Security List | Ingress: 22, 80, 443; Egress: all |
| Public Subnet | `10.0.1.0/24` |
| Compute Instance | `VM.Standard.A1.Flex`, 4 OCPUs, 24GB RAM, Ubuntu 22.04 ARM |
| Boot Volume | 50GB (within 200GB always-free allowance) |
| Object Storage Bucket | Terraform state backend |

Cloudflare DNS A record for `shopping.aydho.com` is also managed by Terraform using the Cloudflare provider, so it updates automatically when the VM IP changes.

---

## cloud-init Bootstrap

Runs once on first VM boot. Leaves the VM fully ready to serve traffic.

1. Install Python 3.12, nginx, git, pip
2. Create `deploy` OS user; install SSH public key
3. Clone repo: `github.com/[owner]/shopping-agent` → `/home/deploy/shopping-agent`
4. Write `.env` file (DATABASE_URL, COLES_API_KEY, DEBUG — injected via Terraform `templatefile`)
5. Write Cloudflare Origin Certificate to `/etc/ssl/cloudflare-origin.pem`
6. Write Cloudflare Origin Key to `/etc/ssl/cloudflare-origin-key.pem`
7. `pip install -e .` in the repo directory
8. Write nginx config: HTTPS on 443 (Cloudflare Origin cert), HTTP 80 → 443 redirect, proxy to `localhost:8000`
9. Write systemd unit `shopping-agent.service`, enable + start
10. Enable + start nginx

---

## GitHub Actions Workflows

### `provision.yml` — Infrastructure (manual)

Trigger: `workflow_dispatch`

Steps:
1. Checkout repo
2. Install Terraform
3. `terraform init` with OCI Object Storage backend
4. `terraform apply -auto-approve`
5. Output VM public IP to job summary

Use case: initial provisioning, or reprovisioning after `terraform destroy`.

### `deploy.yml` — Code Deploy (automatic)

Trigger: push to `main`

Steps:
1. SSH into VM as `deploy` user
2. `git pull origin main`
3. `pip install -e .`
4. `sudo systemctl restart shopping-agent`

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
| `SSH_PRIVATE_KEY` | deploy.yml SSH access; provision.yml cloud-init |
| `SSH_PUBLIC_KEY` | Terraform → cloud-init (installed on VM) |
| `CF_API_TOKEN` | Terraform Cloudflare provider (DNS management) |
| `CF_ORIGIN_CERT` | Terraform → cloud-init → nginx SSL |
| `CF_ORIGIN_KEY` | Terraform → cloud-init → nginx SSL |
| `DATABASE_URL` | Terraform → cloud-init → .env |
| `COLES_API_KEY` | Terraform → cloud-init → .env |

---

## File Structure

```
terraform/
  main.tf                    # VCN, subnet, security list, compute instance
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
- nginx configured with this cert; uvicorn runs HTTP internally (no TLS between nginx and uvicorn)

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

1. Go to GitHub → Actions → `provision.yml` → Run workflow
2. Terraform destroys and recreates the VM
3. cloud-init bootstraps the new VM (~5 min)
4. Terraform updates Cloudflare DNS A record to new IP automatically
5. Site is live at `shopping.aydho.com`
