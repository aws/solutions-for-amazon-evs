# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

###############################################################################
# jumpbox module — plan-level smoke test.
#
# The jumpbox module assumes upstream resources (VPC, IGW, default and EVS
# security groups) already exist and are passed in as IDs. mock_provider
# blocks substitute for real AWS/AWSCC providers so no credentials are
# required.
###############################################################################

mock_provider "aws" {}
mock_provider "awscc" {}

variables {
  vpc_id                        = "vpc-0123456789abcdef0"
  internet_gateway_id           = "igw-0123456789abcdef0"
  vpc_cidr_block                = "10.0.0.0/16"
  vpc_default_security_group_id = "sg-0123456789abcdef0"
  evs_security_group_id         = "sg-abcdef0123456789a"
  availability_zone             = "us-east-1a"
}

run "defaults_plan_succeeds" {
  command = plan
}
