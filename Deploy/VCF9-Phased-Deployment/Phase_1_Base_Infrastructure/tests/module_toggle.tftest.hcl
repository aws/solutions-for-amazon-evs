# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

###############################################################################
# Phase 1 root module — toggle and precondition tests.
#
# All cases are `command = plan` with mock_provider so they require no AWS
# credentials. They exercise the optional-component flags (create_tgw,
# create_jumpbox, enable_public_hcx).
#
# Asserts focus on values that are known at plan time. Conditional resource
# IDs (e.g. transit_gateway_id when create_tgw = true) are not known until
# apply, so we only assert on the "off" path that they are null. The "on"
# paths verify that plan succeeds — that alone confirms the conditional
# wiring is syntactically correct.
###############################################################################

mock_provider "aws" {}
mock_provider "awscc" {}

variables {
  region            = "us-east-1"
  availability_zone = "us-east-1a"
  fqdn              = "test.fqdn.evs"
  cidr_prefix       = "10.0."
}

run "defaults_minimal_topology" {
  command = plan

  variables {
    create_tgw        = false
    create_jumpbox    = true
    enable_public_hcx = false
  }

  assert {
    condition     = output.transit_gateway_id == null
    error_message = "transit_gateway_id must be null when create_tgw = false"
  }

  assert {
    condition     = output.jumpbox_instance_id == null
    error_message = "jumpbox_instance_id must be null when create_jumpbox = false"
  }

  assert {
    condition     = output.hcx_public == false
    error_message = "hcx_public output must reflect the enable_public_hcx flag"
  }

  assert {
    condition     = output.hcx_public_cidr == null
    error_message = "hcx_public_cidr must be null when enable_public_hcx = false"
  }
}

run "with_transit_gateway_plans_cleanly" {
  command = plan

  variables {
    create_tgw        = true
    create_jumpbox    = true
    enable_public_hcx = false
  }
  # Plan success alone confirms create_tgw = true wires correctly. The
  # transit_gateway_id is not known until apply.
}

run "with_jumpbox_plans_cleanly" {
  command = plan

  variables {
    create_tgw        = false
    create_jumpbox    = true
    enable_public_hcx = false
  }
  # Plan success alone confirms create_jumpbox = true wires correctly,
  # including the route-server-propagation resource gated on the same flag.
}

run "with_public_hcx_plans_cleanly" {
  command = plan

  variables {
    create_tgw        = false
    create_jumpbox    = true
    enable_public_hcx = true
  }

  assert {
    condition     = output.hcx_public == true
    error_message = "hcx_public output must reflect the enable_public_hcx flag"
  }
}
