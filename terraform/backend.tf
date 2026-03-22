# terraform/backend.tf
# Credentials and endpoint passed via -backend-config flags or backend.tfvars
# See backend.tfvars.example for local setup

terraform {
  backend "s3" {
    bucket                      = "terraform-state-shopping-agent"
    key                         = "terraform.tfstate"
    region                      = "ap-melbourne-1"
    skip_credentials_validation = true
    skip_metadata_api_check     = true
    skip_region_validation      = true
    force_path_style            = true
  }
}
