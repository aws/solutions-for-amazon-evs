# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

output "instance_id" {
  description = "EC2 instance ID of the jumpbox"
  value       = aws_instance.jumpbox.id
}

output "public_ip" {
  description = "Public IP address of the jumpbox"
  value       = aws_instance.jumpbox.public_ip
}

output "security_group_id" {
  description = "Security group ID for the jumpbox"
  value       = aws_security_group.jumpbox.id
}

output "subnet_id" {
  description = "Subnet ID of the jumpbox public subnet"
  value       = aws_subnet.jumpbox.id
}

output "route_table_id" {
  description = "Route table ID for the jumpbox subnet (used at the root to attach Route Server propagation)"
  value       = aws_route_table.jumpbox.id
}

output "key_pair_name" {
  description = "Name of the jumpbox key pair. The matching private key lives in SSM at /ec2/keypair/<key_pair_id>; treat as sensitive."
  value       = awscc_ec2_key_pair.jumpbox.key_name
  sensitive   = true
}

output "key_pair_id" {
  description = "ID of the jumpbox key pair (used for SSM parameter lookup)"
  value       = awscc_ec2_key_pair.jumpbox.key_pair_id
}
