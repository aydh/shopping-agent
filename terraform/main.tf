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
}

# --- Cloudflare DNS ---

resource "cloudflare_record" "shopping" {
  zone_id = var.cf_zone_id
  name    = "shopping"
  value   = oci_core_instance.app.public_ip
  type    = "A"
  proxied = true
}
