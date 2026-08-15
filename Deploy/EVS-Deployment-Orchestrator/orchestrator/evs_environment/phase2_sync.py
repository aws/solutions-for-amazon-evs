# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Sync EVS host + VLAN info into the Phase 2 Terraform variables file."""

import json
import logging
from pathlib import Path
from typing import Any

from aws_client import AWSClient
from evs_manager import EVSManager

logger = logging.getLogger(__name__)


class Phase2Sync:
    """Populates Phase 2 Terraform variables from EVS.

    Two buckets of data:

      1. A target EC2 host (for the EBS volume attach). Found by looking up
         the first EC2 instance tagged 'DoNotDelete-EVS-[<env_id>]*'.
      2. The list of VLAN subnet IDs EVS created for the environment
         (for associating them with the service access route table),
         plus the route table ID itself read from config.

    Args:
        aws_client: Initialized AWSClient.
        evs: Initialized EVSManager
        environment_id: EVS environment ID.
        output_path: Path to the Phase 2 state file (JSON cache of
            host instance ID, AZ, route table ID, and VLAN subnet IDs).
        config: Phase 2 config dict (provides serviceAccessRouteTableId).
    """

    def __init__(
        self,
        aws_client: AWSClient,
        evs: EVSManager,
        environment_id: str,
        output_path: str | Path,
        config: dict[str, Any],
    ) -> None:
        self._aws = aws_client
        self._evs = evs
        self._environment_id = environment_id
        self._output_path = Path(output_path)
        self._config = config

    def _find_evs_host_instance(self) -> dict[str, str]:
        """Find the EC2 host backing this EVS environment.

        The Name-tag pattern can match every ESXi host in the cluster, so we
        gather ALL matches (paginated) and pick deterministically (lowest
        instance id) rather than an arbitrary API-order first hit. Every host in
        an EVS cluster shares one AZ, so the derived region/AZ is identical
        regardless of which host is chosen -- the deterministic pick just makes
        the selection stable and observable instead of order-dependent.
        """
        name_pattern = f"DoNotDelete-EVS-[{self._environment_id}]*"
        logger.info("Searching for EC2 instances with Name tag: %s", name_pattern)

        ec2 = self._aws.client("ec2")
        matches: list[dict[str, str]] = []
        paginator = ec2.get_paginator("describe_instances")
        for page in paginator.paginate(
            Filters=[
                {"Name": "tag:Name", "Values": [name_pattern]},
                {
                    "Name": "instance-state-name",
                    "Values": ["pending", "running", "stopping", "stopped"],
                },
            ]
        ):
            for reservation in page.get("Reservations", []):
                for instance in reservation.get("Instances", []):
                    az = instance["Placement"]["AvailabilityZone"]
                    matches.append({
                        "instance_id": instance["InstanceId"],
                        "instance_type": instance.get("InstanceType", ""),
                        "region": az[:-1],
                        "availability_zone": az,
                    })

        if not matches:
            raise RuntimeError(
                f"No EC2 instances found matching tag:Name={name_pattern}. "
                f"Verify the EVS environment {self._environment_id} is deployed."
            )

        matches.sort(key=lambda m: m["instance_id"])
        chosen = matches[0]
        if len(matches) > 1:
            logger.info(
                "%d EVS hosts match %s; selecting %s deterministically "
                "(all share AZ %s)",
                len(matches), name_pattern, chosen["instance_id"],
                chosen["availability_zone"],
            )
        logger.info(
            "Found EVS host: instance_id=%s, instance_type=%s, az=%s, region=%s",
            chosen["instance_id"], chosen["instance_type"],
            chosen["availability_zone"], chosen["region"],
        )
        return chosen

    def _collect_vlan_subnet_ids(self) -> list[str]:
        """Return the subnet IDs of every CREATED VLAN in the environment."""
        vlans = self._evs.list_environment_vlans(self._environment_id)

        subnet_ids: list[str] = []
        for vlan in vlans:
            state = vlan.get("vlanState")
            subnet_id = vlan.get("subnetId")
            function_name = vlan.get("functionName", "unknown")

            if not subnet_id:
                logger.warning(
                    "VLAN %s has no subnetId (state=%s); skipping",
                    function_name,
                    state,
                )
                continue
            if state != "CREATED":
                logger.warning(
                    "VLAN %s subnet %s is in state %s (not CREATED); skipping",
                    function_name,
                    subnet_id,
                    state,
                )
                continue

            subnet_ids.append(subnet_id)
            logger.info("VLAN %s -> %s", function_name, subnet_id)

        return subnet_ids

    def _verify_esxi_secrets(self) -> list[str]:
        """Verify the per-host ESXi root password secrets exist in AWS.

        EVS stores each host's root password as a secret named
        ``evs!<environment_id>_<short_host>``; the bang is part of the literal
        AWS name, so pass it through as-is. We only ``DescribeSecret`` here —
        values stay in AWS until Phase 3's resolver pulls them via the
        ``__SECRET:esxi:<host>__`` placeholders at POST time.

        Returns the list of host names whose secrets are missing. Empty
        list = all good.
        """
        from botocore.exceptions import ClientError

        esxi_hostnames = self._config.get("esxiHostnames", []) or []
        if not esxi_hostnames:
            logger.warning(
                "config.esxiHostnames is empty; can't verify ESXi secrets"
            )
            return []

        sm = self._aws.client("secretsmanager")
        missing: list[str] = []
        for short in esxi_hostnames:
            secret_id = f"evs!{self._environment_id}_{short}"
            try:
                sm.describe_secret(SecretId=secret_id)
                logger.info("Verified ESXi secret for %s: %s", short, secret_id)
            except ClientError as e:
                code = e.response.get("Error", {}).get("Code", "?")
                if code != "ResourceNotFoundException":
                    # AccessDenied, throttling, or any other API error is NOT
                    # "the secret doesn't exist yet" — misreporting it as
                    # missing sends the operator chasing the wrong problem.
                    # Only ResourceNotFoundException means genuinely absent.
                    raise
                logger.warning(
                    "ESXi secret %s not present (%s); Phase 3 bringup "
                    "will fail when it tries to resolve "
                    "__SECRET:esxi:%s__",
                    secret_id, code, short,
                )
                missing.append(short)

        return missing

    def _find_hcx_vlan_subnet_id(self) -> str | None:
        """Return the subnet ID of the 'hcx' VLAN, or None if absent.

        Identified by functionName rather than position, since VLAN ordering is
        not guaranteed. Only the public HCX VLAN needs this; the caller decides
        whether to use it based on hcxPublic.
        """
        for vlan in self._evs.list_environment_vlans(self._environment_id):
            if (vlan.get("functionName") or "").lower() != "hcx":
                continue
            if vlan.get("vlanState") != "CREATED":
                logger.warning(
                    "hcx VLAN subnet %s is in state %s (not CREATED)",
                    vlan.get("subnetId"), vlan.get("vlanState"),
                )
                return None
            return vlan.get("subnetId")
        return None

    def sync(self, dry_run: bool = False) -> dict[str, Any]:
        """Look up the host + VLAN subnets and write the Phase 2 tfvars file."""
        host_info = self._find_evs_host_instance()
        vlan_subnet_ids = self._collect_vlan_subnet_ids()
        missing_esxi = self._verify_esxi_secrets()

        route_table_id = self._config.get("serviceAccessRouteTableId", "")
        if not route_table_id and vlan_subnet_ids:
            raise ValueError(
                "serviceAccessRouteTableId is missing from config but "
                f"{len(vlan_subnet_ids)} VLAN subnet(s) were collected — "
                "cannot wire VLAN subnets to an empty route table ID. "
                "Set serviceAccessRouteTableId in the Phase 2 config."
            )
        if not route_table_id:
            logger.warning(
                "serviceAccessRouteTableId missing from config — "
                "route-table associations will not be wired in tfvars"
            )

        tfvars: dict[str, Any] = {
            "region": host_info["region"],
            "availability_zone": host_info["availability_zone"],
            "target_instance_id": host_info["instance_id"],
            "service_access_route_table_id": route_table_id,
            "evs_vlan_subnet_ids": vlan_subnet_ids,
        }

        # The public HCX VLAN must be associated with an IGW-routed route table,
        # not the NAT-routed service-access table, so it needs to be identified
        # separately from the rest. Only relevant when HCX public connectivity
        # is enabled; a private HCX VLAN belongs on the service-access table
        # like every other VLAN.
        if self._config.get("hcxPublic"):
            hcx_subnet_id = self._find_hcx_vlan_subnet_id()
            if hcx_subnet_id:
                tfvars["hcx_public_vlan_subnet_id"] = hcx_subnet_id
                tfvars["public_subnet_id"] = self._config.get("publicSubnetId", "")
            else:
                logger.warning(
                    "hcxPublic is set but no 'hcx' VLAN subnet was found; the "
                    "HCX VLAN cannot be wired to a public route table"
                )

        if dry_run:
            logger.info("DRY RUN — would write to %s:", self._output_path)
            print(json.dumps(tfvars, indent=2))
            return {
                **tfvars,
                "instance_type": host_info["instance_type"],
                "missing_esxi_secrets": missing_esxi,
            }

        self._output_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._output_path, "w") as f:
            json.dump(tfvars, f, indent=2)
            f.write("\n")
        logger.info("Wrote Phase 2 variables to: %s", self._output_path)

        # Return more than the tfvars: main.py uses host_info fields (e.g.
        # instance_type) for the Phase 3 sync, and ``missing_esxi_secrets``
        # lets the caller fail fast before Phase 3 bringup blows up at POST.
        return {
            **tfvars,
            "instance_type": host_info["instance_type"],
            "missing_esxi_secrets": missing_esxi,
        }
