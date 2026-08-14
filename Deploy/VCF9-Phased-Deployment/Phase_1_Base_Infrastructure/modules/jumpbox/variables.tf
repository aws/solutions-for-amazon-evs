# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

variable "vpc_id" {
  type        = string
  description = "VPC ID to deploy the jumpbox into"
}

variable "environment" {
  type        = string
  default     = "dev"
  description = "Environment name applied to the Environment tag on every resource (e.g., dev, staging, prod)"
}

variable "internet_gateway_id" {
  type        = string
  description = "Internet Gateway ID for the public subnet route"
}

variable "vpc_cidr_block" {
  type        = string
  description = "CIDR block of the VPC — used to allow ingress from VPC-internal sources"
}

variable "vpc_default_security_group_id" {
  type        = string
  description = "Default security group ID of the VPC — jumpbox SG will be allowed to ingress"
}

variable "evs_security_group_id" {
  type        = string
  description = "EVS service-access security group ID — jumpbox SG will be allowed to ingress"
}

variable "availability_zone" {
  type        = string
  description = "Availability zone for the jumpbox subnet"
}

variable "subnet_cidr" {
  type        = string
  default     = "10.0.200.0/24"
  description = "CIDR block for the jumpbox public subnet"
}

variable "instance_type" {
  type        = string
  default     = "t3.2xlarge"
  description = "EC2 instance type for the jumpbox"
}
