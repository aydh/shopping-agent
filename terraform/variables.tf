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
  description = "Cloudflare API token scoped to DNS Edit on aydho.com only"
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
