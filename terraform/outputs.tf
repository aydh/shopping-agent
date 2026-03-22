# terraform/outputs.tf

output "vm_public_ip" {
  description = "Public IP address of the shopping-agent VM"
  value       = oci_core_instance.app.public_ip
}

output "ssh_command" {
  description = "SSH command to connect to the VM"
  value       = "ssh deploy@${oci_core_instance.app.public_ip}"
}
