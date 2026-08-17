#!/usr/bin/env python3
"""Standalone EVS environment destroyer.

Run from any machine with AWS credentials.
No need to SSH into the runner or have the full orchestrator installed.

Usage:
    # Tear down EVS environment only (safest — leaves infra for reuse)
    python3 destroy.py --bootstrap-stack evs-bootstrap-myenv --region us-east-2

    # Also remove the landing zone (NAT, Route Server, SG, key pair)
    python3 destroy.py --bootstrap-stack evs-bootstrap-myenv --region us-east-2 --include-infra

    # Full nuke: environment + landing zone + bootstrap stack
    python3 destroy.py --bootstrap-stack evs-bootstrap-myenv --region us-east-2 --all

    # NOTE: --all deletes the bootstrap stack (VPC, runner, IAM) and landing zone
    # (NAT, Route Server, SG, DNS zones). In BYO-VPC mode, only resources created
    # by this stack inside the VPC will be removed — nothing pre-existing is touched.

    # Use a specific AWS profile
    python3 destroy.py --bootstrap-stack evs-bootstrap-myenv --region us-east-2 --profile my-profile

    # Skip confirmation prompt
    python3 destroy.py --bootstrap-stack evs-bootstrap-myenv --region us-east-2 --all -y

Prerequisites:
    pip install boto3
    AWS credentials configured (via env vars, profile, or SSO)
"""

import argparse
import logging
import sys
import time

import boto3
from botocore.exceptions import ClientError

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-5s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("evs-destroy")


class _WarningCounter(logging.Handler):
    """Records WARNING+ messages so the final summary can flag a partial run.

    Steps warn-and-continue so one wedged resource doesn't abort teardown, which
    means the operator can be told "DESTROY COMPLETE" while resources still bill —
    the summary must reflect what was skipped.
    """

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        if len(self.messages) < 25:
            self.messages.append(record.getMessage().strip())


_warning_log = _WarningCounter()
logger.addHandler(_warning_log)

# Landing-zone stack naming: current suffix scheme
# (<bootstrap-stack>-amazon-evs-<version>-infrastructure); legacy prefix scheme
# (evs-aws-config-<bootstrap-stack>) still probed for older deployments.
LEGACY_AWS_CONFIG_STACK_PREFIX = "evs-aws-config-"


def _list_all_environment_hosts(evs_client, env_id: str) -> list:
    """List every environment host, following nextToken pagination.

    The raw list_environment_hosts returns a single page; without draining
    nextToken, hosts on later pages are skipped — leaving still-billing hosts
    behind while teardown reports success.
    """
    hosts, token = [], None
    while True:
        kwargs = {"environmentId": env_id}
        if token:
            kwargs["nextToken"] = token
        resp = evs_client.list_environment_hosts(**kwargs)
        hosts.extend(resp.get("environmentHosts", []))
        token = resp.get("nextToken")
        if not token:
            return hosts


def _list_all_environment_connectors(evs_client, env_id: str) -> list:
    """List every environment connector, following nextToken pagination."""
    connectors, token = [], None
    while True:
        kwargs = {"environmentId": env_id}
        if token:
            kwargs["nextToken"] = token
        resp = evs_client.list_environment_connectors(**kwargs)
        connectors.extend(resp.get("connectors", []))
        token = resp.get("nextToken")
        if not token:
            return connectors


# ---------------------------------------------------------------------------
# Auto-discovery
# ---------------------------------------------------------------------------

def discover_from_stack(session, stack_name: str, evs_endpoint_url: str | None = None,
                        cli_environment_id: str | None = None) -> dict:
    """Read the bootstrap stack to discover environment ID, VPC, and related resources.

    Requires the bootstrap stack's VpcId output; exits with an error rather than
    guessing if absent. The aws-config stack name derives from the bootstrap name.

    ``cli_environment_id`` (the --environment-id override) only changes logging:
    a miss with no override silently skips environment teardown, so it logs at
    WARNING (flagging a partial run); with an override the miss is expected.
    """
    cfn = session.client("cloudformation")
    info = {
        "vpc_id": None,
        "environment_id": None,
        "aws_config_stack": None,
        "stack_created_vpc": False,
    }

    # Read stack outputs
    try:
        resp = cfn.describe_stacks(StackName=stack_name)
        stack = resp["Stacks"][0]
    except ClientError as e:
        err = e.response.get("Error", {})
        if err.get("Code") == "ValidationError" and "does not exist" in err.get("Message", ""):
            logger.error("Stack %s does not exist in this region", stack_name)
        else:
            logger.error("Could not describe stack %s: %s", stack_name, e)
        sys.exit(1)

    outputs = {o["OutputKey"]: o["OutputValue"] for o in stack.get("Outputs", [])}
    info["vpc_id"] = outputs.get("VpcId")

    if not info["vpc_id"]:
        logger.error("Stack %s has no VpcId output. Are you sure this is the bootstrap stack?", stack_name)
        logger.error("The bootstrap stack name is what you passed to CloudFormation (e.g. 'evs-bootstrap-myenv').")
        sys.exit(1)

    logger.info("  VPC: %s", info["vpc_id"])

    # Check if the stack created the VPC (has a Vpc resource)
    try:
        resources = _list_all_stack_resources(cfn, stack_name)
        for r in resources:
            if r.get("LogicalResourceId") == "Vpc" and r.get("ResourceType") == "AWS::EC2::VPC":
                info["stack_created_vpc"] = True
                break
    except ClientError as e:
        logger.warning("  Could not list resources for stack %s: %s", stack_name, e)

    # Discover environment ID from EVS API — match by VPC only
    evs_client = (
        session.client("evs", endpoint_url=evs_endpoint_url)
        if evs_endpoint_url else session.client("evs")
    )
    try:
        envs = []
        paginator = evs_client.get_paginator("list_environments")
        for page in paginator.paginate():
            envs.extend(page.get("environmentSummaries", []))
        for env in envs:
            try:
                env_detail = evs_client.get_environment(
                    environmentId=env["environmentId"]
                ).get("environment", {})
            except ClientError as e:
                if _aws_error_code(e) != "ResourceNotFoundException":
                    logger.warning("  Could not get environment %s: %s",
                                   env["environmentId"], e)
                continue
            state = env_detail.get("environmentState", "UNKNOWN")
            if state == "DELETED":
                continue
            if env_detail.get("vpcId") == info["vpc_id"]:
                info["environment_id"] = env_detail["environmentId"]
                logger.info("  Environment: %s (%s, state=%s)",
                            info["environment_id"],
                            env_detail.get("environmentName", ""),
                            state)
                break
    except ClientError as e:
        logger.warning("  Could not discover environment from EVS API: %s", e)

    if not info["environment_id"]:
        if cli_environment_id:
            # Operator supplied --environment-id, which overrides discovery —
            # a miss here is expected with a custom --endpoint-url and harmless.
            logger.info("  No active environment found in VPC %s "
                        "(using --environment-id override)", info["vpc_id"])
        else:
            # A miss skips the entire environment teardown while hosts/volumes/
            # secrets may still exist and bill — WARNING (not INFO) flags the run
            # as partial and exits non-zero instead of faking a clean teardown.
            logger.warning(
                "  No active environment found in VPC %s — environment "
                "teardown will be SKIPPED. If an environment for this stack "
                "still exists (check the EVS console; an environment created "
                "against a custom endpoint needs the matching --endpoint-url), its hosts "
                "may still be billing. Re-run with --environment-id to "
                "target it explicitly.", info["vpc_id"])

    # aws-config stack name. Current: <bootstrap>-amazon-evs-<vcf-version>-
    # infrastructure (version unknown here, so scan). Legacy probed directly:
    #   evs-aws-config-<bootstrap-stack-name>
    found = None
    try:
        paginator = cfn.get_paginator("list_stacks")
        active = [
            "CREATE_COMPLETE", "UPDATE_COMPLETE", "UPDATE_ROLLBACK_COMPLETE",
            "CREATE_IN_PROGRESS", "ROLLBACK_COMPLETE", "DELETE_FAILED",
        ]
        for page in paginator.paginate(StackStatusFilter=active):
            for summary in page.get("StackSummaries", []):
                name = summary.get("StackName", "")
                if name.startswith(f"{stack_name}-amazon-evs-") and name.endswith("-infrastructure"):
                    found = name
                    break
            if found:
                break
    except ClientError as e:
        logger.warning("  Could not list stacks for landing-zone discovery: %s", e)
    if not found:
        legacy = f"{LEGACY_AWS_CONFIG_STACK_PREFIX}{stack_name}"[:128]
        try:
            cfn.describe_stacks(StackName=legacy)
            found = legacy
        except ClientError as e:
            err = e.response.get("Error", {})
            if not (err.get("Code") == "ValidationError" and "does not exist" in err.get("Message", "")):
                logger.warning("  Could not describe stack %s: %s", legacy, e)
    if found:
        info["aws_config_stack"] = found
        logger.info("  Landing zone stack: %s", info["aws_config_stack"])
    else:
        # Legacy fallback: accept a pattern-matched stack only if its resources
        # live in the same VPC AND its name is anchored to this bootstrap stack —
        # else a shared BYO-VPC could match and delete someone else's landing zone.
        fallback = _find_legacy_aws_config_stack(session, cfn, info["vpc_id"], stack_name)
        if fallback:
            info["aws_config_stack"] = fallback
            logger.info("  Landing zone stack (legacy match): %s", info["aws_config_stack"])
        else:
            logger.info("  No landing zone stack found")

    return info


def _find_legacy_aws_config_stack(session, cfn, vpc_id: str, bootstrap_stack_name: str):
    """Find a legacy aws-config stack, anchored to this specific bootstrap
    stack's name, and verified against the VPC.

    Candidates match ``evs-aws-config-<bootstrap-stack>`` (legacy prefix) or the
    suffix scheme (in case the primary lookup missed a DELETE_FAILED stack). The
    name anchor is required: VPC/SG membership alone is unsafe in a shared BYO-VPC
    where multiple deployments' landing zones share security groups.
    """
    ec2 = session.client("ec2")
    legacy_name = f"{LEGACY_AWS_CONFIG_STACK_PREFIX}{bootstrap_stack_name}"[:128]
    suffix_prefix = f"{bootstrap_stack_name}-amazon-evs-"
    try:
        paginator = cfn.get_paginator("list_stacks")
        pages = paginator.paginate(
            StackStatusFilter=[
                "CREATE_COMPLETE", "UPDATE_COMPLETE", "UPDATE_ROLLBACK_COMPLETE",
                "ROLLBACK_COMPLETE", "DELETE_FAILED", "CREATE_IN_PROGRESS",
            ]
        )
        for page in pages:
            for summary in page.get("StackSummaries", []):
                name = summary.get("StackName", "")
                if not (
                    name == legacy_name
                    or (name.startswith(suffix_prefix) and name.endswith("-infrastructure"))
                ):
                    continue
                try:
                    resources = _list_all_stack_resources(cfn, name)
                    for r in resources:
                        if r.get("ResourceType") != "AWS::EC2::SecurityGroup":
                            continue
                        sg_id = r.get("PhysicalResourceId", "")
                        if not sg_id:
                            continue
                        groups = ec2.describe_security_groups(GroupIds=[sg_id]).get(
                            "SecurityGroups", []
                        )
                        if groups and groups[0].get("VpcId") == vpc_id:
                            return name
                except ClientError as e:
                    logger.warning("  Could not inspect stack %s during legacy lookup: %s", name, e)
                    continue
    except ClientError as e:
        logger.warning("  Legacy stack lookup failed: %s", e)
    return None


# ---------------------------------------------------------------------------
# Destroy steps
# ---------------------------------------------------------------------------

def _aws_error_code(e: Exception) -> str:
    return getattr(e, "response", {}).get("Error", {}).get("Code", "")


def _list_all_stack_resources(cfn, stack_name: str) -> list:
    """Paginated list_stack_resources — the API pages at ~100 resources, so a
    single unpaginated call silently misses resources past page 1 on larger
    stacks, breaking callers that scan for SGs, instances, or ENIs.
    """
    resources = []
    paginator = cfn.get_paginator("list_stack_resources")
    for page in paginator.paginate(StackName=stack_name):
        resources.extend(page.get("StackResourceSummaries", []))
    return resources


def delete_hosts(evs_client, env_id: str) -> None:
    """Delete all hosts in the environment and wait for completion."""
    logger.info("[1/5] Deleting hosts...")
    try:
        hosts = _list_all_environment_hosts(evs_client, env_id)
    except ClientError as e:
        if _aws_error_code(e) in ("ResourceNotFoundException", "ResourceNotFoundFault", "404") \
                or "not found" in str(e).lower():
            logger.info("  Environment not found — skipping host deletion")
            return
        logger.warning("  Could not list hosts: %s", e)
        return

    if not hosts:
        logger.info("  No hosts found")
        return

    for host in hosts:
        host_name = host.get("hostName", "unknown")
        state = host.get("hostState", "UNKNOWN")
        if state in ("DELETED", "DELETING"):
            logger.info("  Host %s already %s", host_name, state)
            continue
        logger.info("  Requesting deletion of host: %s (state=%s)", host_name, state)
        try:
            evs_client.delete_environment_host(environmentId=env_id, hostName=host_name)
        except ClientError as e:
            if _aws_error_code(e) in ("ResourceNotFoundException", "ResourceNotFoundFault", "404"):
                logger.info("  Host %s already gone", host_name)
                continue
            logger.warning("  Could not delete host %s: %s", host_name, e)

    # Wait for all hosts to be gone
    logger.info("  Waiting for hosts to terminate (may take 5-15 min)...")
    deadline = time.time() + 1800
    while time.time() < deadline:
        try:
            all_hosts = _list_all_environment_hosts(evs_client, env_id)
            remaining = [h for h in all_hosts if h.get("hostState") not in ("DELETED", "DELETE_FAILED")]
            failed = [h for h in all_hosts if h.get("hostState") == "DELETE_FAILED"]
            if failed:
                # DELETE_FAILED is a terminal failure state, not "done" —
                # treating it as DELETED would mask a still-present, still-billing
                # host as a successful teardown.
                names = [h.get("hostName", "?") for h in failed]
                logger.warning("  %d host(s) reached DELETE_FAILED and will NOT be retried "
                               "automatically: %s. These are likely still billing — "
                               "investigate in the EVS console and delete manually, then "
                               "re-run this script.", len(failed), names)
            if not remaining:
                if failed:
                    return
                logger.info("  All hosts deleted ✓")
                return
            names = [h.get("hostName", "?") for h in remaining]
            logger.info("  %d host(s) still deleting: %s", len(remaining), names)
        except ClientError as e:
            if _aws_error_code(e) in ("ResourceNotFoundException", "ResourceNotFoundFault", "404") \
                    or "not found" in str(e).lower():
                logger.info("  Environment gone — hosts implicitly deleted ✓")
                return
            logger.warning("  Error polling: %s", e)
        time.sleep(30)

    logger.error("  Timed out waiting for host deletion (30 min)")
    sys.exit(1)


def delete_ebs_volumes(ec2_client, env_id: str) -> None:
    """Detach and delete EBS volumes tagged by the orchestrator (self-deployed mode only)."""
    logger.info("[2/5] Checking for orchestrator EBS volumes...")
    try:
        volumes = ec2_client.describe_volumes(
            Filters=[
                {"Name": "tag:ManagedBy", "Values": ["phase2-automation"]},
                {"Name": "tag:EnvironmentId", "Values": [env_id]},
            ]
        ).get("Volumes", [])
    except ClientError as e:
        logger.warning("  Could not list volumes: %s", e)
        return

    if not volumes:
        logger.info("  No tagged EBS volumes found")
        return

    for vol in volumes:
        vol_id = vol["VolumeId"]
        try:
            if vol.get("Attachments"):
                logger.info("  Detaching %s...", vol_id)
                ec2_client.detach_volume(VolumeId=vol_id, Force=True)
                ec2_client.get_waiter("volume_available").wait(
                    VolumeIds=[vol_id], WaiterConfig={"Delay": 10, "MaxAttempts": 30}
                )
            logger.info("  Deleting %s...", vol_id)
            ec2_client.delete_volume(VolumeId=vol_id)
            logger.info("  %s deleted ✓", vol_id)
        except ClientError as e:
            if _aws_error_code(e) in ("InvalidVolume.NotFound", "ResourceNotFoundException"):
                logger.info("  %s already gone", vol_id)
                continue
            logger.warning("  Could not remove %s: %s", vol_id, e)
        except Exception as e:
            # Waiter failures (WaiterError) are not ClientError
            logger.warning("  Could not remove %s: %s", vol_id, e)


def delete_secrets(sm_client, env_id: str) -> None:
    """Delete all secrets associated with this environment."""
    logger.info("[5/5] Deleting secrets...")

    total = 0
    for prefix in (f"evs-{env_id}", f"evs!{env_id}"):
        try:
            paginator = sm_client.get_paginator("list_secrets")
            secrets = [
                s["Name"]
                for page in paginator.paginate(Filters=[{"Key": "name", "Values": [prefix]}])
                for s in page.get("SecretList", [])
            ]
        except ClientError as e:
            logger.warning("  Could not list secrets for prefix '%s': %s", prefix, e)
            continue

        for name in secrets:
            # Exact-env guard: the list_secrets name filter is a PREFIX match, so
            # it also returns a DIFFERENT environment whose id merely starts with
            # this one (e.g. env-abc123 vs env-abc1234). Only delete this env's own
            # secrets: the bare prefix, or prefix + "_<role/host>".
            if not (name == prefix or name.startswith(prefix + "_")):
                logger.info("  Skipping %s (belongs to a different environment)", name)
                continue
            try:
                # Recoverable delete (default 30-day window) rather than
                # ForceDeleteWithoutRecovery: an accidental or partial teardown
                # can be undone, and a stray prefix match is never irreversibly
                # destroyed.
                sm_client.delete_secret(SecretId=name)
                logger.info("  Deleted (30-day recovery): %s", name)
                total += 1
            except ClientError as e:
                code = _aws_error_code(e)
                if code == "ResourceNotFoundException":
                    logger.info("  %s already gone", name)
                    continue
                if code == "InvalidRequestException" and "scheduled for deletion" in str(e):
                    logger.info("  %s already scheduled for deletion", name)
                    continue
                logger.warning("  Could not delete %s: %s", name, e)

    logger.info("  %d secret(s) deleted ✓", total)


def check_lingering_resources(session, env_id: str | None, vpc_id: str | None) -> None:
    """Post-teardown cross-check — LOG ONLY, deletes nothing.

    Looks for resources that should be gone after teardown: orchestrator-tagged
    EBS volumes (ManagedBy=phase2-automation) scoped to this environment/VPC and
    its evs-* secrets. Anything found logs at WARNING (a non-zero partial result)
    but is not deleted — guarding against a clean-teardown report while it bills.
    """
    if not env_id and not vpc_id:
        return
    logger.info("Cross-checking for lingering resources...")
    ec2 = session.client("ec2")

    # EBS volumes tagged by the orchestrator
    try:
        filters = [{"Name": "tag:ManagedBy", "Values": ["phase2-automation"]}]
        if env_id:
            filters.append({"Name": "tag:EnvironmentId", "Values": [env_id]})
        volumes = []
        paginator = ec2.get_paginator("describe_volumes")
        for page in paginator.paginate(Filters=filters):
            volumes.extend(page.get("Volumes", []))
        if not env_id and vpc_id:
            # No environment ID to match on — attribute volumes via attachment
            # to an instance in the known VPC. An unattached volume can't be
            # tied to a VPC, so it is deliberately not flagged here (stays scoped).
            instance_ids = set()
            inst_paginator = ec2.get_paginator("describe_instances")
            for page in inst_paginator.paginate(
                Filters=[{"Name": "vpc-id", "Values": [vpc_id]}]
            ):
                for reservation in page.get("Reservations", []):
                    for inst in reservation.get("Instances", []):
                        instance_ids.add(inst["InstanceId"])
            volumes = [
                v for v in volumes
                if any(a.get("InstanceId") in instance_ids
                       for a in v.get("Attachments", []))
            ]
        if volumes:
            names = [f"{v['VolumeId']} (state={v.get('State', '?')})" for v in volumes]
            logger.warning("  %d orchestrator-tagged EBS volume(s) still present and "
                           "possibly billing (NOT deleted by this run): %s",
                           len(volumes), names)
        else:
            logger.info("  No lingering orchestrator EBS volumes found")
    except ClientError as e:
        logger.warning("  Could not check for lingering EBS volumes: %s", e)

    # Environment secrets (only checkable when the environment ID is known —
    # secret names embed it)
    if env_id:
        sm = session.client("secretsmanager")
        try:
            leftover = set()
            for prefix in (f"evs-{env_id}", f"evs!{env_id}"):
                sm_paginator = sm.get_paginator("list_secrets")
                for page in sm_paginator.paginate(
                    Filters=[{"Key": "name", "Values": [prefix]}]
                ):
                    leftover.update(s["Name"] for s in page.get("SecretList", []))
            if leftover:
                logger.warning("  %d secret(s) for environment %s still present "
                               "(NOT deleted by this run): %s",
                               len(leftover), env_id, sorted(leftover))
            else:
                logger.info("  No lingering secrets found for environment %s", env_id)
        except ClientError as e:
            logger.warning("  Could not check for lingering secrets: %s", e)


def delete_connectors(evs_client, env_id: str) -> None:
    """Delete any environment connectors."""
    logger.info("[3/5] Deleting environment connectors...")
    try:
        connectors = _list_all_environment_connectors(evs_client, env_id)
    except ClientError as e:
        if _aws_error_code(e) in ("ResourceNotFoundException", "ResourceNotFoundFault", "404") \
                or "not found" in str(e).lower():
            logger.info("  Environment not found — no connectors")
            return
        logger.warning("  Could not list connectors: %s", e)
        return

    if not connectors:
        logger.info("  No connectors found")
        return

    for c in connectors:
        cid = c.get("connectorId")
        logger.info("  Deleting connector %s...", cid)
        try:
            evs_client.delete_environment_connector(environmentId=env_id, connectorId=cid)
        except ClientError as e:
            if _aws_error_code(e) in ("ResourceNotFoundException", "ResourceNotFoundFault", "404"):
                logger.info("  Connector %s already gone", cid)
                continue
            logger.warning("  Could not delete connector %s: %s", cid, e)
            continue

        # Wait for connector deletion
        deadline = time.time() + 600
        while time.time() < deadline:
            try:
                remaining = _list_all_environment_connectors(evs_client, env_id)
                if not any(x.get("connectorId") == cid for x in remaining):
                    logger.info("  Connector %s deleted ✓", cid)
                    break
            except ClientError as e:
                if _aws_error_code(e) not in ("ResourceNotFoundException", "ResourceNotFoundFault", "404"):
                    logger.warning("  Error polling connector %s: %s", cid, e)
                break
            time.sleep(15)
        else:
            logger.warning(
                "  Connector %s did not confirm deletion within 600s — it may "
                "block environment deletion; check the EVS console.", cid,
            )


def delete_environment(evs_client, env_id: str) -> bool:
    """Delete the EVS environment and wait for completion.

    Returns True only if deletion was confirmed (reached DELETED or already
    gone). Returns False on timeout without confirmation — callers must NOT
    force-delete this environment's secrets then, as it may still be live.
    """
    logger.info("[4/5] Deleting EVS environment %s...", env_id)

    # Retry loop: environment might still be CREATING
    deadline = time.time() + 1800
    while True:
        try:
            evs_client.delete_environment(environmentId=env_id)
            logger.info("  Delete initiated")
            break
        except ClientError as e:
            code = _aws_error_code(e)
            if code in ("ResourceNotFoundException", "ResourceNotFoundFault", "404") \
                    or "not found" in str(e).lower():
                logger.info("  Environment already deleted ✓")
                return True
            # Check actual state
            try:
                state = (evs_client.get_environment(environmentId=env_id)
                         .get("environment", {}).get("environmentState", "UNKNOWN"))
            except ClientError as query_error:
                if _aws_error_code(query_error) in ("ResourceNotFoundException", "ResourceNotFoundFault", "404"):
                    logger.info("  Environment already deleted ✓")
                    return True
                logger.error("  Cannot delete or query environment: %s (query error: %s)", e, query_error)
                sys.exit(1)

            if state in ("DELETING", "DELETED"):
                logger.info("  Already %s", state)
                break
            if state == "CREATING" and time.time() < deadline:
                logger.info("  Environment still CREATING — waiting 60s to retry...")
                time.sleep(60)
                continue
            if time.time() >= deadline:
                logger.error("  Timed out waiting for environment to become deletable")
                sys.exit(1)
            logger.warning("  Delete rejected (state=%s): %s — retrying in 60s...", state, e)
            time.sleep(60)

    # Poll until deleted (30-minute deadline; on timeout, warn and continue
    # so the rest of the teardown still runs — the final summary will flag it)
    logger.info("  Waiting for environment deletion...")
    deadline = time.time() + 1800
    while time.time() < deadline:
        try:
            resp = evs_client.get_environment(environmentId=env_id)
            state = resp.get("environment", {}).get("environmentState", "UNKNOWN")
            if state == "DELETED":
                logger.info("  Environment deleted ✓")
                return True
            logger.info("  State: %s — waiting...", state)
        except ClientError as e:
            if _aws_error_code(e) in ("ResourceNotFoundException", "ResourceNotFoundFault", "404") \
                    or "not found" in str(e).lower():
                logger.info("  Environment deleted ✓")
                return True
            logger.warning("  Error polling: %s", e)
        time.sleep(30)

    logger.error("  Timed out waiting for environment %s deletion (30 min) — "
                 "continuing with remaining teardown; verify in the EVS console", env_id)
    logger.warning("  Environment %s may still exist and may still be billing", env_id)
    return False


def _stack_exists(cfn, stack_name: str, attempts: int = 5) -> bool:
    """Determine authoritatively whether a stack exists.

    Only CloudFormation's ``ValidationError ... does not exist`` proves absence;
    every other failure (network, throttling, expired credentials) is transient
    and retried with backoff, and propagates if still undetermined.

    This matters: treating a momentary blip as "absent" would let a teardown that
    deleted nothing report DESTROY COMPLETE, silently leaving billing infra behind.
    """
    last_error = None
    for attempt in range(1, attempts + 1):
        try:
            cfn.describe_stacks(StackName=stack_name)
            return True
        except ClientError as e:
            err = e.response.get("Error", {})
            if err.get("Code") == "ValidationError" and "does not exist" in err.get("Message", ""):
                return False
            last_error = e
        except Exception as e:
            last_error = e

        if attempt < attempts:
            delay = min(2 ** attempt, 30)
            logger.warning(
                "  Could not determine whether stack %s exists (%s); "
                "retrying in %ds (attempt %d/%d)",
                stack_name, last_error, delay, attempt, attempts,
            )
            time.sleep(delay)

    raise RuntimeError(
        f"Could not determine whether stack {stack_name} exists after "
        f"{attempts} attempts: {last_error}. Refusing to report success -- "
        f"re-run the destroy once connectivity is restored and verify in the "
        f"CloudFormation console."
    )


def delete_cfn_stack(session, stack_name: str, vpc_id: str = None,
                      stack_created_vpc: bool = False) -> None:
    """Delete a CloudFormation stack with optional DHCP disassociation.

    ``stack_created_vpc`` gates the DHCP reset: run it only against a VPC this
    tool created, never a customer's BYO-VPC — resetting DHCP options there would
    silently wipe their corporate DNS/NTP settings.
    """
    cfn = session.client("cloudformation")
    logger.info("  Deleting stack: %s", stack_name)

    if not _stack_exists(cfn, stack_name):
        logger.info("  Stack %s does not exist — nothing to do", stack_name)
        return

    # Disassociate custom DHCP options before deleting (avoids dependency
    # error) — only for a VPC this tool created. Never touch DHCP options
    # on a customer-supplied BYO-VPC.
    if vpc_id and stack_created_vpc:
        try:
            ec2 = session.client("ec2")
            ec2.associate_dhcp_options(DhcpOptionsId="default", VpcId=vpc_id)
            logger.info("  VPC re-pointed to default DHCP options")
        except Exception as e:
            logger.warning("  DHCP disassociation failed (continuing): %s", e)

    # Disable termination protection on the runner if present
    try:
        resources = _list_all_stack_resources(cfn, stack_name)
        for r in resources:
            if r.get("ResourceType") == "AWS::EC2::Instance":
                instance_id = r.get("PhysicalResourceId", "")
                if instance_id.startswith("i-"):
                    ec2 = session.client("ec2")
                    ec2.modify_instance_attribute(
                        InstanceId=instance_id,
                        DisableApiTermination={"Value": False},
                    )
                    logger.info("  Disabled termination protection on %s", instance_id)
    except Exception as e:
        logger.warning("  Could not disable termination protection: %s", e)

    # EVS's environment-delete sometimes leaves a service-managed ENI on this
    # stack's SG even after the environment is gone, blocking SG deletion with a
    # DependencyViolation. Drain it before delete_stack, and retry if still hit.
    if vpc_id:
        _wait_for_evs_managed_enis_to_clear(session, stack_name, vpc_id)

    _delete_stack_with_eni_retry(cfn, stack_name)


def _wait_for_evs_managed_enis_to_clear(session, stack_name: str, vpc_id: str,
                                         timeout_seconds: int = 900) -> None:
    """Poll (up to ``timeout_seconds``) for AmazonEVS-managed ENIs on this stack's
    security group(s) to release. Owned by EVS's service account
    (RequesterManaged=True), they can't be force-detached — we can only wait for
    EVS's cleanup, which sometimes lags well behind the environment's deletion.

    Only EVS-owned ENIs are waited on: filtering by security-group-id alone would
    also match this stack's OWN ENIs (runner, jumpbox), which release only on
    stack deletion, so waiting on them would always time out even on a clean run.
    """
    ec2 = session.client("ec2")
    cfn = session.client("cloudformation")
    try:
        resources = _list_all_stack_resources(cfn, stack_name)
        sg_ids = [
            r["PhysicalResourceId"] for r in resources
            if r.get("ResourceType") == "AWS::EC2::SecurityGroup" and r.get("PhysicalResourceId")
        ]
        # This stack's own instances (runner, jumpbox): their ENIs stay attached
        # until the stack delete removes the instance, so exclude them from the
        # "lingering EVS ENI" wait.
        own_instance_ids = {
            r["PhysicalResourceId"] for r in resources
            if r.get("ResourceType") == "AWS::EC2::Instance" and r.get("PhysicalResourceId")
        }
        # Resolver endpoints THIS stack owns: their ENIs only release when the
        # stack is deleted, so they must not be waited on (see _lingering_evs_enis).
        own_resolver_endpoint_ids = {
            r["PhysicalResourceId"] for r in resources
            if r.get("ResourceType") == "AWS::Route53Resolver::ResolverEndpoint"
            and r.get("PhysicalResourceId")
        }
    except ClientError as e:
        logger.warning("  Could not list stack resources to check for lingering ENIs: %s", e)
        return
    if not sg_ids:
        return

    def _lingering_evs_enis():
        enis = ec2.describe_network_interfaces(
            Filters=[{"Name": "group-id", "Values": sg_ids}]
        ).get("NetworkInterfaces", [])
        result = []
        for eni in enis:
            if not eni.get("RequesterManaged"):
                continue  # not EVS/AWS-service-owned — e.g. our own runner/jumpbox ENI
            attachment = eni.get("Attachment") or {}
            if attachment.get("InstanceId") in own_instance_ids:
                continue  # attached to this stack's own instance, not EVS's
            # Skip ENIs owned by a resource THIS stack still owns. They cannot
            # release until the stack is deleted, so waiting on them deadlocks
            # against the very delete_stack call this wait precedes.
            #
            # The Route 53 Resolver inbound endpoint is the concrete case: it is
            # a resource in this stack (AWS::Route53Resolver::ResolverEndpoint)
            # and always creates exactly 2 requester-managed ENIs (an inbound
            # endpoint requires >=2 IPs in different AZs) whose descriptions are
            # "Route 53 Resolver: <resolver-endpoint-id>:...". They carry this
            # stack's SG and RequesterManaged=True, so neither of the guards
            # above excludes them. Measured: every teardown with intact
            # infrastructure burned the FULL 900s timeout on exactly these 2
            # ENIs, then proceeded and deleted cleanly — i.e. this wait never
            # once caught a genuine lagging EVS ENI, it only ever deadlocked on
            # its own stack's resolver endpoint.
            desc = eni.get("Description") or ""
            if any(rid and rid in desc for rid in own_resolver_endpoint_ids):
                continue
            result.append(eni)
        return result

    deadline = time.time() + timeout_seconds
    logged_once = False
    last_count = 0
    while time.time() < deadline:
        try:
            enis = _lingering_evs_enis()
        except ClientError as e:
            logger.warning("  Could not check for lingering ENIs: %s", e)
            return
        if not enis:
            return
        last_count = len(enis)
        if not logged_once:
            logger.info("  Waiting for %d EVS-managed network interface(s) to release "
                        "before deleting the security group (this can lag behind the "
                        "environment being reported deleted)...", last_count)
            logged_once = True
        time.sleep(30)
    if last_count:
        logger.warning("  %d network interface(s) still attached to this stack's security "
                       "group(s) after %ds — deletion may fail with a DependencyViolation; "
                       "retrying the delete itself will also be attempted",
                       last_count, timeout_seconds)


def _delete_stack_dependency_violation(cfn, stack_name: str) -> bool:
    """Check whether this stack's most recent failed delete was a
    DependencyViolation. The waiter's error text never says so (only "we matched
    expected path: 'DELETE_FAILED'"), so read it from the stack's events instead.
    """
    try:
        events = cfn.describe_stack_events(StackName=stack_name).get("StackEvents", [])
    except ClientError:
        return False
    for event in events[:20]:  # most recent first
        if event.get("ResourceStatus") == "DELETE_FAILED":
            reason = event.get("ResourceStatusReason", "")
            if "DependencyViolation" in reason or "dependent object" in reason:
                return True
    return False


def _delete_stack_with_eni_retry(cfn, stack_name: str, max_attempts: int = 5) -> None:
    """Delete a stack, retrying on a security group DependencyViolation from a
    lingering EVS-managed ENI. Fallback to _wait_for_evs_managed_enis_to_clear's
    pre-wait, in case a new ENI attaches after that wait completed.
    """
    for attempt in range(1, max_attempts + 1):
        cfn.delete_stack(StackName=stack_name)
        logger.info("  Stack deletion initiated — waiting... (attempt %d/%d)",
                    attempt, max_attempts)
        try:
            cfn.get_waiter("stack_delete_complete").wait(
                StackName=stack_name, WaiterConfig={"Delay": 30, "MaxAttempts": 60}
            )
            logger.info("  Stack %s deleted ✓", stack_name)
            return
        except Exception as e:
            is_dependency_violation = _delete_stack_dependency_violation(cfn, stack_name)
            if is_dependency_violation and attempt < max_attempts:
                logger.warning("  Stack deletion failed on attempt %d/%d due to a "
                               "DependencyViolation (likely a lingering ENI) — "
                               "retrying in 90s...", attempt, max_attempts)
                time.sleep(90)
                continue
            logger.error("  Stack deletion failed or timed out: %s", e)
            logger.error("  Check the CloudFormation console for details")
            sys.exit(1)


# ---------------------------------------------------------------------------
# HCX networking teardown (HI-4)
# ---------------------------------------------------------------------------

def _hcx_tag(aws_config_stack: str | None) -> str | None:
    """The Name tag prefix the orchestrator stamps on HCX IPAM/EIP resources."""
    return f"EVS-HCX-{aws_config_stack}" if aws_config_stack else None


# ec2:ReleaseAddress attempts per HCX EIP. evs:DisassociateEipFromVlan is async,
# so the first release can lose the race with InvalidIPAddress.InUse. 6 attempts
# with capped exponential backoff (2,4,8,16,30 = ~60s) comfortably covers the
# observed detach lag without stalling teardown.
_EIP_RELEASE_ATTEMPTS = 6


def release_hcx_eips(session, aws_config_stack: str | None, environment_id: str | None = None,
                      evs_client=None) -> None:
    """Release the HCX public EIPs BEFORE deleting the environment.

    HCX-internet deployments allocate up to 3 EIPs (Manager/IX/NE) tagged
    Name=EVS-HCX-<stack>-*. EVS rejects DeleteEnvironment while the public
    subnet still has EIP associations, so these must be released first.

    The association EVS creates between an HCX EIP and the public HCX VLAN is
    made through EVS's OWN API (AssociateEipToVlan), not a plain EC2
    association -- so ec2:DisassociateAddress/ec2:ReleaseAddress alone can
    leave a stuck EIP: EC2 reports it "in use" (EVS's side still claims it) but
    ec2:DisassociateAddress reports the association "not found" (there's no
    EC2-level association to find). The fix is evs:DisassociateEipFromVlan
    (using the real associationId from evs:ListEnvironmentVlans's hcx VLAN
    entry) BEFORE the EC2-level disassociate/release. Confirmed live: every
    HCX EIP that ec2:ReleaseAddress alone left "phantom"/stuck released
    immediately once evs:DisassociateEipFromVlan was called first.
    """
    ec2 = session.client("ec2")
    tag = _hcx_tag(aws_config_stack)
    if not tag:
        logger.info("  HCX EIP release skipped — no stack scope known "
                    "(refusing to match HCX EIPs account-wide)")
        return
    # Pull the real EVS-side association ids for the hcx VLAN, if we can
    # reach the environment. Keyed by allocationId so the EC2-address loop
    # below can look up the matching evs-side associationId.
    evs_assoc_by_alloc: dict[str, str] = {}
    if environment_id:
        try:
            client = evs_client or session.client("evs")
            vlans = client.list_environment_vlans(environmentId=environment_id).get(
                "environmentVlans", [])
            for v in vlans:
                if v.get("functionName") == "hcx":
                    for a in v.get("eipAssociations", []) or []:
                        evs_assoc_by_alloc[a["allocationId"]] = a["associationId"]
        except ClientError as e:
            logger.warning("  HCX: could not list environment VLANs (%s); "
                           "falling back to EC2-only disassociate/release",
                           _aws_error_code(e))
    try:
        addrs = ec2.describe_addresses().get("Addresses", [])
    except ClientError as e:
        logger.warning("  HCX: could not list EIPs: %s", e)
        return
    hcx = [a for a in addrs
           if (n := next((t["Value"] for t in a.get("Tags", []) if t["Key"] == "Name"), ""))
           and n.startswith("EVS-HCX-") and (tag is None or n.startswith(tag))]
    if not hcx:
        return
    logger.info("Releasing %d HCX public EIP(s) before environment deletion...", len(hcx))
    for a in hcx:
        ip = a.get("PublicIp", "?")
        evs_assoc_id = evs_assoc_by_alloc.get(a["AllocationId"])
        if evs_assoc_id and environment_id:
            try:
                (evs_client or session.client("evs")).disassociate_eip_from_vlan(
                    environmentId=environment_id, vlanName="hcx",
                    associationId=evs_assoc_id,
                )
                logger.info("  HCX EIP %s: disassociated from the hcx VLAN via EVS", ip)
            except ClientError as e:
                logger.warning("  HCX EIP %s: evs:DisassociateEipFromVlan failed (%s); "
                               "trying EC2-level release anyway",
                               ip, _aws_error_code(e))
        try:
            if a.get("AssociationId"):
                try:
                    ec2.disassociate_address(AssociationId=a["AssociationId"])
                except ClientError as e:
                    logger.warning("  HCX EIP %s: disassociate failed (%s); trying release",
                                   ip, _aws_error_code(e))
            # evs:DisassociateEipFromVlan is ASYNCHRONOUS: it returns before EVS
            # has finished detaching the address, so an immediate
            # ec2:ReleaseAddress loses the race with InvalidIPAddress.InUse and
            # the EIP leaks (which also blocks the IPAM /28 reclaim, and the
            # /28 pool cap is what limits you to 2 HCX environments per region).
            # Observed live: of 3 HCX EIPs, only the one carrying the EVS-side
            # VLAN association failed this way -- the two with no association
            # released first try. So retry with backoff instead of warning and
            # moving on.
            released = False
            last_err = None
            for attempt in range(1, _EIP_RELEASE_ATTEMPTS + 1):
                try:
                    ec2.release_address(AllocationId=a["AllocationId"])
                    logger.info("  Released HCX EIP %s%s", ip,
                                f" (attempt {attempt})" if attempt > 1 else "")
                    released = True
                    break
                except ClientError as e:
                    last_err = e
                    code = _aws_error_code(e)
                    # InUse = EVS still detaching; AuthFailure = eventual-consistency
                    # blip AWS returns for an address mid-transition.
                    if code not in ("InvalidIPAddress.InUse", "AuthFailure"):
                        raise
                    if attempt < _EIP_RELEASE_ATTEMPTS:
                        delay = min(2 ** attempt, 30)
                        logger.info("  HCX EIP %s still held (%s); retrying in %ds "
                                    "(attempt %d/%d)",
                                    ip, code, delay, attempt, _EIP_RELEASE_ATTEMPTS)
                        time.sleep(delay)
            if not released:
                logger.warning("  HCX EIP %s NOT released after %d attempts (%s) — "
                               "still held by EVS/HCX; release it after the "
                               "environment is fully deleted, then re-run to "
                               "reclaim the IPAM block",
                               ip, _EIP_RELEASE_ATTEMPTS, _aws_error_code(last_err))
        except ClientError as e:
            logger.warning("  HCX EIP %s NOT released (%s) — likely held by EVS/HCX; "
                           "release it after the environment is fully deleted, then "
                           "re-run to reclaim the IPAM block", ip, _aws_error_code(e))


def deprovision_hcx_ipam(session, vpc_id: str | None, aws_config_stack: str | None) -> None:
    """Return the HCX IPAM /28 to AWS and delete the pool (auto-IPAM mode only).

    BYO mode (customer-supplied hcx.public_cidr) creates no orchestrator IPAM
    pool, so nothing matches here and this is a clean no-op — the EIP release
    above is all BYO needs. Auto mode: disassociate the /28 from the VPC,
    deprovision it from the pool, delete the pool. Amazon-provided contiguous
    public blocks are capped at 2/region, so skipping this leaks one per run.
    """
    ec2 = session.client("ec2")
    tag = _hcx_tag(aws_config_stack)
    if not tag:
        logger.info("  HCX IPAM reclaim skipped — no stack scope known "
                    "(refusing to match HCX pools account-wide)")
        return
    try:
        pools = []
        for page in ec2.get_paginator("describe_ipam_pools").paginate():
            for p in page.get("IpamPools", []):
                n = next((t["Value"] for t in p.get("Tags", []) if t["Key"] == "Name"), "")
                if n.startswith("EVS-HCX-") and (tag is None or n == tag):
                    pools.append(p)
    except ClientError as e:
        logger.warning("  HCX: could not list IPAM pools: %s", e)
        return
    if not pools:
        return  # BYO public_cidr, or already cleaned — nothing to do
    vpc_assocs = {}
    if vpc_id:
        try:
            v = ec2.describe_vpcs(VpcIds=[vpc_id])["Vpcs"][0]
            for a in v.get("CidrBlockAssociationSet", []):
                if a.get("CidrBlockState", {}).get("State") in ("associated", "associating"):
                    vpc_assocs[a["CidrBlock"]] = a["AssociationId"]
        except ClientError:
            pass
    logger.info("Reclaiming %d HCX IPAM pool(s)...", len(pools))
    for p in pools:
        pool_id = p["IpamPoolId"]
        try:
            cidrs = [c["Cidr"] for c in ec2.get_ipam_pool_cidrs(IpamPoolId=pool_id)
                     .get("IpamPoolCidrs", []) if c.get("State") == "provisioned"]
        except ClientError as e:
            logger.warning("  HCX pool %s: could not list CIDRs: %s", pool_id, e)
            cidrs = []
        for cidr in cidrs:
            if cidr in vpc_assocs:
                try:
                    ec2.disassociate_vpc_cidr_block(AssociationId=vpc_assocs[cidr])
                    logger.info("  Disassociated HCX CIDR %s from VPC", cidr)
                    time.sleep(5)
                except ClientError as e:
                    logger.warning("  Could not disassociate HCX CIDR %s (%s)",
                                   cidr, _aws_error_code(e))
            try:
                ec2.deprovision_ipam_pool_cidr(IpamPoolId=pool_id, Cidr=cidr)
                logger.info("  Deprovisioning HCX CIDR %s...", cidr)
                for _ in range(12):
                    time.sleep(10)
                    left = ec2.get_ipam_pool_cidrs(IpamPoolId=pool_id).get("IpamPoolCidrs", [])
                    if all(x.get("Cidr") != cidr or x.get("State") == "deprovisioned" for x in left):
                        break
            except ClientError as e:
                logger.warning("  Could not deprovision HCX CIDR %s (%s) — a stuck EIP "
                               "allocation may still hold it; free it and re-run", cidr,
                               _aws_error_code(e))
        try:
            ec2.delete_ipam_pool(IpamPoolId=pool_id)
            logger.info("  Deleted HCX IPAM pool %s", pool_id)
        except ClientError as e:
            logger.warning("  Could not delete HCX IPAM pool %s (%s) — still has an "
                           "allocation (stuck EIP); it frees once that EIP is released",
                           pool_id, _aws_error_code(e))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Destroy an EVS VCF environment and associated resources",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Teardown levels:
  (default)       Environment only — delete hosts, secrets, connectors, env.
                  Leaves infrastructure (VPC, runner, landing zone) for reuse.

  --include-infra Also delete the landing zone stack (NAT, Route Server, SG,
                  key pair). Runner and VPC remain.

  --all           Full nuke — environment + landing zone + bootstrap stack.
                  Removes VPC (if stack-created) and runner. In BYO-VPC mode,
                  only resources created by this stack inside the VPC will be
                  removed — nothing pre-existing is touched.

Examples:
  python3 destroy.py --bootstrap-stack evs-bootstrap-myenv --region us-east-2
  python3 destroy.py --bootstrap-stack evs-bootstrap-myenv --region us-east-2 --include-infra
  python3 destroy.py --bootstrap-stack evs-bootstrap-myenv --region us-east-2 --all -y
        """,
    )
    parser.add_argument("--bootstrap-stack",
                        help="Name of the bootstrap CFN stack. Used to auto-discover VPC, environment, and landing zone.")
    parser.add_argument("--environment-id",
                        help="EVS environment ID (e.g. env-abc123). Overrides auto-discovery.")
    parser.add_argument("--region", required=True, help="AWS region (e.g. us-east-2)")
    parser.add_argument("--profile", help="AWS profile name (optional)")
    parser.add_argument(
        "--endpoint-url",
        help="Optional EVS endpoint override. Must match the "
             "endpoint the environment was created against — an environment "
             "created against a custom endpoint does not exist on the default endpoint.")
    parser.add_argument(
        "--include-infra", action="store_true",
        help="Also delete the landing zone stack (NAT, Route Server, SG, key pair)",
    )
    parser.add_argument(
        "--all", action="store_true",
        help="Full nuke: environment + landing zone + bootstrap stack",
    )
    parser.add_argument(
        "--skip-hosts", action="store_true",
        help="Skip host deletion (if already deleted)",
    )
    parser.add_argument(
        "--yes", "-y", action="store_true",
        help="Skip confirmation prompt",
    )

    args = parser.parse_args()

    if not args.bootstrap_stack and not args.environment_id:
        parser.error("Provide at least one of: --bootstrap-stack, --environment-id")

    if (args.all or args.include_infra) and not args.bootstrap_stack:
        parser.error("--all and --include-infra require --bootstrap-stack")

    # --all implies --include-infra
    if args.all:
        args.include_infra = True

    # Build session
    session_kwargs = {"region_name": args.region}
    if args.profile:
        session_kwargs["profile_name"] = args.profile
    session = boto3.Session(**session_kwargs)

    # Auto-discover from stack (if provided)
    info = {
        "vpc_id": None,
        "environment_id": args.environment_id,  # CLI override takes priority
        "aws_config_stack": None,
        "stack_created_vpc": False,
    }
    if args.bootstrap_stack:
        logger.info("Discovering resources from stack: %s", args.bootstrap_stack)
        info = discover_from_stack(session, args.bootstrap_stack,
                                   evs_endpoint_url=args.endpoint_url,
                                   cli_environment_id=args.environment_id)
        # CLI --environment-id overrides auto-discovery
        if args.environment_id:
            info["environment_id"] = args.environment_id

    # Confirmation
    if not args.yes:
        stack_label = args.bootstrap_stack or "(no stack specified)"
        print(f"\n⚠️  DESTROY — region: {args.region}")
        if args.bootstrap_stack:
            print(f"   Stack: {stack_label}")
        if info["environment_id"]:
            print(f"   Environment: {info['environment_id']}")
        if info["vpc_id"]:
            print(f"   VPC: {info['vpc_id']}")
        level = "FULL NUKE (env + infra + stack)" if args.all else \
                "env + landing zone" if args.include_infra else \
                "environment only (infra preserved)"
        print(f"   Level: {level}")
        if args.all and info["stack_created_vpc"]:
            print(f"   ⚠️  VPC {info['vpc_id']} will be DELETED (created by this stack)")
        response = input("\n   Type 'destroy' to confirm: ")
        if response.strip().lower() != "destroy":
            print("Aborted.")
            sys.exit(0)

    # Execute destroy sequence
    start = time.time()

    # --- Level 1: Environment teardown ---
    if info["environment_id"]:
        evs_client = (
            session.client("evs", endpoint_url=args.endpoint_url)
            if args.endpoint_url else session.client("evs")
        )
        ec2_client = session.client("ec2")
        sm_client = session.client("secretsmanager")

        if not args.skip_hosts:
            delete_hosts(evs_client, info["environment_id"])
        else:
            logger.info("[1/5] Skipping host deletion (--skip-hosts)")

        delete_ebs_volumes(ec2_client, info["environment_id"])
        delete_connectors(evs_client, info["environment_id"])
        # Release HCX public EIPs first — EVS rejects DeleteEnvironment while the
        # public subnet still has EIP associations (HI-4). No-op if not HCX.
        release_hcx_eips(session, info.get("aws_config_stack"),
                         environment_id=info["environment_id"], evs_client=evs_client)
        # Delete the environment BEFORE its secrets: secrets are force-deleted
        # with no recovery window, so a failed/timed-out env delete afterward
        # would strand a live, billing environment with unrecoverable credentials.
        # Env-first keeps failure recoverable; discard secrets only once
        # delete_environment's return value CONFIRMS deletion — a timeout must not.
        environment_confirmed_deleted = delete_environment(evs_client, info["environment_id"])
        if environment_confirmed_deleted:
            delete_secrets(sm_client, info["environment_id"])
        else:
            logger.warning("  Skipping secret deletion — environment deletion was not "
                           "confirmed, so its credentials may still be needed. Re-run "
                           "this script once the environment shows DELETED in the EVS "
                           "console to clean up its secrets.")
        # Reclaim the HCX IPAM /28 + pool now the environment is gone (HI-4).
        # No-op for BYO-public-cidr or non-HCX deployments.
        deprovision_hcx_ipam(session, info["vpc_id"], info.get("aws_config_stack"))
    else:
        logger.info("No environment found in VPC — skipping environment teardown")

    # --- Level 2: Landing zone stack ---
    if args.include_infra and info["aws_config_stack"]:
        logger.info("Deleting landing zone infrastructure...")
        delete_cfn_stack(session, info["aws_config_stack"], vpc_id=info["vpc_id"],
                          stack_created_vpc=info["stack_created_vpc"])
    elif args.include_infra:
        logger.info("No landing zone stack found — skipping")

    # --- Level 3: Bootstrap stack ---
    if args.all:
        logger.info("Deleting bootstrap stack...")
        delete_cfn_stack(session, args.bootstrap_stack, vpc_id=info["vpc_id"],
                          stack_created_vpc=info["stack_created_vpc"])

    # --- Cross-check before declaring success (LOG ONLY, deletes nothing) ---
    # Any lingering resource found here is logged at WARNING, which
    # _WarningCounter records — turning the run into a flagged partial
    # result with a non-zero exit instead of "DESTROY COMPLETE".
    check_lingering_resources(session, info["environment_id"], info["vpc_id"])

    elapsed = time.time() - start
    logger.info("=" * 60)
    if _warning_log.messages:
        logger.info(
            "DESTROY FINISHED WITH %d WARNING(S) (%.0f min %.0f sec)",
            len(_warning_log.messages), elapsed // 60, elapsed % 60,
        )
        logger.info("")
        logger.info("Resources may remain and may still be billing. Warnings:")
        for msg in _warning_log.messages:
            logger.info("  - %s", msg)
        logger.info("")
        logger.info("Verify in the CloudFormation and EC2 consoles, then re-run")
        logger.info("this script with the same arguments to retry.")
        logger.info("=" * 60)
        sys.exit(1)

    logger.info("DESTROY COMPLETE (%.0f min %.0f sec)", elapsed // 60, elapsed % 60)
    logger.info("=" * 60)


if __name__ == "__main__":
    main()
