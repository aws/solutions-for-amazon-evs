# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

variable "region" { type = string }
variable "environment" {
  type        = string
  default     = "dev"
  description = "Environment name applied to the Environment tag on every resource (e.g., dev, staging, prod)"
}
variable "availability_zone" { type = string }
variable "fqdn" { type = string }
variable "cidr_prefix" { type = string }
variable "esxi01_name" { type = string }
variable "esxi02_name" { type = string }
variable "esxi03_name" { type = string }
variable "vc_name" { type = string }
variable "nsx_name" { type = string }
variable "sddcm_name" { type = string }
variable "cb_name" { type = string }
variable "edge01_name" { type = string }
variable "edge02_name" { type = string }
variable "nsx01_name" { type = string }
variable "nsx02_name" { type = string }
variable "nsx03_name" { type = string }

variable "vcf_ops_name" { type = string }

variable "vcf_ops_01_name" { type = string }

variable "vcf_ops_02_name" { type = string }

variable "vcf_ops_03_name" { type = string }

variable "vcf_ops_collector_name" { type = string }

variable "vcf_fleet_name" { type = string }

variable "vsp_platform_name" { type = string }

variable "vsp_instance_name" { type = string }

variable "vsp_fleet_name" { type = string }

variable "vidb_name" { type = string }

variable "vcf_license_name" { type = string }

variable "vcf_logs_name" { type = string }

variable "vcf_auto_platform_name" { type = string }

variable "vcf_auto_name" { type = string }

variable "vcf_sddcm01_name" { type = string }

variable "ops_mgr_name" { type = string }

variable "ops_collector_name" { type = string }

variable "ops_mgr_replica_name" { type = string }

variable "ops_mgr_data01_name" { type = string }

variable "create_tgw" {
  type    = bool
  default = false
}

variable "enable_public_hcx" {
  type        = bool
  default     = false
  description = "When true, provision public IPAM/EIP/NACL for the HCX VLAN"
}
