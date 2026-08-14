# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

output "vpc_id" {
  description = "ID of the underlay VPC"
  value       = aws_vpc.underlay.id
}

output "vpc_cidr_block" {
  description = "Primary CIDR block of the underlay VPC"
  value       = aws_vpc.underlay.cidr_block
}

output "vpc_default_security_group_id" {
  description = "Default security group ID of the underlay VPC"
  value       = aws_vpc.underlay.default_security_group_id
}

output "evs_security_group_id" {
  description = "Security group used by EVS service access and the Route 53 inbound resolver"
  value       = aws_security_group.evs.id
}

output "service_access_subnet_id" {
  description = "ID of the service access subnet"
  value       = aws_subnet.service_access.id
}

output "service_access_route_table_id" {
  description = "ID of the service access route table (EVS VLAN subnets are associated with this)"
  value       = aws_route_table.service_access.id
}

output "forward_zone_id" {
  description = "Route 53 forward private hosted zone ID"
  value       = aws_route53_zone.forward.zone_id
}

output "reverse_zone_id" {
  description = "Route 53 reverse private hosted zone ID"
  value       = aws_route53_zone.reverse.zone_id
}

output "route_server_id" {
  description = "ID of the VPC Route Server"
  value       = awscc_ec2_route_server.evs.route_server_id
}

output "route_server_peer01_id" {
  description = "ID of Route Server BGP peer 01"
  value       = awscc_ec2_route_server_peer.peer01.route_server_peer_id
}

output "route_server_peer02_id" {
  description = "ID of Route Server BGP peer 02"
  value       = awscc_ec2_route_server_peer.peer02.route_server_peer_id
}

output "route_server_endpoint01_ip" {
  description = "ENI IP address of Route Server Endpoint 01 (BGP peer IP for NSX edges)"
  value       = awscc_ec2_route_server_endpoint.ep01.eni_address
}

output "route_server_endpoint02_ip" {
  description = "ENI IP address of Route Server Endpoint 02 (BGP peer IP for NSX edges)"
  value       = awscc_ec2_route_server_endpoint.ep02.eni_address
}

output "key_pair_name" {
  description = "Name of the EC2 key pair. The matching private key lives in SSM Parameter Store at /ec2/keypair/<key_pair_id>; treat as sensitive."
  value       = awscc_ec2_key_pair.evs.key_name
  sensitive   = true
}

output "transit_gateway_id" {
  description = "ID of the Transit Gateway when create_tgw is true; null otherwise"
  value       = var.create_tgw ? aws_ec2_transit_gateway.evs[0].id : null
}

output "internet_gateway_id" {
  description = "ID of the Internet Gateway attached to the underlay VPC"
  value       = aws_internet_gateway.evs.id
}

output "hcx_public_cidr" {
  description = "Public /28 CIDR block allocated from the IPAM pool for the HCX VLAN. Null when enable_public_hcx is false."
  value       = var.enable_public_hcx ? aws_vpc_ipam_pool_cidr.public_hcx[0].cidr : null
}

output "hcx_network_acl_id" {
  description = "Network ACL ID for the HCX VLAN. Null when enable_public_hcx is false."
  value       = var.enable_public_hcx ? aws_network_acl.public_hcx[0].id : null
}

output "hcx_eip_allocation_id" {
  description = "Allocation ID of the EIP from the public HCX IPAM pool. Null when enable_public_hcx is false. Phase 2 reads this and calls evs:AssociateEipToVlan after CreateEnvironment."
  value       = var.enable_public_hcx ? aws_eip.public_hcx[0].allocation_id : null
}
