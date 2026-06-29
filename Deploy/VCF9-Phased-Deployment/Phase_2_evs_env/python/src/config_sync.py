# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Sync Phase 1 Terraform outputs into the Phase 2 config file."""

import json
import logging
import re
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class ConfigSync:
    """Reads Terraform state and updates the Phase 2 config with infrastructure values.

    Extracts VPC ID, service access subnet ID, and VPC CIDR from the Terraform
    state file, then updates the config's VPC ID, subnet ID, and rewrites the
    first two octets of every VLAN CIDR to match the VPC's CIDR prefix.

    Args:
        tfstate_path: Path to the Phase 1 terraform.tfstate file.
        config_path: Path to the Phase 2 config.json file.
    """

    def __init__(self, tfstate_path: str | Path, config_path: str | Path) -> None:
        self._tfstate_path = Path(tfstate_path)
        self._config_path = Path(config_path)

    def _load_json(self, path: Path) -> dict[str, Any]:
        """Load and return a JSON file."""
        if not path.exists():
            raise FileNotFoundError(f"File not found: {path}")
        with open(path) as f:
            return json.load(f)

    def _save_json(self, path: Path, data: dict[str, Any]) -> None:
        """Write data to a JSON file with pretty formatting."""
        with open(path, "w") as f:
            json.dump(data, f, indent=2)
            f.write("\n")
        logger.info("Updated config written to: %s", path)

    def _extract_from_state(self, state: dict[str, Any]) -> dict[str, Any]:
        """Extract VPC ID, subnet ID, VPC CIDR, region, and assorted other
        Phase 1 outputs from a Terraform state file.

        Checks the outputs section first, then falls back to walking
        resources for the four required values (VPC, subnet, CIDR, region).
        Optional values (hostnames, route server IPs, HCX public CIDR) are
        only sourced from outputs.

        Returns:
            Dict with keys present from the state. Required keys are
            guaranteed; optional keys may or may not appear. Values can be
            strings, dicts, lists, bools, or None depending on the output.
        """
        results: dict[str, Any] = {}

        # Try outputs section first
        outputs = state.get("outputs", {})
        if "region" in outputs:
            results["region"] = outputs["region"]["value"]
        if "fqdn" in outputs:
            results["fqdn"] = outputs["fqdn"]["value"]
        if "vcf_hostnames" in outputs:
            results["vcf_hostnames"] = outputs["vcf_hostnames"]["value"]
        if "esxi_hostnames" in outputs:
            results["esxi_hostnames"] = outputs["esxi_hostnames"]["value"]
        if "vpc_id" in outputs:
            results["vpc_id"] = outputs["vpc_id"]["value"]
        if "service_access_subnet_id" in outputs:
            results["service_access_subnet_id"] = outputs["service_access_subnet_id"]["value"]
        if "service_access_route_table_id" in outputs:
            results["service_access_route_table_id"] = outputs["service_access_route_table_id"]["value"]
        if "vpc_default_security_group_id" in outputs:
            results["vpc_default_security_group_id"] = outputs["vpc_default_security_group_id"]["value"]
        if "evs_security_group_id" in outputs:
            results["evs_security_group_id"] = outputs["evs_security_group_id"]["value"]
        if "route_server_endpoint01_ip" in outputs:
            results["route_server_endpoint01_ip"] = outputs["route_server_endpoint01_ip"]["value"]
        if "route_server_endpoint02_ip" in outputs:
            results["route_server_endpoint02_ip"] = outputs["route_server_endpoint02_ip"]["value"]
        if "hcx_public" in outputs:
            results["hcx_public"] = outputs["hcx_public"]["value"]
        if "hcx_public_cidr" in outputs:
            # Will be None when hcx_public = false; consumers must check both.
            results["hcx_public_cidr"] = outputs["hcx_public_cidr"]["value"]
        if "hcx_network_acl_id" in outputs:
            results["hcx_network_acl_id"] = outputs["hcx_network_acl_id"]["value"]
        if "hcx_eip_allocation_id" in outputs:
            results["hcx_eip_allocation_id"] = outputs["hcx_eip_allocation_id"]["value"]

        # Walk resources for values not found in outputs
        resources = state.get("resources", [])
        for resource in resources:
            res_type = resource.get("type", "")
            res_name = resource.get("name", "")
            instances = resource.get("instances", [])

            if not instances:
                continue

            attrs = instances[0].get("attributes", {})

            if res_type == "aws_vpc" and res_name == "underlay":
                if "vpc_id" not in results:
                    results["vpc_id"] = attrs.get("id", "")
                if "vpc_cidr_block" not in results:
                    results["vpc_cidr_block"] = attrs.get("cidr_block", "")
                if "vpc_default_security_group_id" not in results:
                    results["vpc_default_security_group_id"] = attrs.get(
                        "default_security_group_id", ""
                    )
                # Extract region from VPC ARN as fallback
                if "region" not in results:
                    arn = attrs.get("arn", "")
                    arn_parts = arn.split(":")
                    if len(arn_parts) >= 4 and arn_parts[3]:
                        results["region"] = arn_parts[3]

            elif res_type == "aws_subnet" and res_name == "service_access":
                if "service_access_subnet_id" not in results:
                    results["service_access_subnet_id"] = attrs.get("id", "")

        missing = {"vpc_id", "service_access_subnet_id", "vpc_cidr_block", "region"} - results.keys()
        if missing:
            friendly_names = {
                "vpc_id": "VPC ID",
                "service_access_subnet_id": "Service Access Subnet ID",
                "vpc_cidr_block": "VPC CIDR Block",
                "region": "Region",
            }
            missing_labels = [friendly_names.get(k, k) for k in missing]
            raise ValueError(
                f"The following values are missing from the Terraform state: "
                f"{', '.join(missing_labels)}. "
                f"The Phase 1 Terraform may not have been applied yet."
            )

        # Check for empty values
        required_fields = {
            "vpc_id": "VPC ID",
            "service_access_subnet_id": "Service Access Subnet ID",
            "vpc_cidr_block": "VPC CIDR Block",
        }
        empty_fields = [
            label for key, label in required_fields.items()
            if not results.get(key)
        ]
        if empty_fields:
            raise ValueError(
                f"The following values are empty in the Terraform state: "
                f"{', '.join(empty_fields)}. "
                f"The Phase 1 Terraform may not have been applied yet."
            )

        return results

    @staticmethod
    def _extract_cidr_prefix(vpc_cidr: str) -> str:
        """Extract the first two octets from a VPC CIDR block.

        Args:
            vpc_cidr: A CIDR string like '10.0.0.0/16'.

        Returns:
            The first two octets with a trailing dot, e.g. '10.0.'.
        """
        match = re.match(r"^(\d+\.\d+)\.", vpc_cidr)
        if not match:
            raise ValueError(f"Cannot parse CIDR prefix from: {vpc_cidr}")
        return match.group(1) + "."

    @staticmethod
    def _rewrite_vlan_cidrs(
        vlans: dict[str, Any], new_prefix: str
    ) -> dict[str, Any]:
        """Rewrite the first two octets of every VLAN CIDR.

        Args:
            vlans: The initialVlans dict from config.
            new_prefix: The new two-octet prefix (e.g. '10.0.').

        Returns:
            Updated vlans dict with rewritten CIDRs.
        """
        updated = {}
        for vlan_name, vlan_config in vlans.items():
            if isinstance(vlan_config, dict) and "cidr" in vlan_config:
                old_cidr = vlan_config["cidr"]
                # Replace first two octets: "X.Y.Z.W/mask" -> "A.B.Z.W/mask"
                new_cidr = re.sub(
                    r"^\d+\.\d+\.",
                    new_prefix,
                    old_cidr,
                )
                updated[vlan_name] = {"cidr": new_cidr}
                if old_cidr != new_cidr:
                    logger.info(
                        "VLAN %s CIDR: %s -> %s", vlan_name, old_cidr, new_cidr
                    )
            else:
                updated[vlan_name] = vlan_config
        return updated

    def sync(self, dry_run: bool = False) -> dict[str, Any]:
        """Read Terraform state and update the config file.

        Args:
            dry_run: If True, show changes without writing the file.

        Returns:
            The updated config dict.
        """
        logger.info("Reading Terraform state from: %s", self._tfstate_path)
        state = self._load_json(self._tfstate_path)
        tf_values = self._extract_from_state(state)

        logger.info("Reading config from: %s", self._config_path)
        config = self._load_json(self._config_path)

        # Update region
        config["region"] = tf_values["region"]
        logger.info("region -> %s", tf_values["region"])

        # Update FQDN (if available from Phase 1)
        if "fqdn" in tf_values:
            config["fqdn"] = tf_values["fqdn"]
            logger.info("fqdn -> %s", tf_values["fqdn"])

        # Update VCF hostnames (if available from Phase 1)
        if "vcf_hostnames" in tf_values:
            config["vcfHostnames"] = tf_values["vcf_hostnames"]
            logger.info("vcfHostnames -> %s", tf_values["vcf_hostnames"])

        # Update ESXi hostnames (if available from Phase 1)
        if "esxi_hostnames" in tf_values:
            config["esxiHostnames"] = tf_values["esxi_hostnames"]
            logger.info("esxiHostnames -> %s", tf_values["esxi_hostnames"])

        # Update VPC ID and subnet ID
        config["vpcId"] = tf_values["vpc_id"]
        logger.info("vpcId -> %s", tf_values["vpc_id"])

        config["serviceAccessSubnetId"] = tf_values["service_access_subnet_id"]
        logger.info("serviceAccessSubnetId -> %s", tf_values["service_access_subnet_id"])

        if "service_access_route_table_id" in tf_values:
            config["serviceAccessRouteTableId"] = tf_values["service_access_route_table_id"]
            logger.info(
                "serviceAccessRouteTableId -> %s",
                tf_values["service_access_route_table_id"],
            )

        # Set serviceAccessSecurityGroups authoritatively from Phase 1.
        # Prefer the dedicated EVS SG; fall back to the VPC default for older
        # Phase 1 state files that don't export it yet.
        evs_sg = tf_values.get("evs_security_group_id") or tf_values.get(
            "vpc_default_security_group_id"
        )
        if evs_sg:
            existing = config.get("serviceAccessSecurityGroups", {})
            if not isinstance(existing, dict):
                existing = {}
            existing["securityGroups"] = [evs_sg]
            config["serviceAccessSecurityGroups"] = existing
            logger.info("serviceAccessSecurityGroups -> %s", evs_sg)

        # Rewrite VLAN CIDRs
        vpc_cidr = tf_values["vpc_cidr_block"]
        new_prefix = self._extract_cidr_prefix(vpc_cidr)
        logger.info("VPC CIDR prefix: %s", new_prefix)

        # DNS servers are the inbound resolver endpoints at <prefix>0.100 and <prefix>0.101
        dns_servers = [f"{new_prefix}0.100", f"{new_prefix}0.101"]
        config["dnsServers"] = dns_servers
        logger.info("dnsServers -> %s", dns_servers)

        # Route Server endpoint IPs (BGP peer IPs for NSX edges)
        if "route_server_endpoint01_ip" in tf_values:
            config["routeServerEndpoint01Ip"] = tf_values["route_server_endpoint01_ip"]
            logger.info("routeServerEndpoint01Ip -> %s", tf_values["route_server_endpoint01_ip"])
        if "route_server_endpoint02_ip" in tf_values:
            config["routeServerEndpoint02Ip"] = tf_values["route_server_endpoint02_ip"]
            logger.info("routeServerEndpoint02Ip -> %s", tf_values["route_server_endpoint02_ip"])

        if "initialVlans" in config:
            config["initialVlans"] = self._rewrite_vlan_cidrs(
                config["initialVlans"], new_prefix
            )

        # HCX public-internet override. When Phase 1 was applied with
        # ``enable_public_hcx = true``, the POC's public_hcx.tf created an
        # IPAM, provisioned a /28, attached it as a secondary VPC CIDR,
        # allocated an EIP, and created a permissive NACL. Here we propagate
        # all three artifacts into config so CreateEnvironment gets the NACL
        # and post-create can associate the EIP.
        # See https://docs.aws.amazon.com/evs/latest/userguide/evs-env-hcx-internet-access.html
        if tf_values.get("hcx_public") and tf_values.get("hcx_public_cidr"):
            hcx_cidr = tf_values["hcx_public_cidr"]
            config.setdefault("initialVlans", {}).setdefault("hcx", {})
            config["initialVlans"]["hcx"]["cidr"] = hcx_cidr
            config["hcxPublic"] = True
            logger.info(
                "HCX public connectivity enabled — initialVlans.hcx.cidr -> %s",
                hcx_cidr,
            )

            # isHcxPublic and hcxNetworkAclId are top-level fields inside
            # the initialVlans structure (siblings of vmkManagement, hcx, etc.)
            config["initialVlans"]["isHcxPublic"] = True
            logger.info("initialVlans.isHcxPublic -> true")

            hcx_nacl_id = tf_values.get("hcx_network_acl_id")
            if hcx_nacl_id:
                config["initialVlans"]["hcxNetworkAclId"] = hcx_nacl_id
                logger.info(
                    "initialVlans.hcxNetworkAclId -> %s", hcx_nacl_id
                )

            # EIP allocation ID — stored in config for the post-create
            # AssociateEipToVlan step.
            hcx_eip_id = tf_values.get("hcx_eip_allocation_id")
            if hcx_eip_id:
                config["hcxEipAllocationId"] = hcx_eip_id
                logger.info("hcxEipAllocationId -> %s", hcx_eip_id)
        else:
            # Private HCX — remove any stale public HCX fields that may
            # have been left from a prior public-HCX run. The EVS API
            # rejects isHcxPublic/hcxNetworkAclId when HCX is private.
            config["hcxPublic"] = False
            config.get("initialVlans", {}).pop("isHcxPublic", None)
            config.get("initialVlans", {}).pop("hcxNetworkAclId", None)
            config.pop("hcxEipAllocationId", None)

        if dry_run:
            logger.info("DRY RUN — updated config would be:")
            print(json.dumps(config, indent=2))
        else:
            self._save_json(self._config_path, config)

        return config
