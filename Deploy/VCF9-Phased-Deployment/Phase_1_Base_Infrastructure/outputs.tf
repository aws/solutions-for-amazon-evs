# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

###############################################################################
# Base AWS Infrastructure Outputs
###############################################################################

output "region" {
  description = "AWS region"
  value       = var.region
}

output "fqdn" {
  description = "FQDN / domain suffix for EVS appliances and hosts"
  value       = var.fqdn
}

output "vpc_id" {
  description = "ID of the underlay VPC"
  value       = module.base_aws_infrastructure.vpc_id
}

output "vpc_cidr_block" {
  description = "CIDR block of the underlay VPC"
  value       = module.base_aws_infrastructure.vpc_cidr_block
}

output "vpc_default_security_group_id" {
  description = "Default security group ID of the underlay VPC"
  value       = module.base_aws_infrastructure.vpc_default_security_group_id
}

output "evs_security_group_id" {
  description = "Security group used by EVS service access and the R53 inbound resolver"
  value       = module.base_aws_infrastructure.evs_security_group_id
}

output "service_access_subnet_id" {
  description = "ID of the service access subnet"
  value       = module.base_aws_infrastructure.service_access_subnet_id
}

output "service_access_route_table_id" {
  description = "ID of the service access route table (EVS VLAN subnets will be associated with this)"
  value       = module.base_aws_infrastructure.service_access_route_table_id
}

output "forward_zone_id" {
  description = "Route 53 forward hosted zone ID"
  value       = module.base_aws_infrastructure.forward_zone_id
}

output "reverse_zone_id" {
  description = "Route 53 reverse hosted zone ID"
  value       = module.base_aws_infrastructure.reverse_zone_id
}

output "route_server_id" {
  description = "ID of the VPC Route Server"
  value       = module.base_aws_infrastructure.route_server_id
}

output "route_server_peer01_id" {
  description = "ID of Route Server Peer 01"
  value       = module.base_aws_infrastructure.route_server_peer01_id
}

output "route_server_peer02_id" {
  description = "ID of Route Server Peer 02"
  value       = module.base_aws_infrastructure.route_server_peer02_id
}

output "route_server_endpoint01_ip" {
  description = "ENI IP address of Route Server Endpoint 01 (BGP peer IP for NSX edges)"
  value       = module.base_aws_infrastructure.route_server_endpoint01_ip
}

output "route_server_endpoint02_ip" {
  description = "ENI IP address of Route Server Endpoint 02 (BGP peer IP for NSX edges)"
  value       = module.base_aws_infrastructure.route_server_endpoint02_ip
}

output "key_pair_name" {
  description = "Name of the EC2 key pair. The matching private key lives in SSM Parameter Store at /ec2/keypair/<key_pair_id>; treat as sensitive."
  value       = module.base_aws_infrastructure.key_pair_name
  sensitive   = true
}

output "transit_gateway_id" {
  description = "ID of the Transit Gateway (if created)"
  value       = module.base_aws_infrastructure.transit_gateway_id
}

output "hcx_public_cidr" {
  description = "Public /28 CIDR allocated for the HCX VLAN (null if disabled)"
  value       = module.base_aws_infrastructure.hcx_public_cidr
}

output "hcx_network_acl_id" {
  description = "Network ACL ID for the HCX VLAN (null if disabled)"
  value       = module.base_aws_infrastructure.hcx_network_acl_id
}

output "hcx_eip_allocation_id" {
  description = "Elastic IP allocation ID associated to the HCX VLAN (null if disabled)"
  value       = module.base_aws_infrastructure.hcx_eip_allocation_id
}

###############################################################################
# VCF Hostname Outputs
###############################################################################

output "vcf_hostnames" {
  description = "Short hostnames for VCF appliances"
  value = {
    vcenter           = var.vc_name
    nsx               = var.nsx_name
    sddc_manager      = var.sddcm_name
    cloud_builder     = var.cb_name
    edge01            = var.edge01_name
    edge02            = var.edge02_name
    nsx01             = var.nsx01_name
    nsx02             = var.nsx02_name
    nsx03             = var.nsx03_name
    vcf_ops           = var.vcf_ops_name
    vcf_ops_01        = var.vcf_ops_01_name
    vcf_ops_02        = var.vcf_ops_02_name
    vcf_ops_03        = var.vcf_ops_03_name
    vcf_ops_collector = var.vcf_ops_collector_name
    vcf_fleet         = var.vcf_fleet_name
    vsp_platform      = var.vsp_platform_name
    vsp_instance      = var.vsp_instance_name
    vsp_fleet         = var.vsp_fleet_name
    vcf_license       = var.vcf_license_name
    vcf_vidb          = var.vidb_name
    vcf_logs          = var.vcf_logs_name
    vcf_auto_platform = var.vcf_auto_platform_name
    vcf_auto          = var.vcf_auto_name
    vcf_sddcm01       = var.vcf_sddcm01_name
    ops_mgr           = var.ops_mgr_name
    ops_collector     = var.ops_collector_name
    ops_mgr_replica   = var.ops_mgr_replica_name
    ops_mgr_data01    = var.ops_mgr_data01_name
  }
}

output "esxi_hostnames" {
  description = "Short hostnames for ESXi hosts (in provisioning order)"
  value       = [var.esxi01_name, var.esxi02_name, var.esxi03_name]
}

###############################################################################
# Jumpbox Outputs
###############################################################################

output "jumpbox_instance_id" {
  description = "EC2 instance ID of the jumpbox (if created)"
  value       = var.create_jumpbox ? module.jumpbox[0].instance_id : null
}

output "jumpbox_public_ip" {
  description = "Public IP address of the jumpbox (if created)"
  value       = var.create_jumpbox ? module.jumpbox[0].public_ip : null
}

output "jumpbox_key_pair_id" {
  description = "ID of the jumpbox key pair (private key at /ec2/keypair/<id> in SSM)"
  value       = var.create_jumpbox ? module.jumpbox[0].key_pair_id : null
}

###############################################################################
# HCX Public Internet Connectivity
###############################################################################

output "hcx_public" {
  description = "Whether HCX public internet connectivity is enabled"
  value       = var.enable_public_hcx
}
