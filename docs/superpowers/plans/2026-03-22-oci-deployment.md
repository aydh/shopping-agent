# OCI Deployment Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Provision an OCI Melbourne ARM A1 Always Free VM via Terraform + GitHub Actions, serving shopping-agent at `shopping.aydho.com` with automatic deploys on push to `main`.

**Architecture:** Terraform defines all OCI networking and compute resources plus the Cloudflare DNS record. A cloud-init template bootstraps the VM on first boot (Python 3.12 venv, nginx, systemd service). Two GitHub Actions workflows handle provisioning (manual `workflow_dispatch`) and code deploys (push to `main` via SSH).

**Tech Stack:** Terraform >= 1.6, OCI provider ~> 5.0, Cloudflare provider ~> 4.0, Ubuntu 22.04 ARM, Python 3.12 (deadsnakes PPA), nginx, systemd, GitHub Actions

---

## Pre-Implementation Notes

### Two types of OCI credentials are needed

| Purpose | Credential type | Where to get |
|---------|----------------|--------------|
| Terraform OCI provider | API Key (User OCID + PEM key + fingerprint) | OCI Console → Profile → API Keys |
| Terraform S3 backend (state storage) | Customer Secret Key | OCI Console → Profile → Customer Secret Keys |

These are different keys. The API Key is used by the OCI provider to manage resources. The Customer Secret Key is used by Terraform's S3-compatible backend to read/write state files in Object Storage.

### Object Storage bucket must be pre-created

The state bucket cannot be created by Terraform (chicken-and-egg). Create it manually in OCI Console → Object Storage → Create Bucket before running `terraform init`. Name it `terraform-state-shopping-agent` in the `ap-melbourne-1` region. Note your Object Storage namespace (shown in the console).

### GitHub Deploy Key for repo clone

The VM clones the repo during bootstrap using a read-only deploy key. Generate one with:
```bash
ssh-keygen -t ed25519 -C "shopping-agent-deploy" -f deploy_key -N ""
```
Add `deploy_key.pub` as a deploy key in GitHub → repo → Settings → Deploy Keys (read-only). Store `deploy_key` (private) as the `GH_DEPLOY_KEY` GitHub secret.

### Cloudflare Origin Certificate

In Cloudflare dashboard → SSL/TLS → Origin Server → Create Certificate:
- Hostnames: `shopping.aydho.com`
- Validity: 15 years
- Save the certificate PEM as `CF_ORIGIN_CERT` and key PEM as `CF_ORIGIN_KEY` GitHub secrets.

Set SSL/TLS mode to **Full (Strict)** in Cloudflare.

---

## File Structure

```
terraform/
  main.tf                   # VCN, subnet, security list, IGW, route table, compute, Cloudflare DNS
  providers.tf              # OCI + Cloudflare provider + Terraform version config
  backend.tf                # OCI Object Storage S3-compatible state backend
  variables.tf              # All input variables with descriptions
  outputs.tf                # VM public IP
  cloud-init.yaml.tpl       # Bootstrap script (templatefile — injected by Terraform)
  terraform.tfvars.example  # Example values for all variables (committed)
  backend.tfvars.example    # Example backend credentials (committed)
  terraform.tfvars          # Actual values (gitignored)
  backend.tfvars            # Actual backend credentials (gitignored)

.github/workflows/
  provision.yml             # workflow_dispatch: terraform apply (with recreate option)
  deploy.yml                # push to main: SSH git pull + pip install + migrate + restart
```

---

## Task 1: Scaffold Terraform directory + update .gitignore

**Files:**
- Create: `terraform/` (directory)
- Modify: `.gitignore`

- [ ] **Step 1: Create the terraform directory**

```bash
mkdir -p terraform
```

- [ ] **Step 2: Add Terraform entries to .gitignore**

Append to `.gitignore`:

```
# Terraform
terraform/.terraform/
terraform/.terraform.lock.hcl
terraform/terraform.tfvars
terraform/backend.tfvars
terraform/*.tfstate
terraform/*.tfstate.backup
terraform/crash.log
```

- [ ] **Step 3: Commit**

```bash
git add .gitignore
git commit -m "feat: scaffold terraform directory and gitignore"
```

---

## Task 2: providers.tf + backend.tf

**Files:**
- Create: `terraform/providers.tf`
- Create: `terraform/backend.tf`

- [ ] **Step 1: Write providers.tf**

```hcl
# terraform/providers.tf

terraform {
  required_version = ">= 1.6"

  required_providers {
    oci = {
      source  = "oracle/oci"
      version = "~> 5.0"
    }
    cloudflare = {
      source  = "cloudflare/cloudflare"
      version = "~> 4.0"
    }
  }
}

provider "oci" {
  tenancy_ocid = var.tenancy_ocid
  user_ocid    = var.user_ocid
  fingerprint  = var.fingerprint
  private_key  = var.private_key
  region       = var.region
}

provider "cloudflare" {
  api_token = var.cf_api_token
}
```

- [ ] **Step 2: Write backend.tf**

Credentials are passed via `-backend-config` flags (not stored in this file).

```hcl
# terraform/backend.tf

terraform {
  backend "s3" {
    # Credentials and endpoint passed via -backend-config flags or backend.tfvars
    # See backend.tfvars.example for local setup
    bucket                      = "terraform-state-shopping-agent"
    key                         = "terraform.tfstate"
    region                      = "ap-melbourne-1"
    skip_credentials_validation = true
    skip_metadata_api_check     = true
    skip_region_validation      = true
    force_path_style            = true
  }
}
```

- [ ] **Step 3: Write backend.tfvars.example**

```hcl
# terraform/backend.tfvars.example
# Copy to backend.tfvars and fill in your values (gitignored)

access_key = "your-customer-secret-key-id"
secret_key = "your-customer-secret-key"
endpoint   = "https://<namespace>.compat.objectstorage.ap-melbourne-1.oraclecloud.com"
```

- [ ] **Step 4: Commit**

```bash
git add terraform/providers.tf terraform/backend.tf terraform/backend.tfvars.example
git commit -m "feat: add terraform providers and backend config"
```

---

## Task 3: variables.tf

**Files:**
- Create: `terraform/variables.tf`

- [ ] **Step 1: Write variables.tf**

```hcl
# terraform/variables.tf

# OCI auth
variable "tenancy_ocid" {
  description = "OCID of the OCI tenancy (root compartment)"
  type        = string
}

variable "user_ocid" {
  description = "OCID of the OCI user for API access"
  type        = string
}

variable "fingerprint" {
  description = "Fingerprint of the OCI API key"
  type        = string
}

variable "private_key" {
  description = "PEM content of the OCI API private key"
  type        = string
  sensitive   = true
}

variable "region" {
  description = "OCI region"
  type        = string
  default     = "ap-melbourne-1"
}

# Compute
variable "ssh_public_key" {
  description = "SSH public key installed for the deploy user"
  type        = string
}

variable "gh_deploy_key" {
  description = "Private SSH key (ed25519) for cloning the GitHub repo"
  type        = string
  sensitive   = true
}

# App secrets injected into .env via cloud-init
variable "database_url" {
  description = "Supabase PostgreSQL connection string"
  type        = string
  sensitive   = true
}

variable "coles_api_key" {
  description = "Coles API key"
  type        = string
  sensitive   = true
}

variable "woolworths_api_key" {
  description = "Woolworths API key"
  type        = string
  sensitive   = true
}

# Cloudflare
variable "cf_api_token" {
  description = "Cloudflare API token scoped to DNS Edit on aydho.com"
  type        = string
  sensitive   = true
}

variable "cf_zone_id" {
  description = "Cloudflare zone ID for aydho.com"
  type        = string
}

variable "cf_origin_cert" {
  description = "Cloudflare Origin CA certificate PEM"
  type        = string
  sensitive   = true
}

variable "cf_origin_key" {
  description = "Cloudflare Origin CA private key PEM"
  type        = string
  sensitive   = true
}

# GitHub repo
variable "github_repo" {
  description = "GitHub repo in owner/name format (e.g. andrewsaunders/shopping-agent)"
  type        = string
}
```

- [ ] **Step 2: Write terraform.tfvars.example**

```hcl
# terraform/terraform.tfvars.example
# Copy to terraform.tfvars and fill in your values (gitignored)

tenancy_ocid  = "ocid1.tenancy.oc1..aaaa..."
user_ocid     = "ocid1.user.oc1..aaaa..."
fingerprint   = "aa:bb:cc:dd:..."
private_key   = <<EOF
-----BEGIN RSA PRIVATE KEY-----
...
-----END RSA PRIVATE KEY-----
EOF

region         = "ap-melbourne-1"
ssh_public_key = "ssh-ed25519 AAAA... your-key"
gh_deploy_key  = <<EOF
-----BEGIN OPENSSH PRIVATE KEY-----
...
-----END OPENSSH PRIVATE KEY-----
EOF

database_url       = "postgresql+asyncpg://postgres:password@db.xxx.supabase.co:5432/postgres"
coles_api_key      = "your-coles-api-key"
woolworths_api_key = "your-woolworths-api-key"

cf_api_token   = "your-cloudflare-api-token"
cf_zone_id     = "your-zone-id"
cf_origin_cert = <<EOF
-----BEGIN CERTIFICATE-----
...
-----END CERTIFICATE-----
EOF
cf_origin_key = <<EOF
-----BEGIN PRIVATE KEY-----
...
-----END PRIVATE KEY-----
EOF

github_repo = "your-github-username/shopping-agent"
```

- [ ] **Step 3: Commit**

```bash
git add terraform/variables.tf terraform/terraform.tfvars.example
git commit -m "feat: add terraform variables"
```

---

## Task 4: outputs.tf

**Files:**
- Create: `terraform/outputs.tf`

- [ ] **Step 1: Write outputs.tf**

```hcl
# terraform/outputs.tf

output "vm_public_ip" {
  description = "Public IP address of the shopping-agent VM"
  value       = oci_core_instance.app.public_ip
}

output "ssh_command" {
  description = "SSH command to connect to the VM"
  value       = "ssh deploy@${oci_core_instance.app.public_ip}"
}
```

- [ ] **Step 2: Commit**

```bash
git add terraform/outputs.tf
git commit -m "feat: add terraform outputs"
```

---

## Task 5: main.tf — networking

**Files:**
- Create: `terraform/main.tf`

- [ ] **Step 1: Write networking resources**

```hcl
# terraform/main.tf

# --- Availability Domain ---

data "oci_identity_availability_domains" "ads" {
  compartment_id = var.tenancy_ocid
}

locals {
  availability_domain = data.oci_identity_availability_domains.ads.availability_domains[0].name
}

# --- VCN ---

resource "oci_core_vcn" "main" {
  compartment_id = var.tenancy_ocid
  cidr_block     = "10.0.0.0/16"
  display_name   = "shopping-agent-vcn"
}

# --- Internet Gateway ---

resource "oci_core_internet_gateway" "main" {
  compartment_id = var.tenancy_ocid
  vcn_id         = oci_core_vcn.main.id
  display_name   = "shopping-agent-igw"
}

# --- Route Table ---

resource "oci_core_route_table" "main" {
  compartment_id = var.tenancy_ocid
  vcn_id         = oci_core_vcn.main.id
  display_name   = "shopping-agent-rt"

  route_rules {
    destination       = "0.0.0.0/0"
    network_entity_id = oci_core_internet_gateway.main.id
  }
}

# --- Security List ---

resource "oci_core_security_list" "main" {
  compartment_id = var.tenancy_ocid
  vcn_id         = oci_core_vcn.main.id
  display_name   = "shopping-agent-sl"

  egress_security_rules {
    destination = "0.0.0.0/0"
    protocol    = "all"
  }

  ingress_security_rules {
    protocol = "6" # TCP
    source   = "0.0.0.0/0"
    tcp_options {
      min = 22
      max = 22
    }
  }

  ingress_security_rules {
    protocol = "6"
    source   = "0.0.0.0/0"
    tcp_options {
      min = 80
      max = 80
    }
  }

  ingress_security_rules {
    protocol = "6"
    source   = "0.0.0.0/0"
    tcp_options {
      min = 443
      max = 443
    }
  }
}

# --- Subnet ---

resource "oci_core_subnet" "main" {
  compartment_id    = var.tenancy_ocid
  vcn_id            = oci_core_vcn.main.id
  cidr_block        = "10.0.1.0/24"
  display_name      = "shopping-agent-subnet"
  route_table_id    = oci_core_route_table.main.id
  security_list_ids = [oci_core_security_list.main.id]
}
```

- [ ] **Step 2: Validate networking (requires backend.tfvars)**

```bash
cd terraform
terraform init -backend-config=backend.tfvars
terraform validate
```

Expected: `Success! The configuration is valid.`

- [ ] **Step 3: Commit**

```bash
git add terraform/main.tf
git commit -m "feat: add OCI networking resources (VCN, subnet, security list)"
```

---

## Task 6: main.tf — compute instance (placeholder user_data)

**Files:**
- Modify: `terraform/main.tf`

- [ ] **Step 1: Look up Ubuntu 22.04 ARM image**

Append to `main.tf`:

```hcl
# --- Ubuntu 22.04 ARM Image ---

data "oci_core_images" "ubuntu_arm" {
  compartment_id           = var.tenancy_ocid
  operating_system         = "Canonical Ubuntu"
  operating_system_version = "22.04"
  shape                    = "VM.Standard.A1.Flex"
  sort_by                  = "TIMECREATED"
  sort_order               = "DESC"
}

# --- Compute Instance ---

resource "oci_core_instance" "app" {
  compartment_id      = var.tenancy_ocid
  availability_domain = local.availability_domain
  display_name        = "shopping-agent"
  shape               = "VM.Standard.A1.Flex"

  shape_config {
    ocpus         = 4
    memory_in_gbs = 24
  }

  source_details {
    source_type             = "image"
    source_id               = data.oci_core_images.ubuntu_arm.images[0].id
    boot_volume_size_in_gbs = 50
  }

  create_vnic_details {
    subnet_id        = oci_core_subnet.main.id
    assign_public_ip = true
    display_name     = "shopping-agent-vnic"
  }

  metadata = {
    ssh_authorized_keys = var.ssh_public_key
    # user_data added in Task 9 after cloud-init template is written
  }
}
```

- [ ] **Step 2: Validate**

```bash
cd terraform
terraform validate
```

Expected: `Success! The configuration is valid.`

- [ ] **Step 3: Preview plan (networking + compute)**

```bash
terraform plan -var-file=terraform.tfvars
```

Review output: should show ~8 resources to add. No resources to change/destroy.

- [ ] **Step 4: Commit**

```bash
git add terraform/main.tf
git commit -m "feat: add OCI compute instance (ARM A1)"
```

---

## Task 7: cloud-init.yaml.tpl

**Files:**
- Create: `terraform/cloud-init.yaml.tpl`

This template is rendered by Terraform's `templatefile()` function. All `${var}` references are Terraform variables injected at apply time.

- [ ] **Step 1: Write cloud-init.yaml.tpl**

```yaml
#cloud-config
# terraform/cloud-init.yaml.tpl

package_update: true
package_upgrade: true

packages:
  - git
  - nginx
  - software-properties-common
  - curl

runcmd:
  # Disable ufw (prevent accidental port blocking)
  - ufw disable || true

  # Install Python 3.12 via deadsnakes PPA
  - add-apt-repository ppa:deadsnakes/ppa -y
  - apt-get update -y
  - apt-get install -y python3.12 python3.12-venv python3.12-distutils

  # Create deploy user
  - useradd -m -s /bin/bash deploy
  - mkdir -p /home/deploy/.ssh
  - chmod 700 /home/deploy/.ssh

  # Install SSH public key for deploy user
  - echo "${ssh_public_key}" > /home/deploy/.ssh/authorized_keys
  - chmod 600 /home/deploy/.ssh/authorized_keys

  # Install GitHub deploy key
  - |
    cat > /home/deploy/.ssh/id_ed25519 << 'SSHKEY'
    ${gh_deploy_key}
    SSHKEY
  - chmod 600 /home/deploy/.ssh/id_ed25519

  # Configure SSH to use deploy key for github.com
  - |
    cat > /home/deploy/.ssh/config << 'SSHCONFIG'
    Host github.com
      HostName github.com
      User git
      IdentityFile /home/deploy/.ssh/id_ed25519
      StrictHostKeyChecking no
    SSHCONFIG
  - chmod 600 /home/deploy/.ssh/config

  # Clone repo
  - chown -R deploy:deploy /home/deploy/.ssh
  - sudo -u deploy git clone git@github.com:${github_repo}.git /home/deploy/shopping-agent

  # Create virtualenv
  - sudo -u deploy python3.12 -m venv /home/deploy/venv

  # Install app dependencies
  - sudo -u deploy /home/deploy/venv/bin/pip install --upgrade pip
  - sudo -u deploy /home/deploy/venv/bin/pip install -e /home/deploy/shopping-agent

  # Write SSL certificate
  - |
    cat > /etc/ssl/cloudflare-origin.pem << 'CERT'
    ${cf_origin_cert}
    CERT
  - |
    cat > /etc/ssl/cloudflare-origin-key.pem << 'KEY'
    ${cf_origin_key}
    KEY
  - chmod 600 /etc/ssl/cloudflare-origin-key.pem

  # Write nginx config
  - |
    cat > /etc/nginx/sites-available/shopping-agent << 'NGINX'
    server {
        listen 80;
        server_name shopping.aydho.com;
        return 301 https://$host$request_uri;
    }

    server {
        listen 443 ssl;
        server_name shopping.aydho.com;

        ssl_certificate     /etc/ssl/cloudflare-origin.pem;
        ssl_certificate_key /etc/ssl/cloudflare-origin-key.pem;

        location / {
            proxy_pass         http://127.0.0.1:8000;
            proxy_set_header   Host $host;
            proxy_set_header   X-Real-IP $remote_addr;
            proxy_set_header   X-Forwarded-For $proxy_add_x_forwarded_for;
            proxy_set_header   X-Forwarded-Proto $scheme;
        }
    }
    NGINX
  - ln -sf /etc/nginx/sites-available/shopping-agent /etc/nginx/sites-enabled/shopping-agent
  - rm -f /etc/nginx/sites-enabled/default
  - nginx -t

  # Write systemd service
  - |
    cat > /etc/systemd/system/shopping-agent.service << 'SYSTEMD'
    [Unit]
    Description=Shopping Agent
    After=network.target

    [Service]
    User=deploy
    WorkingDirectory=/home/deploy/shopping-agent
    EnvironmentFile=/home/deploy/shopping-agent/.env
    ExecStart=/home/deploy/venv/bin/uvicorn shopping_agent.main:app --host 127.0.0.1 --port 8000
    Restart=always
    RestartSec=5

    [Install]
    WantedBy=multi-user.target
    SYSTEMD

  # Write .env file
  - |
    cat > /home/deploy/shopping-agent/.env << 'ENVFILE'
    DATABASE_URL=${database_url}
    COLES_API_KEY=${coles_api_key}
    WOOLWORTHS_API_KEY=${woolworths_api_key}
    DEBUG=false
    ENVFILE
  - chmod 600 /home/deploy/shopping-agent/.env
  - chown deploy:deploy /home/deploy/shopping-agent/.env

  # Sudoers rule for deploy user to restart service
  - echo "deploy ALL=(ALL) NOPASSWD: /bin/systemctl restart shopping-agent" > /etc/sudoers.d/deploy-restart
  - chmod 440 /etc/sudoers.d/deploy-restart

  # Fix ownership
  - chown -R deploy:deploy /home/deploy/shopping-agent
  - chown -R deploy:deploy /home/deploy/venv

  # Enable and start services
  - systemctl daemon-reload
  - systemctl enable shopping-agent
  - systemctl start shopping-agent
  - systemctl enable nginx
  - systemctl restart nginx

  # Write sentinel file to signal bootstrap complete
  - touch /home/deploy/.cloud-init-done
```

- [ ] **Step 2: Commit**

```bash
git add terraform/cloud-init.yaml.tpl
git commit -m "feat: add cloud-init bootstrap template"
```

---

## Task 8: main.tf — wire cloud-init + Cloudflare DNS

**Files:**
- Modify: `terraform/main.tf`

- [ ] **Step 1: Replace the metadata block in oci_core_instance.app**

Find this block in `main.tf`:
```hcl
  metadata = {
    ssh_authorized_keys = var.ssh_public_key
    # user_data added in Task 9 after cloud-init template is written
  }
```

Replace with:
```hcl
  metadata = {
    ssh_authorized_keys = var.ssh_public_key
    user_data = base64encode(templatefile("${path.module}/cloud-init.yaml.tpl", {
      ssh_public_key     = var.ssh_public_key
      gh_deploy_key      = var.gh_deploy_key
      github_repo        = var.github_repo
      database_url       = var.database_url
      coles_api_key      = var.coles_api_key
      woolworths_api_key = var.woolworths_api_key
      cf_origin_cert     = var.cf_origin_cert
      cf_origin_key      = var.cf_origin_key
    }))
  }
```

- [ ] **Step 2: Append Cloudflare DNS record to main.tf**

```hcl
# --- Cloudflare DNS ---

resource "cloudflare_record" "shopping" {
  zone_id = var.cf_zone_id
  name    = "shopping"
  value   = oci_core_instance.app.public_ip
  type    = "A"
  proxied = true
}
```

- [ ] **Step 3: Validate**

```bash
cd terraform
terraform validate
```

Expected: `Success! The configuration is valid.`

- [ ] **Step 4: Preview full plan**

```bash
terraform plan -var-file=terraform.tfvars
```

Expected: ~9 resources to add (VCN, IGW, route table, security list, subnet, image data, availability domain data, compute instance, Cloudflare DNS record).

- [ ] **Step 5: Commit**

```bash
git add terraform/main.tf
git commit -m "feat: wire cloud-init into instance and add Cloudflare DNS record"
```

---

## Task 9: .github/workflows/provision.yml

**Files:**
- Create: `.github/workflows/provision.yml`

- [ ] **Step 1: Create .github/workflows directory**

```bash
mkdir -p .github/workflows
```

- [ ] **Step 2: Write provision.yml**

```yaml
# .github/workflows/provision.yml
name: Provision Infrastructure

on:
  workflow_dispatch:
    inputs:
      recreate:
        description: 'Force VM recreation (replace existing instance)'
        required: false
        default: 'false'
        type: boolean

jobs:
  provision:
    name: Terraform Apply
    runs-on: ubuntu-latest

    steps:
      - name: Checkout
        uses: actions/checkout@v4

      - name: Setup Terraform
        uses: hashicorp/setup-terraform@v3
        with:
          terraform_version: "~> 1.6"

      - name: Terraform Init
        working-directory: terraform
        env:
          AWS_ACCESS_KEY_ID: ${{ secrets.OCI_S3_ACCESS_KEY }}
          AWS_SECRET_ACCESS_KEY: ${{ secrets.OCI_S3_SECRET_KEY }}
        run: |
          terraform init \
            -backend-config="endpoint=https://${{ secrets.OCI_OBJECT_STORAGE_NAMESPACE }}.compat.objectstorage.ap-melbourne-1.oraclecloud.com"

      - name: Terraform Apply
        working-directory: terraform
        env:
          AWS_ACCESS_KEY_ID: ${{ secrets.OCI_S3_ACCESS_KEY }}
          AWS_SECRET_ACCESS_KEY: ${{ secrets.OCI_S3_SECRET_KEY }}
          TF_VAR_tenancy_ocid: ${{ secrets.OCI_TENANCY_OCID }}
          TF_VAR_user_ocid: ${{ secrets.OCI_USER_OCID }}
          TF_VAR_fingerprint: ${{ secrets.OCI_FINGERPRINT }}
          TF_VAR_private_key: ${{ secrets.OCI_PRIVATE_KEY }}
          TF_VAR_region: "ap-melbourne-1"
          TF_VAR_ssh_public_key: ${{ secrets.SSH_PUBLIC_KEY }}
          TF_VAR_gh_deploy_key: ${{ secrets.GH_DEPLOY_KEY }}
          TF_VAR_github_repo: ${{ secrets.GITHUB_REPO }}
          TF_VAR_database_url: ${{ secrets.DATABASE_URL }}
          TF_VAR_coles_api_key: ${{ secrets.COLES_API_KEY }}
          TF_VAR_woolworths_api_key: ${{ secrets.WOOLWORTHS_API_KEY }}
          TF_VAR_cf_api_token: ${{ secrets.CF_API_TOKEN }}
          TF_VAR_cf_zone_id: ${{ secrets.CF_ZONE_ID }}
          TF_VAR_cf_origin_cert: ${{ secrets.CF_ORIGIN_CERT }}
          TF_VAR_cf_origin_key: ${{ secrets.CF_ORIGIN_KEY }}
        run: |
          if [ "${{ inputs.recreate }}" = "true" ]; then
            terraform apply -replace=oci_core_instance.app -auto-approve
          else
            terraform apply -auto-approve
          fi

      - name: Output VM IP
        working-directory: terraform
        env:
          AWS_ACCESS_KEY_ID: ${{ secrets.OCI_S3_ACCESS_KEY }}
          AWS_SECRET_ACCESS_KEY: ${{ secrets.OCI_S3_SECRET_KEY }}
        run: |
          echo "## Provisioning Complete" >> $GITHUB_STEP_SUMMARY
          echo "VM IP: $(terraform output -raw vm_public_ip)" >> $GITHUB_STEP_SUMMARY
          echo "SSH: $(terraform output -raw ssh_command)" >> $GITHUB_STEP_SUMMARY
          echo "" >> $GITHUB_STEP_SUMMARY
          echo "⚠️ Wait ~5 minutes for cloud-init to complete before deploying code." >> $GITHUB_STEP_SUMMARY
```

- [ ] **Step 3: Commit**

```bash
git add .github/workflows/provision.yml
git commit -m "feat: add provision.yml GitHub Actions workflow"
```

---

## Task 10: .github/workflows/deploy.yml

**Files:**
- Create: `.github/workflows/deploy.yml`

- [ ] **Step 1: Write deploy.yml**

```yaml
# .github/workflows/deploy.yml
name: Deploy

on:
  push:
    branches: [main]

jobs:
  deploy:
    name: SSH Deploy
    runs-on: ubuntu-latest

    steps:
      - name: Deploy to VM
        uses: appleboy/ssh-action@v1
        with:
          host: ${{ secrets.VM_IP }}
          username: deploy
          key: ${{ secrets.SSH_PRIVATE_KEY }}
          port: 22
          script: |
            set -e
            cd /home/deploy/shopping-agent
            git pull origin main
            /home/deploy/venv/bin/pip install -e .
            /home/deploy/venv/bin/alembic upgrade head
            sudo systemctl restart shopping-agent
            echo "Deploy complete"
```

Note: `VM_IP` must be set manually as a GitHub secret after first provisioning. Update it after any reprovision that changes the IP.

- [ ] **Step 2: Commit**

```bash
git add .github/workflows/deploy.yml
git commit -m "feat: add deploy.yml GitHub Actions workflow"
```

---

## Task 11: GitHub Secrets Checklist

Set all of these in GitHub → repo → Settings → Secrets and variables → Actions:

| Secret | Value |
|--------|-------|
| `OCI_TENANCY_OCID` | From OCI Console → Tenancy |
| `OCI_USER_OCID` | From OCI Console → Profile |
| `OCI_FINGERPRINT` | From OCI Console → Profile → API Keys |
| `OCI_PRIVATE_KEY` | PEM content of OCI API private key |
| `OCI_S3_ACCESS_KEY` | Customer Secret Key ID (OCI Console → Profile → Customer Secret Keys) |
| `OCI_S3_SECRET_KEY` | Customer Secret Key value |
| `OCI_OBJECT_STORAGE_NAMESPACE` | Object Storage namespace (shown in OCI Console → Object Storage) |
| `SSH_PRIVATE_KEY` | Private key for `deploy` user SSH access (used by deploy.yml) |
| `SSH_PUBLIC_KEY` | Corresponding public key (installed on VM by cloud-init) |
| `GH_DEPLOY_KEY` | Private ed25519 key for GitHub repo clone |
| `CF_API_TOKEN` | Cloudflare API token (DNS Edit scope on aydho.com only) |
| `CF_ZONE_ID` | Cloudflare zone ID for aydho.com |
| `CF_ORIGIN_CERT` | Cloudflare Origin CA certificate PEM |
| `CF_ORIGIN_KEY` | Cloudflare Origin CA private key PEM |
| `DATABASE_URL` | Supabase connection string |
| `COLES_API_KEY` | Coles API key |
| `WOOLWORTHS_API_KEY` | Woolworths API key |
| `GITHUB_REPO` | `owner/shopping-agent` (your GitHub username + repo) |
| `VM_IP` | Set after first `terraform apply` (get from job summary) |

- [ ] **Step 1: Set all secrets above in GitHub Actions**

- [ ] **Step 2: Create Object Storage bucket in OCI Console**

OCI Console → Object Storage & Archive Storage → Object Storage → Create Bucket
- Name: `terraform-state-shopping-agent`
- Region: ap-melbourne-1
- Storage tier: Standard

- [ ] **Step 3: Create Customer Secret Keys in OCI Console**

OCI Console → Profile → Customer Secret Keys → Generate Secret Key
- Note both the Access Key (shown in UI) and Secret Key (shown once at creation)
- Set as `OCI_S3_ACCESS_KEY` and `OCI_S3_SECRET_KEY`

---

## Task 12: First deploy end-to-end

- [ ] **Step 1: Run provision workflow**

GitHub → Actions → Provision Infrastructure → Run workflow (leave `recreate` as false)

Expected: workflow completes in ~3 minutes, job summary shows VM IP

- [ ] **Step 2: Set VM_IP secret**

Copy IP from job summary → GitHub Secrets → set `VM_IP`

- [ ] **Step 3: Wait for cloud-init**

```bash
ssh deploy@<VM_IP> "tail -f /var/log/cloud-init-output.log"
```

Wait until you see the sentinel: `touch /home/deploy/.cloud-init-done`

Or poll:
```bash
ssh deploy@<VM_IP> "until [ -f /home/deploy/.cloud-init-done ]; do sleep 10; echo waiting...; done; echo done"
```

- [ ] **Step 4: Verify services**

```bash
ssh deploy@<VM_IP> "systemctl status shopping-agent nginx"
```

Expected: both `active (running)`

- [ ] **Step 5: Verify site**

```bash
curl -I https://shopping.aydho.com/healthz
```

Expected: HTTP 200

- [ ] **Step 6: Trigger a code deploy**

Make a trivial commit and push to `main`. Watch the deploy workflow complete successfully.

- [ ] **Step 7: Verify reprovision works**

GitHub → Actions → Provision Infrastructure → Run workflow with `recreate=true`

Expected: new VM, Cloudflare DNS updated automatically, site back up after ~5 minutes.

---

## Notes on ARM A1 Capacity

If `terraform apply` fails with:
```
Error: 500-InternalError ... Out of host capacity
```

This means OCI has no ARM capacity available at this moment in Melbourne. Options:
1. Retry after a few hours (capacity is released as others stop their instances)
2. Try at off-peak times (early morning AEST)
3. The error is transient — just re-run the workflow
