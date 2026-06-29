# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Sync EVS host + VLAN info into the Phase 2 Terraform variables file."""

import json
import logging
from pathlib import Path
from typing import Any

from src.aws_client import AWSClient
from src.evs_manager import EVSManager

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
        """Find the first EC2 instance matching the EVS naming pattern."""
        name_pattern = f"DoNotDelete-EVS-[{self._environment_id}]*"
        logger.info("Searching for EC2 instances with Name tag: %s", name_pattern)

        ec2 = self._aws.client("ec2")
        response = ec2.describe_instances(
            Filters=[
                {"Name": "tag:Name", "Values": [name_pattern]},
                {
                    "Name": "instance-state-name",
                    "Values": ["pending", "running", "stopping", "stopped"],
                },
            ]
        )

        for reservation in response.get("Reservations", []):
            for instance in reservation.get("Instances", []):
                instance_id = instance["InstanceId"]
                instance_type = instance.get("InstanceType", "")
                az = instance["Placement"]["AvailabilityZone"]
                region = az[:-1]

                logger.info(
                    "Found EVS host: instance_id=%s, instance_type=%s, az=%s, region=%s",
                    instance_id,
                    instance_type,
                    az,
                    region,
                )
                return {
                    "instance_id": instance_id,
                    "instance_type": instance_type,
                    "region": region,
                    "availability_zone": az,
                }

        raise RuntimeError(
            f"No EC2 instances found matching tag:Name={name_pattern}. "
            f"Verify the EVS environment {self._environment_id} is deployed."
        )

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
                    "VLAN %s subnet %s is in state %s (not CREATED); including anyway",
                    function_name,
                    subnet_id,
                    state,
                )

            subnet_ids.append(subnet_id)
            logger.info("VLAN %s -> %s", function_name, subnet_id)

        return subnet_ids

    def _verify_esxi_secrets(self) -> list[str]:
        """Verify the per-host ESXi root password secrets exist in AWS.

        EVS stores each host's root password as a separate secret named
        ``evs!<environment_id>_<short_host>`` (e.g.
        ``evs!env-abc123_esxi01``). The bang (``!``) is part of the
        literal name AWS uses; pass it through to Secrets Manager as-is.

        We only ``DescribeSecret`` here — values stay in AWS until
        Phase 3's runtime resolver pulls them at POST time. Phase 3
        substitutes them into the bringup spec via the
        ``__SECRET:esxi:<host>__`` placeholders that
        ``sddc_spec_builder._build_host_specs`` writes.

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
                logger.warning(
                    "ESXi secret %s not present (%s); Phase 3 bringup "
                    "will fail when it tries to resolve "
                    "__SECRET:esxi:%s__",
                    secret_id, code, short,
                )
                missing.append(short)

        return missing

    def sync(self, dry_run: bool = False) -> dict[str, Any]:
        """Look up the host + VLAN subnets and write the Phase 2 tfvars file."""
        host_info = self._find_evs_host_instance()
        vlan_subnet_ids = self._collect_vlan_subnet_ids()
        missing_esxi = self._verify_esxi_secrets()

        route_table_id = self._config.get("serviceAccessRouteTableId", "")
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

        # Return a dict with more than just the tfvars — main.py uses
        # host_info fields (like instance_type) to inform the Phase 3
        # sync. ``missing_esxi_secrets`` lets the caller fail fast if
        # any ESXi root secret is absent (Phase 3 bringup would
        # otherwise blow up at POST time).
        return {
            **tfvars,
            "instance_type": host_info["instance_type"],
            "missing_esxi_secrets": missing_esxi,
        }
