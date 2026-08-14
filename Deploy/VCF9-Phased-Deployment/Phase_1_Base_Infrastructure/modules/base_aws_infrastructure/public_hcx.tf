# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

###############################################################################
# Public HCX (conditional)
#
# When enable_public_hcx = true, EVS expects:
#   - the HCX VLAN to use a public /28 CIDR block that is also a secondary
#     CIDR on the VPC,
#   - a Network ACL ID supplied as initialVlans.hcxNetworkAclId,
#   - an Elastic IP (allocated from the same public IPAM pool) which is
#     associated to the HCX VLAN after CreateEnvironment via
#     evs:AssociateEipToVlan.
#
# Reference: EVS CreateEnvironment API documentation for public HCX requirements.
###############################################################################

# 1. IPAM with a public scope in this region
resource "aws_vpc_ipam" "public_hcx" {
  count       = var.enable_public_hcx ? 1 : 0
  description = "IPAM used to allocate a public /28 CIDR for the HCX VLAN"

  operating_regions {
    region_name = var.region
  }

  tags = merge(local.common_tags, { Name = "EVS-PublicHcx-IPAM" })
}

# 2. Public IPAM pool sourced from Amazon-provided public IPs, /28 default
resource "aws_vpc_ipam_pool" "public_hcx" {
  count                             = var.enable_public_hcx ? 1 : 0
  address_family                    = "ipv4"
  ipam_scope_id                     = aws_vpc_ipam.public_hcx[0].public_default_scope_id
  locale                            = var.region
  public_ip_source                  = "amazon"
  aws_service                       = "ec2"
  allocation_default_netmask_length = 28
  description                       = "Public /28 pool for EVS HCX VLAN"

  tags = merge(local.common_tags, { Name = "EVS-PublicHcx-Pool" })
}

# 3. Provision a /28 from the pool. cidr is exported once provisioning completes.
resource "aws_vpc_ipam_pool_cidr" "public_hcx" {
  count          = var.enable_public_hcx ? 1 : 0
  ipam_pool_id   = aws_vpc_ipam_pool.public_hcx[0].id
  netmask_length = 28
}

# 4. Attach the /28 as a secondary CIDR on the underlay VPC. This is what
#    lets EVS validate that the HCX VLAN CIDR is contained in the VPC.
resource "aws_vpc_ipv4_cidr_block_association" "public_hcx" {
  count      = var.enable_public_hcx ? 1 : 0
  vpc_id     = aws_vpc.underlay.id
  cidr_block = aws_vpc_ipam_pool_cidr.public_hcx[0].cidr
}

# 5. Allocate an EIP from the same pool. AWS reserves the first 2 and last
#    addresses in the /28; pick the 3rd usable host (index 2) so the EIP
#    sits inside the VLAN CIDR.
resource "aws_eip" "public_hcx" {
  count        = var.enable_public_hcx ? 1 : 0
  domain       = "vpc"
  ipam_pool_id = aws_vpc_ipam_pool.public_hcx[0].id
  address      = cidrhost(aws_vpc_ipam_pool_cidr.public_hcx[0].cidr, 2)

  depends_on = [aws_vpc_ipv4_cidr_block_association.public_hcx]

  tags = merge(local.common_tags, { Name = "EVS-PublicHcx-EIP" })
}

# 6. Network ACL for the HCX VLAN. No explicit rules — inherits the
#    default deny-all. Users must ensure that they have appropriate
#    network access control lists configured to restrict access
#    as needed for their security requirements.
resource "aws_network_acl" "public_hcx" {
  count  = var.enable_public_hcx ? 1 : 0
  vpc_id = aws_vpc.underlay.id

  tags = merge(local.common_tags, { Name = "HcxNacl" })
}
