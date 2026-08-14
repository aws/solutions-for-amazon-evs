# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""EVS environment lifecycle management."""

import logging
from typing import Any

from aws_client import AWSClient

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
        client_token: str | None = None,
    ) -> dict[str, Any]:
        """Create an EVS environment.

        Args:
            vpc_id: VPC ID for the environment.
            service_access_subnet_id: Subnet ID for service access.
            vcf_version: VCF version to deploy, e.g. '9.0.2' or '9.1.0' (the
                numeric VCF 9.x version; sent verbatim as ``vcfVersion``).
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
            client_token: Idempotency token (``clientToken``, ``[!-~]+``, ≤100
                chars). MUST be deterministic across restarts (e.g. config-hash
                + env name) — a random per-call token makes a --resume create a
                SECOND billing environment invisible to destroy.py. Omitted →
                botocore generates a random one (no cross-process protection).

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
        if client_token is not None:
            params["clientToken"] = client_token

        logger.info("Creating EVS environment in VPC %s...", vpc_id)
        safe_params = {**params}
        if "licenseInfo" in safe_params:
            safe_params["licenseInfo"] = "***REDACTED***"
        logger.debug("CreateEnvironment params: %s", safe_params)

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

        def _version_sort_key(v: str) -> tuple[int, ...]:
            """Sort by the FULL numeric version tuple, not just the trailing
            build number — comparing only the last segment would rank
            'ESXi-9.0.1.100' above 'ESXi-9.0.2.50' even though 9.0.2 is newer.
            Non-numeric segments fall back to 0 so an unexpected version string
            sorts last instead of crashing resolution with a ValueError.
            """
            digits = v.removeprefix("ESXi-").split(".")
            key = []
            for d in digits:
                try:
                    key.append(int(d))
                except ValueError:
                    key.append(0)
            return tuple(key)

        matching.sort(key=_version_sort_key, reverse=True)
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
        timeout_seconds: int = 7200,
        poll_interval_seconds: int = 60,
    ) -> list[dict[str, Any]]:
        """Poll list_environment_hosts until every host reaches CREATED.

        Used by the ``deploy-environment`` chain so create-hosts flows into
        post-evs-sync-config without manual waiting. Host realization takes
        20-40 min/host; the 120-minute default budget covers that.

        Treats ``CREATE_FAILED``, ``UPDATE_FAILED``, ``DELETING``, ``DELETED``
        as fatal and raises immediately; ``CREATING`` (and unknown) keeps polling.

        Args:
            environment_id: The EVS environment to poll.
            timeout_seconds: Total budget. Default 120 minutes (7200s).
            poll_interval_seconds: Time between polls (default 60s).

        Returns:
            The final list of host dicts once all are ``CREATED``.

        Raises:
            RuntimeError: If any host lands in a fatal state.
            TimeoutError: If not all hosts reach CREATED in time.
        """
        import time

        # Terminal host states that mean creation will never succeed.
        # CREATING continues polling; everything else either succeeded
        # (CREATED) or is fatal for this wait.
        terminal_failures = {"CREATE_FAILED", "UPDATE_FAILED", "DELETING", "DELETED"}
        start_time = time.time()
        deadline = start_time + timeout_seconds
        last_summary: tuple[tuple[str, str], ...] | None = None
        poll_count = 0

        while time.time() < deadline:
            poll_count += 1
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
                    # EVS reports the actual cause in stateDetails (e.g. an
                    # EC2 vCPU limit, which the customer can raise themselves).
                    # Surface it -- telling them to open a support case when the
                    # fix is self-service sends them down the wrong path.
                    detail = (host.get("stateDetails") or "").strip()
                    raise RuntimeError(
                        f"Host '{host.get('hostName', '?')}' in environment "
                        f"{environment_id} ended in fatal state '{state}'. "
                        + (f"EVS reported: {detail}"
                           if detail else
                           "EVS reported no further detail — open an AWS "
                           "support case for assistance.")
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
            elif poll_count % 5 == 0:
                # Heartbeat: every 5th poll (~5 minutes at the default
                # 60s interval) log the current state even if unchanged,
                # so long waits show visible progress.
                elapsed = int(time.time() - start_time)
                remaining = int(deadline - time.time())
                logger.info(
                    "Still waiting: %s (%dm elapsed, %dm before timeout)",
                    ", ".join(f"{name}={state}" for name, state in states),
                    elapsed // 60, remaining // 60,
                )

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
        timeout_seconds: int = 7200,
        poll_interval_seconds: int = 60,
    ) -> dict[str, Any]:
        """Poll get_environment until the environment reaches the desired state.

        Used by the chained ``create-environment-and-hosts`` action so the
        operator needn't poll between create-environment and create-hosts.

        Treats ``FAILED``, ``CREATE_FAILED``, ``DELETING``, ``DELETED`` as
        fatal and raises immediately; anything else keeps polling until the
        desired state is reached or the timeout elapses.

        Args:
            environment_id: The EVS environment to poll.
            desired_state: Target state (default ``CREATED``).
            timeout_seconds: Total budget. EVS creation typically takes 5-10
                minutes; the 120-minute default ceiling covers slow regions or
                AZ capacity issues without hanging the CLI indefinitely.
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
        start_time = time.time()
        deadline = start_time + timeout_seconds
        last_state: str | None = None
        poll_count = 0

        while time.time() < deadline:
            poll_count += 1
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
            elif poll_count % 5 == 0:
                # Heartbeat: every 5th poll (~5 minutes at the default
                # 60s interval) log the current state even if unchanged,
                # so long waits show visible progress.
                elapsed = int(time.time() - start_time)
                remaining = int(deadline - time.time())
                logger.info(
                    "Still waiting: %s=%s (%dm elapsed, %dm before timeout)",
                    environment_id, state,
                    elapsed // 60, remaining // 60,
                )

            if state == desired_state:
                logger.info(
                    "Environment %s reached state %s",
                    environment_id, desired_state,
                )
                return response

            if state in terminal_failures:
                detail = (environment.get("stateDetails") or "").strip()
                raise RuntimeError(
                    f"Environment {environment_id} ended in fatal state '{state}'. "
                    + (f"EVS reported: {detail}"
                       if detail else
                       "EVS reported no further detail — open an AWS support "
                       "case for assistance.")
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

    def delete_environment_host(
        self,
        environment_id: str,
        host_name: str,
    ) -> dict[str, Any]:
        """Delete a single host from an EVS environment.

        Used to clear a host that landed in a fatal state. EVS keeps the
        ``hostName`` reserved for a failed host, so the record must be removed
        before that name can be recreated.
        """
        logger.info(
            "Deleting host '%s' from environment %s...", host_name, environment_id
        )
        response = self._evs.delete_environment_host(
            environmentId=environment_id,
            hostName=host_name,
        )
        logger.info("Host '%s' deletion initiated", host_name)
        return response

    def wait_for_hosts_absent(
        self,
        environment_id: str,
        host_names: list[str],
        timeout_minutes: int = 30,
        poll_seconds: int = 30,
    ) -> None:
        """Block until the named hosts no longer appear in the environment.

        Args:
            environment_id: The environment being reconciled.
            host_names: Host names expected to disappear.
            timeout_minutes: Give up after this long.
            poll_seconds: Delay between polls.

        Raises:
            RuntimeError: If any named host is still present at timeout.
        """
        import time

        pending = set(host_names)
        if not pending:
            return

        deadline = time.time() + timeout_minutes * 60
        still_there = set(pending)
        while time.time() < deadline:
            present = {
                h.get("hostName") for h in self.list_environment_hosts(environment_id)
            }
            still_there = pending & present
            if not still_there:
                logger.info(
                    "Host(s) %s removed from %s",
                    ", ".join(sorted(pending)), environment_id,
                )
                return
            logger.info(
                "Waiting for host deletion: %s still present",
                ", ".join(sorted(still_there)),
            )
            time.sleep(poll_seconds)

        raise RuntimeError(
            f"Host(s) {', '.join(sorted(still_there))} still present in "
            f"{environment_id} after {timeout_minutes} minutes. Delete them "
            f"manually and re-run with --start-from phase2_deploy."
        )

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
        created_host_names: list[str] = []
        for host in hosts:
            try:
                response = self.create_environment_host(
                    environment_id=environment_id,
                    host=host,
                    esx_version=esx_version,
                )
            except Exception:
                if created_host_names:
                    logger.critical(
                        "Host creation failed for '%s' in environment %s, but "
                        "%d host(s) were already created and are BILLING NOW: "
                        "%s. These hosts are NOT cleaned up automatically. "
                        "Tear them down with the teardown flow (destroy.py, "
                        "or the orchestrator's --destroy mode) for environment "
                        "%s before retrying.",
                        host.get("hostName", "unknown"),
                        environment_id,
                        len(created_host_names),
                        ", ".join(created_host_names),
                        environment_id,
                    )
                raise
            responses.append(response)
            created_host_names.append(host.get("hostName", "unknown"))

        logger.info("All %d host creation requests submitted", len(hosts))
        return responses
