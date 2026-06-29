# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

###############################################################################
# base_aws_infrastructure module — plan-level smoke tests.
#
# Covers the default topology, the create_tgw toggle, and the public-HCX
# flow. mock_provider blocks substitute for real AWS/AWSCC providers so no
# AWS credentials are required.
###############################################################################

mock_provider "aws" {}
mock_provider "awscc" {}

variables {
  region            = "us-east-1"
  environment       = "test"
  availability_zone = "us-east-1a"
  fqdn              = "test.fqdn.evs"
  cidr_prefix       = "10.0."

  esxi01_name = "esxi01"
  esxi02_name = "esxi02"
  esxi03_name = "esxi03"

  vc_name    = "vc"
  nsx_name   = "nsx"
  sddcm_name = "sddcm"
  cb_name    = "cb"

  edge01_name = "edge01"
  edge02_name = "edge02"

  nsx01_name = "nsx01"
  nsx02_name = "nsx02"
  nsx03_name = "nsx03"

  vcf_ops_name           = "vcfops"
  vcf_ops_01_name        = "vcfops01"
  vcf_ops_02_name        = "vcfops02"
  vcf_ops_03_name        = "vcfops03"
  vcf_ops_collector_name = "vcfopscol"
  vcf_fleet_name         = "vcffleet"
}

run "defaults_plan_succeeds" {
  command = plan

  variables {
    create_tgw        = false
    enable_public_hcx = false
  }

  assert {
    condition     = output.transit_gateway_id == null
    error_message = "transit_gateway_id must be null when create_tgw = false"
  }

  assert {
    condition     = output.hcx_public_cidr == null
    error_message = "hcx_public_cidr must be null when enable_public_hcx = false"
  }

  assert {
    condition     = output.hcx_network_acl_id == null
    error_message = "hcx_network_acl_id must be null when enable_public_hcx = false"
  }

  assert {
    condition     = output.hcx_eip_allocation_id == null
    error_message = "hcx_eip_allocation_id must be null when enable_public_hcx = false"
  }
}

run "with_transit_gateway_plans_cleanly" {
  command = plan

  variables {
    create_tgw        = true
    enable_public_hcx = false
  }
  # Plan success alone confirms create_tgw = true wires correctly.
}

run "with_public_hcx_plans_cleanly" {
  command = plan

  variables {
    create_tgw        = false
    enable_public_hcx = true
  }
  # Plan success alone confirms the IPAM/EIP/NACL stack in public_hcx.tf
  # wires correctly. The CIDR/EIP/NACL IDs are not known until apply.
}
