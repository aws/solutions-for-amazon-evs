# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

###############################################################################
# VPC Route Server (AWSCC provider – no aws provider equivalent yet)
###############################################################################

resource "awscc_ec2_route_server" "evs" {
  amazon_side_asn         = 65022
  persist_routes          = "enable"
  persist_routes_duration = 5

  tags = concat(local.common_tags_list, [{ key = "Name", value = "EVS-Route-Server" }])
}

resource "awscc_ec2_route_server_association" "evs" {
  route_server_id = awscc_ec2_route_server.evs.route_server_id
  vpc_id          = aws_vpc.underlay.id
}

resource "awscc_ec2_route_server_endpoint" "ep01" {
  route_server_id = awscc_ec2_route_server.evs.route_server_id
  subnet_id       = aws_subnet.service_access.id

  tags = concat(local.common_tags_list, [{ key = "Name", value = "EVS-RouteServer-Endpoint01" }])

  depends_on = [awscc_ec2_route_server_association.evs]
}

resource "awscc_ec2_route_server_endpoint" "ep02" {
  route_server_id = awscc_ec2_route_server.evs.route_server_id
  subnet_id       = aws_subnet.service_access.id

  tags = concat(local.common_tags_list, [{ key = "Name", value = "EVS-RouteServer-Endpoint02" }])

  depends_on = [awscc_ec2_route_server_association.evs]
}

resource "awscc_ec2_route_server_peer" "peer01" {
  bgp_options = {
    peer_asn                = 65000
    peer_liveness_detection = "bgp-keepalive"
  }
  peer_address             = "${var.cidr_prefix}80.10"
  route_server_endpoint_id = awscc_ec2_route_server_endpoint.ep01.route_server_endpoint_id

  tags = concat(local.common_tags_list, [{ key = "Name", value = "EVS-RouteServer-Peer01" }])
}

resource "awscc_ec2_route_server_peer" "peer02" {
  bgp_options = {
    peer_asn                = 65000
    peer_liveness_detection = "bgp-keepalive"
  }
  peer_address             = "${var.cidr_prefix}80.11"
  route_server_endpoint_id = awscc_ec2_route_server_endpoint.ep02.route_server_endpoint_id

  tags = concat(local.common_tags_list, [{ key = "Name", value = "EVS-RouteServer-Peer02" }])
}

resource "awscc_ec2_route_server_propagation" "service_access" {
  route_server_id = awscc_ec2_route_server.evs.route_server_id
  route_table_id  = aws_route_table.service_access.id

  depends_on = [awscc_ec2_route_server_association.evs]
}

resource "awscc_ec2_route_server_propagation" "public" {
  route_server_id = awscc_ec2_route_server.evs.route_server_id
  route_table_id  = aws_route_table.public.id

  depends_on = [awscc_ec2_route_server_association.evs]
}
