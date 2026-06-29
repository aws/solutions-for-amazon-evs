# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

###############################################################################
# Terraform & Provider Configuration
###############################################################################

terraform {
  required_version = ">= 1.5"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = ">= 5.0"
    }
    awscc = {
      source  = "hashicorp/awscc"
      version = ">= 1.0"
    }
  }
}

provider "aws" {
  region = var.region
}

provider "awscc" {
  region = var.region
}

###############################################################################
# Module 1: EVS Networking
###############################################################################

module "base_aws_infrastructure" {
  source = "./modules/base_aws_infrastructure"

  region            = var.region
  environment       = var.environment
  availability_zone = var.availability_zone
  fqdn              = var.fqdn
  cidr_prefix       = var.cidr_prefix
  create_tgw        = var.create_tgw
  enable_public_hcx = var.enable_public_hcx

  esxi01_name            = var.esxi01_name
  esxi02_name            = var.esxi02_name
  esxi03_name            = var.esxi03_name
  vc_name                = var.vc_name
  nsx_name               = var.nsx_name
  sddcm_name             = var.sddcm_name
  cb_name                = var.cb_name
  edge01_name            = var.edge01_name
  edge02_name            = var.edge02_name
  nsx01_name             = var.nsx01_name
  nsx02_name             = var.nsx02_name
  nsx03_name             = var.nsx03_name
  vcf_ops_name           = var.vcf_ops_name
  vcf_ops_01_name        = var.vcf_ops_01_name
  vcf_ops_02_name        = var.vcf_ops_02_name
  vcf_ops_03_name        = var.vcf_ops_03_name
  vcf_ops_collector_name = var.vcf_ops_collector_name
  vcf_fleet_name         = var.vcf_fleet_name
  vsp_platform_name      = var.vsp_platform_name
  vsp_instance_name      = var.vsp_instance_name
  vsp_fleet_name         = var.vsp_fleet_name
  vidb_name              = var.vidb_name
  vcf_license_name       = var.vcf_license_name
  vcf_logs_name          = var.vcf_logs_name
  vcf_auto_platform_name = var.vcf_auto_platform_name
  vcf_auto_name          = var.vcf_auto_name
  vcf_sddcm01_name       = var.vcf_sddcm01_name
  ops_mgr_name           = var.ops_mgr_name
  ops_collector_name     = var.ops_collector_name
  ops_mgr_replica_name   = var.ops_mgr_replica_name
  ops_mgr_data01_name    = var.ops_mgr_data01_name
}

###############################################################################
# Module 2: Jumpbox (conditional)
###############################################################################

module "jumpbox" {
  source = "./modules/jumpbox"
  count  = var.create_jumpbox ? 1 : 0

  vpc_id                        = module.base_aws_infrastructure.vpc_id
  vpc_cidr_block                = module.base_aws_infrastructure.vpc_cidr_block
  vpc_default_security_group_id = module.base_aws_infrastructure.vpc_default_security_group_id
  evs_security_group_id         = module.base_aws_infrastructure.evs_security_group_id
  internet_gateway_id           = module.base_aws_infrastructure.internet_gateway_id
  environment                   = var.environment
  availability_zone             = var.availability_zone
  subnet_cidr                   = "${var.cidr_prefix}200.0/24"
  instance_type                 = var.jumpbox_instance_type
}

###############################################################################
# Route Server propagation for the jumpbox route table
#
# The base module attaches the Route Server to the service_access and public
# route tables. The jumpbox lives in its own subnet with its own route table
# (Jumpbox-RTB), which the base module doesn't know about. Without this
# propagation, NSX-advertised CIDRs (T0-connected segments, T1-connected
# segments) won't show up on the jumpbox after BGP comes up — the jumpbox
# can reach VCF management VMs over their VLAN-20 IPs but can't reach
# anything that lives behind the T0/T1.
#
# Conditional on create_jumpbox so it disappears with the jumpbox itself.
###############################################################################

resource "awscc_ec2_route_server_propagation" "jumpbox" {
  count = var.create_jumpbox ? 1 : 0

  route_server_id = module.base_aws_infrastructure.route_server_id
  route_table_id  = module.jumpbox[0].route_table_id

  depends_on = [module.base_aws_infrastructure]
}
