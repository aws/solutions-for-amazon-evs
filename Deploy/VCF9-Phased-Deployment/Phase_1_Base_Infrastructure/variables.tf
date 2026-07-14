# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

variable "region" {
  type        = string
  default     = "us-east-1"
  description = "AWS region to deploy into"
}

variable "environment" {
  type        = string
  default     = "dev"
  description = "Environment name applied to the Environment tag on every resource (e.g., dev, staging, prod)"
}

variable "availability_zone" {
  type        = string
  description = "Availability Zone to select from the current working region"
}

variable "fqdn" {
  type        = string
  default     = "my.fqdn.evs"
  description = "FQDN for the Route 53 forward hosted zone"
}

variable "cidr_prefix" {
  type        = string
  default     = "10.0."
  description = "Base CIDR prefix for the underlay VPC (e.g. '10.0.')"
}

variable "esxi01_name" {
  type        = string
  default     = "esxi01"
  description = "DNS name for esxi01"
}

variable "esxi02_name" {
  type        = string
  default     = "esxi02"
  description = "DNS name for esxi02"
}

variable "esxi03_name" {
  type        = string
  default     = "esxi03"
  description = "DNS name for esxi03"
}

variable "esxi04_name" {
  type        = string
  default     = "esxi04"
  description = "DNS name for esxi04"
}

variable "vc_name" {
  type        = string
  default     = "vc"
  description = "DNS name for vCenter"
}

variable "nsx_name" {
  type        = string
  default     = "nsx"
  description = "DNS name for NSX Manager cluster"
}

variable "sddcm_name" {
  type        = string
  default     = "sddcm"
  description = "DNS name for SDDC Manager"
}

variable "cb_name" {
  type        = string
  default     = "cb"
  description = "DNS name for Cloud Builder"
}

variable "edge01_name" {
  type        = string
  default     = "edge01"
  description = "DNS name for edge01"
}

variable "edge02_name" {
  type        = string
  default     = "edge02"
  description = "DNS name for edge02"
}

variable "nsx01_name" {
  type        = string
  default     = "nsx01"
  description = "DNS name for NSX Manager 01 appliance"
}

variable "nsx02_name" {
  type        = string
  default     = "nsx02"
  description = "DNS name for NSX Manager 02 appliance"
}

variable "nsx03_name" {
  type        = string
  default     = "nsx03"
  description = "DNS name for NSX Manager 03 appliance"
}

variable "vcf_ops_name" {
  type        = string
  default     = "vcfops"
  description = "DNS name for VCF Operations VIP"
}

variable "vcf_ops_01_name" {
  type        = string
  default     = "vcfops01"
  description = "DNS name for VCF Operations node 01"
}

variable "vcf_ops_02_name" {
  type        = string
  default     = "vcfops02"
  description = "DNS name for VCF Operations node 02"
}

variable "vcf_ops_03_name" {
  type        = string
  default     = "vcfops03"
  description = "DNS name for VCF Operations node 03"
}

variable "vcf_ops_collector_name" {
  type        = string
  default     = "vcfopscol"
  description = "DNS name for VCF Operations Collector"
}

variable "vcf_fleet_name" {
  type        = string
  default     = "vcffleet"
  description = "DNS name for VCF Fleet Manager"
}

variable "vsp_platform_name" {
  type        = string
  default     = "vsp-platform"
  description = "DNS name for VSP platform (VCF 9.1 service runtime)"
}

variable "vsp_instance_name" {
  type        = string
  default     = "vsp-instance"
  description = "DNS name for VSP instance components (VCF 9.1)"
}

variable "vsp_fleet_name" {
  type        = string
  default     = "vsp-fleet"
  description = "DNS name for VSP fleet components (VCF 9.1)"
}

variable "vidb_name" {
  type        = string
  default     = "vcf-vidb"
  description = "DNS name for Virtual Identity Broker (VCF 9.1)"
}

variable "vcf_license_name" {
  type        = string
  default     = "vcf-license"
  description = "DNS name for VCF License Server (VCF 9.1)"
}

variable "vcf_logs_name" {
  type        = string
  default     = "vcf-logs"
  description = "DNS name for VCF Logs component (VCF 9.1)"
}

variable "vcf_auto_platform_name" {
  type        = string
  default     = "vcf-auto-platform"
  description = "DNS name for VCF Automation platform (VCF 9.1)"
}

variable "vcf_auto_name" {
  type        = string
  default     = "vcf-auto"
  description = "DNS name for VCF Automation (VCF 9.1)"
}

variable "vcf_sddcm01_name" {
  type        = string
  default     = "vcf-sddcm01"
  description = "DNS name for SDDC Manager node 01 (VCF 9.1)"
}

variable "ops_mgr_name" {
  type        = string
  default     = "ops-mgr"
  description = "DNS name for VCF Operations Manager VIP"
}

variable "ops_collector_name" {
  type        = string
  default     = "ops-collector"
  description = "DNS name for VCF Operations Collector"
}

variable "ops_mgr_replica_name" {
  type        = string
  default     = "ops-mgr-replica"
  description = "DNS name for VCF Operations Manager replica node (VCF 9.1)"
}

variable "ops_mgr_data01_name" {
  type        = string
  default     = "ops-mgr-data01"
  description = "DNS name for VCF Operations Manager data node (VCF 9.1)"
}

variable "create_tgw" {
  type        = bool
  default     = false
  description = "Whether to create a Transit Gateway and VPC attachment"
}

variable "enable_public_hcx" {
  type        = bool
  default     = false
  description = "Whether to provision public IPAM/EIP/NACL for the HCX VLAN"
}

variable "create_jumpbox" {
  type        = bool
  default     = true
  description = "Whether to create the Windows jumpbox instance"
}

variable "jumpbox_instance_type" {
  type        = string
  default     = "t3.2xlarge"
  description = "EC2 instance type for the jumpbox"
}
