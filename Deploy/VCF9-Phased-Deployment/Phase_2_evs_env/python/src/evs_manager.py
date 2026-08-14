# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""EVS environment lifecycle management."""

import logging
from typing import Any

from src.aws_client import AWSClient

logger = logging.getLogger(__name__)

EVS_SERVICE_NAME = "evs"


class EVSManager:
    """Manages EVS environment creation and lifecycle operations.

    Args:
        aws_client: An initialized AWSClient instance.
    """

    def __init__(self, aws_client: AWSClient) -> None:
        self._aws = aws_client
        self._evs = aws_client.client(EVS_SERVICE_NAME)

    def create_environment(
        self,
        vpc_id: str,
        service_access_subnet_id: str,
        vcf_version: str,
        initial_vlans: dict[str, Any],
        terms_accepted: bool = True,
        environment_name: str | None = None,
        connectivity_info: dict[str, Any] | None = None,
        license_info: list[dict[str, str]] | None = None,
        hosts: list[dict[str, str]] | None = None,
        vcf_hostnames: dict[str, str] | None = None,
        site_id: str | None = None,
        service_access_security_groups: dict[str, Any] | None = None,
        tags: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Create an EVS environment.

        Args:
            vpc_id: VPC ID for the environment.
            service_access_subnet_id: Subnet ID for service access.
            vcf_version: VCF version to deploy (e.g. 'VCF-5.2.2').
            initial_vlans: VLAN configuration for the environment.
            terms_accepted: Whether EVS terms are accepted.
            environment_name: Optional name for the environment.
            connectivity_info: Route server peering configuration.
            license_info: Broadcom license keys.
            hosts: List of host configurations.
            vcf_hostnames: VCF component hostname mappings.
            site_id: Broadcom site ID.
            service_access_security_groups: Security groups for service access.
            tags: Resource tags.

        Returns:
            The API response containing the created environment details.
        """
        params: dict[str, Any] = {
            "vpcId": vpc_id,
            "serviceAccessSubnetId": service_access_subnet_id,
            "vcfVersion": vcf_version,
            "termsAccepted": terms_accepted,
            "initialVlans": initial_vlans,
        }

        if environment_name is not None:
            params["environmentName"] = environment_name
        if connectivity_info is not None:
            params["connectivityInfo"] = connectivity_info
        if license_info is not None:
            params["licenseInfo"] = license_info
        if hosts is not None:
            params["hosts"] = hosts
        if vcf_hostnames is not None:
            params["vcfHostnames"] = vcf_hostnames
        if site_id is not None:
            params["siteId"] = site_id
        if service_access_security_groups is not None:
            params["serviceAccessSecurityGroups"] = service_access_security_groups
        if tags is not None:
            params["tags"] = tags

        logger.info("Creating EVS environment in VPC %s...", vpc_id)
        logger.debug("CreateEnvironment params: %s", params)

        response = self._evs.create_environment(**params)

        environment = response.get("environment", {})
        env_id = environment.get("environmentId", "unknown")
        state = environment.get("environmentState", "unknown")

        logger.info(
            "EVS environment creation initiated: id=%s, state=%s",
            env_id,
            state,
        )

        return response

    def get_latest_esx_version(self, target_version: str, instance_type: str) -> str:
        """Resolve the latest ESXi version from the EVS get-versions API.

        Filters by instance_type, then picks the latest version whose
        major.minor matches target_version.
        """
        response = self._evs.get_versions()

        esx_versions: list[str] = []
        for entry in response.get("instanceTypeEsxVersions", []):
            if entry.get("instanceType") == instance_type:
                esx_versions = entry.get("esxVersions", [])
                break

        if not esx_versions:
            raise RuntimeError(
                f"No ESXi versions found for instance type '{instance_type}' "
                f"via get-versions API."
            )

        parts = target_version.split(".")
        prefix = f"ESXi-{parts[0]}.{parts[1]}." if len(parts) >= 2 else f"ESXi-{target_version}"

        matching = [v for v in esx_versions if v.startswith(prefix)]
        if not matching:
            raise RuntimeError(
                f"No ESXi version matching '{prefix}*' for {instance_type}. "
                f"Available: {esx_versions}. "
                f"Set 'esxVersion' in config.json to override."
            )

        matching.sort(key=lambda v: int(v.rsplit(".", 1)[-1]), reverse=True)
        latest = matching[0]
        logger.info(
            "Resolved ESXi version from get-versions API: %s "
            "(target=%s, instance_type=%s, %d candidate(s))",
            latest, target_version, instance_type, len(matching),
        )
        return latest

    def get_environment(self, environment_id: str) -> dict[str, Any]:
        """Get the current state of an EVS environment.

        Args:
            environment_id: The ID of the EVS environment.

        Returns:
            The API response containing the environment details.
        """
        logger.info("Getting environment status: %s", environment_id)
        return self._evs.get_environment(environmentId=environment_id)

    def list_environment_vlans(self, environment_id: str) -> list[dict[str, Any]]:
        """List all VLANs (and their subnet IDs) in an EVS environment.

        Follows pagination via nextToken to return the full set.

        Args:
            environment_id: The ID of the EVS environment.

        Returns:
            A list of Vlan structures. Each has keys like vlanId, cidr,
            availabilityZone, functionName, subnetId, vlanState.
        """
        vlans: list[dict[str, Any]] = []
        next_token: str | None = None

        while True:
            params: dict[str, Any] = {"environmentId": environment_id}
            if next_token:
                params["nextToken"] = next_token

            response = self._evs.list_environment_vlans(**params)
            vlans.extend(response.get("environmentVlans", []))

            next_token = response.get("nextToken")
            if not next_token:
                break

        logger.info(
            "Listed %d VLAN(s) for environment %s",
            len(vlans),
            environment_id,
        )
        return vlans

    def list_environment_hosts(self, environment_id: str) -> list[dict[str, Any]]:
        """List all hosts in an EVS environment.

        Follows pagination via nextToken to return the full set.

        Args:
            environment_id: The ID of the EVS environment.

        Returns:
            A list of Host structures. Each has keys like hostName,
            hostState, instanceType, instanceId.
        """
        hosts: list[dict[str, Any]] = []
        next_token: str | None = None

        while True:
            params: dict[str, Any] = {"environmentId": environment_id}
            if next_token:
                params["nextToken"] = next_token

            response = self._evs.list_environment_hosts(**params)
            hosts.extend(response.get("environmentHosts", []))

            next_token = response.get("nextToken")
            if not next_token:
                break

        return hosts

    def wait_for_hosts_ready(
        self,
        environment_id: str,
        timeout_seconds: int = 5400,
        poll_interval_seconds: int = 60,
    ) -> list[dict[str, Any]]:
        """Poll list_environment_hosts until every host reaches CREATED.

        Used by the all-in-one ``deploy-environment`` action so the chain
        can run create-hosts -> post-evs-sync-config without manual waiting
        in between. Host realization (EC2 metal provisioning + EVS
        configuration + ESXi boot) typically takes 20-40 minutes per host;
        the 90-minute default budget covers that with headroom.

        Treats ``FAILED`` and ``CREATE_FAILED`` as fatal and raises
        immediately. ``CREATING`` (and any unknown state) keeps polling.

        Args:
            environment_id: The EVS environment to poll.
            timeout_seconds: Total budget. Default 90 minutes.
            poll_interval_seconds: Time between polls (default 60s).

        Returns:
            The final list of host dicts once all are ``CREATED``.

        Raises:
            RuntimeError: If any host lands in a fatal state.
            TimeoutError: If not all hosts reach CREATED in time.
        """
        import time

        terminal_failures = {"FAILED", "CREATE_FAILED"}
        deadline = time.time() + timeout_seconds
        last_summary: tuple[tuple[str, str], ...] | None = None

        while time.time() < deadline:
            hosts = self.list_environment_hosts(environment_id)
            if not hosts:
                logger.info(
                    "No hosts visible yet for environment %s; "
                    "retrying in %ds",
                    environment_id, poll_interval_seconds,
                )
                time.sleep(poll_interval_seconds)
                continue

            # Check for any fatal state first.
            for host in hosts:
                state = (host.get("hostState") or "").upper()
                if state in terminal_failures:
                    raise RuntimeError(
                        f"Host '{host.get('hostName', '?')}' in environment "
                        f"{environment_id} ended in fatal state '{state}'. "
                        f"Open an AWS support case for assistance."
                    )

            states = tuple(
                sorted(
                    (h.get("hostName", "?"), (h.get("hostState") or "").upper())
                    for h in hosts
                )
            )
            if states != last_summary:
                # Log only on transitions so a 40-minute wait doesn't
                # flood the log with identical CREATING lines.
                logger.info(
                    "Environment %s host states: %s",
                    environment_id,
                    ", ".join(f"{name}={state}" for name, state in states),
                )
                last_summary = states

            if all(state == "CREATED" for _, state in states):
                logger.info(
                    "All %d host(s) in environment %s reached CREATED",
                    len(hosts), environment_id,
                )
                return hosts

            time.sleep(poll_interval_seconds)

        raise TimeoutError(
            f"Hosts in environment {environment_id} did not all reach CREATED "
            f"within {timeout_seconds}s. Last observed: {last_summary}."
        )

    def verify_environment_ready(self, environment_id: str) -> None:
        """Verify that an EVS environment is in the CREATED state.

        Args:
            environment_id: The ID of the EVS environment.

        Raises:
            RuntimeError: If the environment is not in the CREATED state.
        """
        response = self.get_environment(environment_id)
        environment = response.get("environment", {})
        state = environment.get("environmentState", "UNKNOWN")

        logger.info("Environment %s is in state: %s", environment_id, state)

        if state == "CREATING":
            raise RuntimeError(
                f"Environment {environment_id} is still being created. "
                f"Please check back shortly."
            )
        elif state in ("FAILED", "CREATE_FAILED"):
            raise RuntimeError(
                f"Environment {environment_id} is in a FAILED state. "
                f"Please open an AWS support case for assistance."
            )
        elif state != "CREATED":
            raise RuntimeError(
                f"Environment {environment_id} is in an unexpected state: {state}. "
                f"Expected CREATED."
            )

    def wait_for_environment_state(
        self,
        environment_id: str,
        desired_state: str = "CREATED",
        timeout_seconds: int = 5400,
        poll_interval_seconds: int = 60,
    ) -> dict[str, Any]:
        """Poll get_environment until the environment reaches the desired state.

        Used by the chained ``create-environment-and-hosts`` action so the
        operator doesn't have to manually poll between create-environment
        and create-hosts. Single-action runs don't need this.

        Treats common terminal failure states (``FAILED``, ``CREATE_FAILED``,
        ``DELETING``, ``DELETED``) as fatal and raises immediately. Anything
        else (``CREATING`` and unknown values) keeps polling until either
        the desired state is reached or the timeout elapses.

        Args:
            environment_id: The EVS environment to poll.
            desired_state: Target state (default ``CREATED``).
            timeout_seconds: Total budget. EVS environment creation
                typically completes in 5-10 minutes, but slow regions or
                AZ-level capacity issues can stretch it out — the
                90-minute default ceiling is generous enough to cover
                those without hanging the CLI indefinitely.
            poll_interval_seconds: Time between polls (default 60s).

        Returns:
            The full API response dict from the final ``get_environment``
            call once the desired state is reached.

        Raises:
            RuntimeError: If the environment lands in a fatal state.
            TimeoutError: If the desired state isn't reached in time.
        """
        import time

        terminal_failures = {"FAILED", "CREATE_FAILED", "DELETING", "DELETED"}
        deadline = time.time() + timeout_seconds
        last_state: str | None = None

        while time.time() < deadline:
            response = self.get_environment(environment_id)
            environment = response.get("environment", {})
            state = environment.get("environmentState", "UNKNOWN")

            if state != last_state:
                # Only log on transitions so a 30-minute wait doesn't flood
                # the log with repeated CREATING lines.
                logger.info(
                    "Environment %s state: %s (waiting for %s)",
                    environment_id, state, desired_state,
                )
                last_state = state

            if state == desired_state:
                logger.info(
                    "Environment %s reached state %s",
                    environment_id, desired_state,
                )
                return response

            if state in terminal_failures:
                raise RuntimeError(
                    f"Environment {environment_id} ended in fatal state '{state}'. "
                    f"Open an AWS support case for assistance."
                )

            time.sleep(poll_interval_seconds)

        raise TimeoutError(
            f"Environment {environment_id} did not reach state '{desired_state}' "
            f"within {timeout_seconds}s. Last observed state: {last_state}."
        )

    def associate_eip_to_vlan(
        self,
        environment_id: str,
        vlan_name: str,
        allocation_id: str,
    ) -> dict[str, Any]:
        """Associate an Elastic IP with an HCX VLAN.

        Called after CreateEnvironment when HCX public connectivity is
        enabled. The EIP must come from the same public IPAM pool that
        provided the VLAN's /28 CIDR.

        Args:
            environment_id: The EVS environment ID.
            vlan_name: The VLAN function name (e.g. 'hcx').
            allocation_id: The EIP allocation ID (eipalloc-...).

        Returns:
            The API response.
        """
        logger.info(
            "Associating EIP %s with VLAN %s in environment %s",
            allocation_id, vlan_name, environment_id,
        )
        return self._evs.associate_eip_to_vlan(
            environmentId=environment_id,
            vlanName=vlan_name,
            allocationId=allocation_id,
        )

    def create_environment_host(
        self,
        environment_id: str,
        host: dict[str, str],
        esx_version: str | None = None,
    ) -> dict[str, Any]:
        """Create a single host in an EVS environment.

        Args:
            environment_id: The ID of the target EVS environment.
            host: Host configuration with keys: hostName, keyName, instanceType,
                  and optionally placementGroupId, dedicatedHostId.
            esx_version: Optional ESXi version override.

        Returns:
            The API response containing the host and environment summary.
        """
        params: dict[str, Any] = {
            "environmentId": environment_id,
            "host": host,
        }

        if esx_version is not None:
            params["esxVersion"] = esx_version

        host_name = host.get("hostName", "unknown")
        logger.info(
            "Creating host '%s' in environment %s...",
            host_name,
            environment_id,
        )
        logger.debug("CreateEnvironmentHost params: %s", params)

        response = self._evs.create_environment_host(**params)

        created_host = response.get("host", {})
        state = created_host.get("hostState", "unknown")
        logger.info(
            "Host creation initiated: name=%s, state=%s",
            host_name,
            state,
        )

        return response

    def create_environment_hosts(
        self,
        environment_id: str,
        hosts: list[dict[str, str]],
        esx_version: str | None = None,
    ) -> list[dict[str, Any]]:
        """Create multiple hosts in an EVS environment.

        Verifies the environment is in CREATED state before proceeding.
        Calls create_environment_host for each host sequentially.

        Args:
            environment_id: The ID of the target EVS environment.
            hosts: List of host configurations.
            esx_version: Optional ESXi version override applied to all hosts.

        Returns:
            List of API responses, one per host.

        Raises:
            RuntimeError: If the environment is not in the CREATED state.
        """
        self.verify_environment_ready(environment_id)

        logger.info(
            "Creating %d host(s) in environment %s...",
            len(hosts),
            environment_id,
        )

        responses = []
        for host in hosts:
            response = self.create_environment_host(
                environment_id=environment_id,
                host=host,
                esx_version=esx_version,
            )
            responses.append(response)

        logger.info("All %d host creation requests submitted", len(hosts))
        return responses
