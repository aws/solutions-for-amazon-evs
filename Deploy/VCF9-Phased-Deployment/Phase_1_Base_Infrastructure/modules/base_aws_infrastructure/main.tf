# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

###############################################################################
# Local values – CIDR octets for reverse DNS zone name
###############################################################################

locals {
  cidr_octets  = split(".", var.cidr_prefix)
  cidr_octet_0 = local.cidr_octets[0]
  cidr_octet_1 = local.cidr_octets[1]

  common_tags = {
    Environment = var.environment
    ManagedBy   = "terraform"
    Project     = "evs-vcf9-automation"
  }

  # AWSCC resources expect tags as a list of {key, value} objects rather than
  # a map. Derive from common_tags so the two representations cannot drift.
  common_tags_list = [for k, v in local.common_tags : { key = k, value = v }]

  forward_records = {
    esxi01            = { hostname = var.esxi01_name, ip_suffix = "10.11" }
    esxi02            = { hostname = var.esxi02_name, ip_suffix = "10.12" }
    esxi03            = { hostname = var.esxi03_name, ip_suffix = "10.13" }
    vcenter           = { hostname = var.vc_name, ip_suffix = "60.10" }
    nsx_mgr           = { hostname = var.nsx_name, ip_suffix = "60.11" }
    sddcm             = { hostname = var.sddcm_name, ip_suffix = "60.12" }
    cb                = { hostname = var.cb_name, ip_suffix = "60.13" }
    edge01            = { hostname = var.edge01_name, ip_suffix = "60.14" }
    edge02            = { hostname = var.edge02_name, ip_suffix = "60.15" }
    nsx01             = { hostname = var.nsx01_name, ip_suffix = "60.16" }
    nsx02             = { hostname = var.nsx02_name, ip_suffix = "60.17" }
    nsx03             = { hostname = var.nsx03_name, ip_suffix = "60.18" }
    vcf_ops           = { hostname = var.vcf_ops_name, ip_suffix = "60.19" }
    vcf_ops_01        = { hostname = var.vcf_ops_01_name, ip_suffix = "60.20" }
    vcf_ops_02        = { hostname = var.vcf_ops_02_name, ip_suffix = "60.21" }
    vcf_ops_03        = { hostname = var.vcf_ops_03_name, ip_suffix = "60.22" }
    vcf_ops_collector = { hostname = var.vcf_ops_collector_name, ip_suffix = "60.23" }
    vcf_fleet         = { hostname = var.vcf_fleet_name, ip_suffix = "60.24" }
    vsp_platform      = { hostname = var.vsp_platform_name, ip_suffix = "60.25" }
    vsp_instance      = { hostname = var.vsp_instance_name, ip_suffix = "60.26" }
    vsp_fleet         = { hostname = var.vsp_fleet_name, ip_suffix = "60.27" }
    vidb              = { hostname = var.vidb_name, ip_suffix = "60.28" }
    vcf_license       = { hostname = var.vcf_license_name, ip_suffix = "60.29" }
    vcf_logs          = { hostname = var.vcf_logs_name, ip_suffix = "60.30" }
    vcf_auto_platform = { hostname = var.vcf_auto_platform_name, ip_suffix = "60.31" }
    vcf_auto          = { hostname = var.vcf_auto_name, ip_suffix = "60.32" }
    vcf_sddcm01       = { hostname = var.vcf_sddcm01_name, ip_suffix = "60.33" }
    ops_mgr           = { hostname = var.ops_mgr_name, ip_suffix = "60.34" }
    ops_collector     = { hostname = var.ops_collector_name, ip_suffix = "60.35" }
    ops_mgr_replica   = { hostname = var.ops_mgr_replica_name, ip_suffix = "60.36" }
    ops_mgr_data01    = { hostname = var.ops_mgr_data01_name, ip_suffix = "60.37" }
  }

  reverse_records = {
    for k, v in local.forward_records : k => {
      hostname   = v.hostname
      ptr_prefix = join(".", reverse(split(".", v.ip_suffix)))
    }
  }
}

###############################################################################
# VPC & Networking
###############################################################################

resource "aws_vpc" "underlay" {

  cidr_block           = "${var.cidr_prefix}0.0/16"
  enable_dns_hostnames = true
  enable_dns_support   = true
  instance_tenancy     = "default"

  tags = merge(local.common_tags, { Name = "EVS-VPC" })
}

resource "aws_vpc_dhcp_options" "evs" {
  domain_name         = var.fqdn
  domain_name_servers = ["${var.cidr_prefix}0.100", "${var.cidr_prefix}0.101"]
  ntp_servers         = ["169.254.169.123"]

  tags = merge(local.common_tags, { Name = "EVS-DHCP-OpsSet" })
}

resource "aws_vpc_dhcp_options_association" "evs" {
  vpc_id          = aws_vpc.underlay.id
  dhcp_options_id = aws_vpc_dhcp_options.evs.id
}

# --- Service Access Subnet ---------------------------------------------------

resource "aws_subnet" "service_access" {
  vpc_id            = aws_vpc.underlay.id
  availability_zone = var.availability_zone
  cidr_block        = "${var.cidr_prefix}0.0/24"

  tags = merge(local.common_tags, { Name = "EVS-Service-Access-Subnet" })
}

resource "aws_route_table" "service_access" {
  vpc_id = aws_vpc.underlay.id
  tags   = merge(local.common_tags, { Name = "Service-Access-RTB" })
}

resource "aws_route_table_association" "service_access" {
  subnet_id      = aws_subnet.service_access.id
  route_table_id = aws_route_table.service_access.id
}

# --- Public Subnet ------------------------------------------------------------

resource "aws_subnet" "public" {
  vpc_id                  = aws_vpc.underlay.id
  availability_zone       = var.availability_zone
  cidr_block              = "${var.cidr_prefix}5.0/24"
  map_public_ip_on_launch = true

  tags = merge(local.common_tags, { Name = "Public-Access-Subnet" })
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.underlay.id
  tags   = merge(local.common_tags, { Name = "Public-Subnet-RTB" })
}

resource "aws_route_table_association" "public" {
  subnet_id      = aws_subnet.public.id
  route_table_id = aws_route_table.public.id
}

# --- Internet Gateway ---------------------------------------------------------

resource "aws_internet_gateway" "evs" {
  tags = merge(local.common_tags, { Name = "VPC-IGW" })
}

resource "aws_internet_gateway_attachment" "evs" {
  internet_gateway_id = aws_internet_gateway.evs.id
  vpc_id              = aws_vpc.underlay.id
}

resource "aws_route" "public_igw" {
  route_table_id         = aws_route_table.public.id
  destination_cidr_block = "0.0.0.0/0"
  gateway_id             = aws_internet_gateway.evs.id

  depends_on = [aws_internet_gateway_attachment.evs]
}

# --- NAT Gateway --------------------------------------------------------------

resource "aws_eip" "nat" {
  domain = "vpc"
}

resource "aws_nat_gateway" "evs" {
  allocation_id     = aws_eip.nat.id
  connectivity_type = "public"
  subnet_id         = aws_subnet.public.id

  tags = merge(local.common_tags, { Name = "VPC-NatGW" })
}

resource "aws_route" "private_nat" {
  route_table_id         = aws_route_table.service_access.id
  destination_cidr_block = "0.0.0.0/0"
  nat_gateway_id         = aws_nat_gateway.evs.id
}

###############################################################################
# Route 53 – Forward Zone & Records
###############################################################################

resource "aws_route53_zone" "forward" {
  name    = var.fqdn
  comment = "Forward lookup zone for EVS"

  vpc {
    vpc_id     = aws_vpc.underlay.id
    vpc_region = var.region
  }
}

resource "aws_route53_record" "forward" {
  for_each = local.forward_records

  zone_id = aws_route53_zone.forward.zone_id
  name    = "${each.value.hostname}.${var.fqdn}"
  type    = "A"
  ttl     = 300
  records = ["${var.cidr_prefix}${each.value.ip_suffix}"]
}

###############################################################################
# Route 53 – Reverse Zone & PTR Records
###############################################################################

resource "aws_route53_zone" "reverse" {
  name    = "${local.cidr_octet_1}.${local.cidr_octet_0}.in-addr.arpa"
  comment = "Reverse lookup zone for EVS"

  vpc {
    vpc_id     = aws_vpc.underlay.id
    vpc_region = var.region
  }
}

resource "aws_route53_record" "reverse" {
  for_each = local.reverse_records

  zone_id = aws_route53_zone.reverse.zone_id
  name    = "${each.value.ptr_prefix}.${local.cidr_octet_1}.${local.cidr_octet_0}.in-addr.arpa"
  type    = "PTR"
  ttl     = 300
  records = ["${each.value.hostname}.${var.fqdn}"]
}

###############################################################################
# EVS Security Group
#
# Used by the R53 inbound resolver endpoint and passed to EVS as
# serviceAccessSecurityGroups. Mirrors the permissive defaults of the VPC
# default SG (allow all intra-SG + all egress) without touching the default.
###############################################################################

resource "aws_security_group" "evs" {
  name        = "evs-service-access"
  description = "EVS service access security group (R53 resolver + EVS hosts)"
  vpc_id      = aws_vpc.underlay.id

  tags = merge(local.common_tags, { Name = "EVS-Service-Access-SG" })
}

resource "aws_vpc_security_group_ingress_rule" "evs_intra" {
  security_group_id            = aws_security_group.evs.id
  referenced_security_group_id = aws_security_group.evs.id
  ip_protocol                  = "-1"
  description                  = "All traffic from self"
}

resource "aws_vpc_security_group_ingress_rule" "evs_from_vpc" {
  security_group_id = aws_security_group.evs.id
  cidr_ipv4         = aws_vpc.underlay.cidr_block
  ip_protocol       = "-1"
  description       = "All traffic from VPC CIDR"
}

resource "aws_vpc_security_group_egress_rule" "evs_all" {
  security_group_id = aws_security_group.evs.id
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "-1"
  description       = "All egress"
}

###############################################################################
# Route 53 Inbound Resolver
###############################################################################

resource "aws_route53_resolver_endpoint" "inbound" {
  name               = "R53InboundRslvr"
  direction          = "INBOUND"
  security_group_ids = [aws_security_group.evs.id]

  ip_address {
    subnet_id = aws_subnet.service_access.id
    ip        = "${var.cidr_prefix}0.100"
  }

  ip_address {
    subnet_id = aws_subnet.service_access.id
    ip        = "${var.cidr_prefix}0.101"
  }

  depends_on = [aws_subnet.service_access]
}

###############################################################################
# Transit Gateway (conditional)
###############################################################################

resource "aws_ec2_transit_gateway" "evs" {
  count = var.create_tgw ? 1 : 0

  description                     = "TGW for EVS"
  auto_accept_shared_attachments  = "disable"
  default_route_table_association = "enable"

  tags = merge(local.common_tags, { Name = "EVS-TGW" })
}

resource "aws_ec2_transit_gateway_vpc_attachment" "evs" {
  count = var.create_tgw ? 1 : 0

  transit_gateway_id = aws_ec2_transit_gateway.evs[0].id
  vpc_id             = aws_vpc.underlay.id
  subnet_ids         = [aws_subnet.service_access.id]

  appliance_mode_support = "disable"
  dns_support            = "disable"
  ipv6_support           = "disable"

  tags = merge(local.common_tags, { Name = "EVS-TGW-VPC-Attachment" })

  depends_on = [
    aws_ec2_transit_gateway.evs,
    aws_vpc.underlay,
    aws_subnet.service_access,
  ]
}

###############################################################################
# EC2 Key Pair (server-side generated, private key stored in SSM automatically)
###############################################################################

resource "awscc_ec2_key_pair" "evs" {
  key_name   = "EVS-WS-KeyPair"
  key_type   = "rsa"
  key_format = "pem"

  tags = concat(local.common_tags_list, [{ key = "Name", value = "EVS-WS-KeyPair" }])
}
