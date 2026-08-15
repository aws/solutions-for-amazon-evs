# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Entry point for the EVS environment deployment tool."""

import argparse
import json
import logging
import os
import sys
import tempfile
from pathlib import Path

from botocore.exceptions import ClientError

from aws_client import AWSClient
from ebs_volume_manager import EbsVolumeManager
from edge_cluster_spec import EdgeClusterSpec
from evs_manager import EVSManager
from phase2_sync import Phase2Sync
from phase3_sync import Phase3Sync
from host_thumbprints import fetch_all_host_thumbprints
from vcf_password_provisioner import ensure_vcf_passwords
from vlan_route_table_associator import VlanRouteTableAssociator

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
)
logger = logging.getLogger(__name__)


def _resolve_output_paths() -> tuple[Path, Path, Path]:
    """Resolve Phase 2 state path, Phase 3 bringup spec path, and Phase 3
    edge cluster spec path.

    Environment variables take precedence; fall back to repo-relative paths
    computed from this source file's location.
    """
    env2 = os.environ.get("PHASE2_STATE_PATH") or os.environ.get(
        "PHASE2_TFVARS_PATH"  # legacy env var, kept for backward compat
    )
    env3 = os.environ.get("PHASE3_BRINGUP_SPEC_PATH")
    env3_edge = os.environ.get("PHASE3_EDGE_CLUSTER_SPEC_PATH")

    if env2 and env3 and env3_edge:
        return Path(env2), Path(env3), Path(env3_edge)

    # Only compute repo-root fallback when at least one env var is missing.
    orchestrator_dir = Path(__file__).resolve().parents[1]
    default2 = orchestrator_dir / "evs_environment" / "state.json"
    default3 = orchestrator_dir / "vcf_deployment" / "bringup_spec.json"
    default3_edge = orchestrator_dir / "vcf_deployment" / "edge_cluster_spec.json"
    return (
        Path(env2) if env2 else default2,
        Path(env3) if env3 else default3,
        Path(env3_edge) if env3_edge else default3_edge,
    )


PHASE2_STATE_PATH, PHASE3_BRINGUP_SPEC_PATH, PHASE3_EDGE_CLUSTER_SPEC_PATH = _resolve_output_paths()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description="EVS environment deployment tool",
    )
    parser.add_argument(
        "action",
        choices=[
            "create-environment",
            "create-hosts",
            "create-environment-and-hosts",
            "post-evs-sync-config",
            "associate-vlan-subnets",
            "associate-hcx-eip",
            "create-and-attach-ebs",
        ],
        help="Action to perform",
    )
    parser.add_argument(
        "--profile",
        default=None,
        help="AWS credentials profile name",
    )
    parser.add_argument(
        "--role-arn",
        default=None,
        help="IAM role ARN to assume",
    )
    parser.add_argument(
        "--config",
        required=True,
        help="Path to the EVS environment configuration JSON file",
    )
    parser.add_argument(
        "--instance-type",
        choices=["i4i.metal", "i7i.metal-24xl"],
        default=None,
        help="EC2 instance type for EVS hosts (required for create-hosts and create-environment-and-hosts)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Show what would be sent to the API without executing",
    )
    return parser.parse_args(argv)


def load_config(config_path: str) -> dict:
    """Load and return the EVS environment configuration from a JSON file."""
    path = Path(config_path)
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")

    with open(path) as f:
        config = json.load(f)

    logger.info("Loaded environment config from: %s", path)
    return config


def _to_evs_vcf_hostnames(vcf_hostnames: dict | None) -> dict | None:
    """Translate our canonical vcfHostnames dict into the EVS API schema.

    Our Phase 1 outputs use snake_case canonical keys (plus extras for Phase 3
    like vcf_ops*, vcf_fleet) because they feed both the EVS API and the VCF
    bringup module. The EVS API expects a specific camelCase subset, so we
    map one into the other here. Keys absent from the API schema are dropped.
    """
    if not vcf_hostnames:
        return vcf_hostnames

    key_map = {
        "vcenter": "vCenter",
        "nsx": "nsx",
        "nsx01": "nsxManager1",
        "nsx02": "nsxManager2",
        "nsx03": "nsxManager3",
        "edge01": "nsxEdge1",
        "edge02": "nsxEdge2",
        "sddc_manager": "sddcManager",
        "cloud_builder": "cloudBuilder",
    }

    translated: dict[str, str] = {}
    for src_key, value in vcf_hostnames.items():
        dst_key = key_map.get(src_key)
        if dst_key is not None:
            translated[dst_key] = value

    return translated


def _atomic_write_json(path: str, data: dict) -> None:
    """Write JSON atomically (temp file + os.replace) so a crash mid-write
    can't leave a truncated file the next run fails to parse. Mirrors the
    top-level orchestrator's Checkpoint._save.
    """
    directory = os.path.dirname(os.path.abspath(path)) or "."
    tmp_fd, tmp_path = tempfile.mkstemp(
        dir=directory, prefix=f".{os.path.basename(path)}.", suffix=".tmp",
    )
    try:
        with os.fdopen(tmp_fd, "w") as f:
            json.dump(data, f, indent=2)
            f.write("\n")
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def run_create_environment(evs: EVSManager, config: dict, dry_run: bool, config_path: str) -> int:
    """Handle the create-environment action."""
    # vcfHostnames is only accepted when EVS is deploying VCF. For
    # SELF_DEPLOYED the server rejects the parameter.
    vcf_hostnames = (
        _to_evs_vcf_hostnames(config.get("vcfHostnames"))
        if config.get("evsEnvVersion") != "SELF_DEPLOYED"
        else None
    )

    # Build initialVlans — strip any non-API keys (like networkAclId)
    # that config_sync may have cached from older runs.
    initial_vlans = {
        k: {"cidr": v["cidr"]} if isinstance(v, dict) and "cidr" in v else v
        for k, v in config["initialVlans"].items()
    }

    params = {
        "vpc_id": config["vpcId"],
        "service_access_subnet_id": config["serviceAccessSubnetId"],
        "vcf_version": config["evsEnvVersion"],
        "initial_vlans": initial_vlans,
        "terms_accepted": config.get("termsAccepted", True),
        "environment_name": config.get("environmentName"),
        "connectivity_info": config.get("connectivityInfo"),
        "license_info": config.get("licenseInfo"),
        "hosts": config.get("hosts"),
        "vcf_hostnames": vcf_hostnames,
        "site_id": config.get("siteId"),
        "service_access_security_groups": config.get("serviceAccessSecurityGroups"),
        "tags": config.get("tags"),
        # clientToken not passed — EVS does not support it currently and
        # passing it causes a spurious "Premium Support" rejection.
    }
    # Drop None values so the API doesn't receive optional fields that
    # the service model marks as required at the botocore layer.
    params = {k: v for k, v in params.items() if v is not None}

    if dry_run:
        logger.info("DRY RUN — would call create_environment with:")
        safe_params = {**params}
        if safe_params.get("license_info") is not None:
            safe_params["license_info"] = "***REDACTED***"
        print(json.dumps(safe_params, indent=2, default=str))
        return 0

    # Idempotency guard: if config carries an environmentId, confirm it's
    # still LIVE before creating another — re-creating strands a duplicate,
    # continuously billing environment (EVS has no clientToken dedupe). Rules:
    #   - live (CREATING/CREATED)                         -> skip, resume
    #   - not found / DELETING / DELETED / CREATE_FAILED  -> stale id, create
    #   - any other error (AccessDenied/throttle/transient) -> raise; never
    #     blindly skip (would no-op) or blindly create (would duplicate)
    existing_id = config.get("environmentId")
    if existing_id:
        try:
            existing = evs.get_environment(existing_id).get("environment", {})
            state = existing.get("environmentState")
            if state in ("CREATING", "CREATED"):
                logger.info(
                    "Environment %s already exists (state=%s) — skipping "
                    "create_environment (idempotent resume)",
                    existing_id, state,
                )
                return 0
            logger.warning(
                "Stored environmentId %s is in state %s (not live) — "
                "creating a new environment", existing_id, state,
            )
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") == "ResourceNotFoundException":
                logger.warning(
                    "Stored environmentId %s no longer exists — creating a "
                    "new environment", existing_id,
                )
            else:
                # Cannot confirm the environment's state — do NOT skip (would
                # silently no-op) and do NOT blindly create (would risk a
                # duplicate billing environment). Fail loud instead.
                logger.error(
                    "Could not verify existing environment %s before create: %s",
                    existing_id, e,
                )
                raise

    response = evs.create_environment(**params)
    env = response.get("environment", {})
    env_id = env.get("environmentId")
    logger.info("Environment ID: %s", env_id)
    logger.info("Environment State: %s", env.get("environmentState"))

    # Write the environment ID back to config.json
    if env_id:
        config["environmentId"] = env_id
        _atomic_write_json(config_path, config)
        logger.info("Wrote environmentId to %s", config_path)

    return 0


def run_create_hosts(evs: EVSManager, config: dict, dry_run: bool, config_path: str, instance_type: str | None = None) -> int:
    """Handle the create-hosts action."""
    if not instance_type:
        logger.error("--instance-type is required for create-hosts")
        return 1

    environment_id = config.get("environmentId")
    if not environment_id:
        logger.error("'environmentId' is required in config for create-hosts")
        return 1

    additional_hosts = config.get("additionalHosts", [])
    if not additional_hosts:
        logger.error("'additionalHosts' list is required in config for create-hosts")
        return 1

    # Override instance type on every host with the CLI-provided value
    additional_hosts = [
        {**host, "instanceType": instance_type} for host in additional_hosts
    ]
    logger.info("Using instance type '%s' for all hosts", instance_type)

    esx_version = config.get("esxVersion")
    auto_resolved = False
    if not esx_version:
        target = config.get("vcfInstallerProductVersion") or ""
        if not target:
            logger.error(
                "'esxVersion' and 'vcfInstallerProductVersion' are both empty in "
                "config.json. Set at least one so the ESXi version can be determined."
            )
            return 1
        esx_version = evs.get_latest_esx_version(target, instance_type)
        auto_resolved = True

    if dry_run:
        payload = {
            "environmentId": environment_id,
            "hosts": additional_hosts,
            "esxVersion": esx_version,
        }
        logger.info("DRY RUN — would call create_environment_host for %d host(s):", len(additional_hosts))
        print(json.dumps(payload, indent=2, default=str))
        return 0

    # Persist an auto-resolved esxVersion before creating any hosts. Otherwise
    # a host created later (capacity retry, or a resume re-adding a missing
    # host) re-resolves "latest" against a catalog that may have a newer patch
    # build — a mixed-ESXi cluster that breaks VCF host commissioning.
    if auto_resolved:
        config["esxVersion"] = esx_version
        _atomic_write_json(config_path, config)
        logger.info("Persisted auto-resolved esxVersion to %s: %s", config_path, esx_version)

    # Idempotency (resume-safety): skip hosts already in CREATING/CREATED so a
    # re-run doesn't re-submit them — EVS rejects a duplicate hostName and
    # would abort the whole run. FAILED/absent hosts still need (re)creation.
    # Best-effort: if the list call fails, fall back to creating all hosts.
    try:
        existing_hosts = evs.list_environment_hosts(environment_id)
    except Exception as e:  # noqa: BLE001
        logger.warning(
            "Could not list existing hosts for the idempotency check (%s); "
            "proceeding to create all configured hosts", e,
        )
        existing_hosts = []
    live_names = {
        h.get("hostName") for h in existing_hosts
        if h.get("hostState") in ("CREATING", "CREATED")
    }
    if live_names:
        before = len(additional_hosts)
        additional_hosts = [
            h for h in additional_hosts if h.get("hostName") not in live_names
        ]
        skipped = before - len(additional_hosts)
        if skipped:
            logger.info(
                "Skipping %d host(s) already present in CREATING/CREATED: %s",
                skipped, sorted(live_names),
            )
    if not additional_hosts:
        logger.info("All configured hosts already exist — nothing to create.")
        return 0

    responses = evs.create_environment_hosts(
        environment_id=environment_id,
        hosts=additional_hosts,
        esx_version=esx_version,
    )

    for resp in responses:
        host = resp.get("host", {})
        logger.info(
            "Host: %s — State: %s",
            host.get("hostName", "unknown"),
            host.get("hostState", "unknown"),
        )

    return 0


def run_create_environment_and_hosts(
    evs: EVSManager,
    config: dict,
    dry_run: bool,
    config_path: str,
    instance_type: str | None,
) -> int:
    """One-shot wrapper that runs all three steps in order:

      1. ``create-environment`` — POSTs CreateEnvironment, writes the
         returned ``environmentId`` back into ``config.json``.
      2. Polls ``get_environment`` every 60s until the environment is
         in ``CREATED`` state (typical: 5-10 minutes).
      3. ``create-hosts`` — provisions the EVS hosts.

    Needs ``--instance-type`` for step 3. ``--dry-run`` previews each step's
    API call and skips the waits. The three individual actions remain
    available for operators who want to inspect state between steps.
    """
    if not instance_type:
        logger.error(
            "--instance-type is required for create-environment-and-hosts"
        )
        return 1

    if dry_run:
        # Just preview both API calls back-to-back; skip the wait since no
        # real environment exists in dry-run mode.
        logger.info("DRY RUN — create-environment-and-hosts step 1/3")
        rc = run_create_environment(evs, config, dry_run=True, config_path=config_path)
        if rc != 0:
            return rc
        logger.info(
            "DRY RUN — step 2/3 wait_for_environment_state would poll until CREATED"
        )
        logger.info("DRY RUN — create-environment-and-hosts step 3/3")
        return run_create_hosts(
            evs, config, dry_run=True,
            config_path=config_path, instance_type=instance_type,
        )

    # Step 1: create-environment.
    logger.info("create-environment-and-hosts 1/3: create-environment")
    rc = run_create_environment(evs, config, dry_run=False, config_path=config_path)
    if rc != 0:
        return rc

    # run_create_environment wrote environmentId to config.json; re-read so
    # step 3 sees it (disk is the authoritative source).
    config = load_config(config_path)
    env_id = config.get("environmentId")
    if not env_id:
        logger.error(
            "create-environment didn't write an environmentId — can't "
            "continue to create-hosts."
        )
        return 1

    # Step 2: poll until CREATED.
    logger.info(
        "create-environment-and-hosts 2/3: waiting for environment %s "
        "to reach CREATED (typical: 5-10 minutes)",
        env_id,
    )
    try:
        evs.wait_for_environment_state(env_id, desired_state="CREATED")
    except (RuntimeError, TimeoutError) as e:
        logger.error("Environment readiness check failed: %s", e)
        return 1

    # Step 3: create-hosts.
    logger.info("create-environment-and-hosts 3/3: create-hosts")
    return run_create_hosts(
        evs, config, dry_run=False,
        config_path=config_path, instance_type=instance_type,
    )


def run_post_evs_sync_config(
    aws: AWSClient,
    args: argparse.Namespace,
    config: dict,
) -> int:
    """Handle the post-evs-sync-config action.

    Looks up the EVS host instance for EBS attach, queries EVS for VLAN
    subnets, fetches per-host ESXi passwords from Secrets Manager, and
    writes Phase 2 tfvars + Phase 3 spec files with env-ID-derived names.
    """
    env_id = config.get("environmentId")
    if not env_id:
        logger.error("'environmentId' is required in config for post-evs-sync-config")
        return 1

    hosts = config.get("additionalHosts") or []
    if len(hosts) > 3 and config.get("simpleDeployment", True):
        logger.warning(
            "%d hosts detected with simpleDeployment=true. For 4+ hosts the "
            "recommended setting is simpleDeployment=false (deploys 3 NSX Manager "
            "nodes and an HA Operations cluster). Set simpleDeployment to false in "
            "your config to enable HA deployment.", len(hosts),
        )

    evs = EVSManager(aws)

    # Phase 2: EBS-volume tfvars + VLAN subnet IDs + route table ID
    phase2 = Phase2Sync(
        aws_client=aws,
        evs=evs,
        environment_id=env_id,
        output_path=PHASE2_STATE_PATH,
        config=config,
    )
    phase2_result = phase2.sync(dry_run=args.dry_run)

    # Carry the EVS host's instance type forward into Phase 3 so the
    # bringup spec can set clusterSpec.clusterEvcMode correctly.
    if phase2_result.get("instance_type"):
        config["evsInstanceType"] = phase2_result["instance_type"]

    # Fail fast if any per-host ESXi root secret is missing. Phase 3's bringup
    # resolves ``__SECRET:esxi:<host>__`` against ``evs!<env>_<host>``; without
    # those the POST blows up at host commissioning. EVS creates them per-host,
    # so a miss usually means a host failed to land.
    missing_esxi = phase2_result.get("missing_esxi_secrets") or []
    if missing_esxi and not args.dry_run:
        logger.error(
            "Missing ESXi root password secret(s) in AWS Secrets Manager "
            "for env %s: %s. EVS provisions these per-host when each "
            "host lands; check ListEnvironmentHosts for failed hosts.",
            env_id, ", ".join(sorted(missing_esxi)),
        )
        return 1

    # Provision the VCF appliance passwords as Secrets Manager secrets.
    # Idempotent: existing secrets are left alone. Phase 3's secret_resolver
    # fetches these at runtime via the placeholders in bringup_spec.json /
    # edge_cluster_spec.json.
    if not args.dry_run:
        sm_client = aws.client("secretsmanager")
        ensure_vcf_passwords(sm_client, env_id, config)
    else:
        from vcf_password_provisioner import required_secret_roles
        logger.info(
            "DRY RUN — would provision VCF appliance secrets for roles: %s",
            ", ".join(required_secret_roles(config)),
        )

    # Phase 3: VCF bringup spec (now with env-id-derived names)
    #
    # Best-effort SSL thumbprint pre-fetch — if hosts are reachable,
    # pre-populates esxiSslThumbprints so Phase 3 can skip its own
    # fetch. Non-fatal; Phase 3 retries independently if this fails.
    if not args.dry_run:
        try:
            thumbprints = fetch_all_host_thumbprints(config)
            if thumbprints:
                config["esxiSslThumbprints"] = thumbprints
                logger.info(
                    "Pre-fetched SSL thumbprint(s) for %d host(s)",
                    len(thumbprints),
                )
        except Exception as e:  # noqa: BLE001
            logger.warning(
                "Could not pre-fetch ESXi SSL thumbprints (%s) — Phase 3 "
                "will fetch them itself at bringup time (or fall back to "
                "a placeholder if hosts aren't reachable from there "
                "either)", e,
            )

    phase3 = Phase3Sync(
        output_path=PHASE3_BRINGUP_SPEC_PATH,
        config=config,
        environment_id=env_id,
    )
    phase3.sync(dry_run=args.dry_run)

    # Phase 3: Edge cluster spec (now with env-id-derived names; clusterId
    # remains a placeholder that start-edge-cluster resolves at run time)
    edge_spec = EdgeClusterSpec(
        output_path=PHASE3_EDGE_CLUSTER_SPEC_PATH,
        config=config,
        environment_id=env_id,
    )
    edge_spec.sync(dry_run=args.dry_run)
    return 0


def _load_phase2_tfvars() -> dict:
    """Read Phase 2's tfvars.json (the file Phase2Sync writes).

    Used by the standalone ``associate-vlan-subnets`` and
    ``create-and-attach-ebs`` actions so they can run after a prior
    ``post-evs-sync-config`` without re-running the full chain. The chained
    path passes the same data in-memory and skips this read.
    """
    if not PHASE2_STATE_PATH.exists():
        raise FileNotFoundError(
            f"{PHASE2_STATE_PATH} not found — run post-evs-sync-config "
            f"first to populate it."
        )
    with open(PHASE2_STATE_PATH) as f:
        return json.load(f)


def run_associate_vlan_subnets(
    aws: AWSClient,
    args: argparse.Namespace,
    *,
    tfvars: dict | None = None,
) -> int:
    """Associate every EVS VLAN subnet with the service-access route table.

    Replaces the ``vlan_route_associations`` Terraform module. Idempotent:
    subnets already associated are skipped. Tolerates the brief window
    after EVS reports a subnet ``CREATED`` but AWS-side propagation
    hasn't surfaced it to ``DescribeRouteTables`` yet — retries with
    exponential backoff before giving up.

    Args:
        aws: AWSClient.
        args: argparse namespace (we use ``dry_run``).
        tfvars: Optional pre-loaded tfvars dict. The chained
            ``deploy-environment`` path passes this in-memory; standalone
            invocations read it from disk via ``_load_phase2_tfvars``.
    """
    if tfvars is None:
        tfvars = _load_phase2_tfvars()

    route_table_id = tfvars.get("service_access_route_table_id") or ""
    subnet_ids = tfvars.get("evs_vlan_subnet_ids") or []

    if not route_table_id:
        logger.error(
            "service_access_route_table_id missing from %s — ensure the "
            "bootstrap stack provided it and config.json is complete.",
            PHASE2_STATE_PATH,
        )
        return 1
    if not subnet_ids:
        logger.error(
            "evs_vlan_subnet_ids missing/empty in %s — "
            "did post-evs-sync-config find any VLANs?",
            PHASE2_STATE_PATH,
        )
        return 1

    associator = VlanRouteTableAssociator(aws.client("ec2"))
    associations = associator.associate_subnets(
        route_table_id=route_table_id,
        subnet_ids=subnet_ids,
        dry_run=args.dry_run,
    )
    if args.dry_run:
        logger.info(
            "DRY RUN — would associate %d subnet(s) with %s",
            len(subnet_ids), route_table_id,
        )
    else:
        logger.info(
            "Route table %s now associated with %d subnet(s)",
            route_table_id, len(associations),
        )
    return 0


def run_associate_hcx_eip(
    evs: EVSManager,
    config: dict,
    dry_run: bool,
) -> int:
    """Associate every pre-allocated EIP with the HCX VLAN.

    Only runs when ``hcxPublic`` is true. HCX needs one public address per
    appliance -- HCX Manager and HCX Interconnect (HCX-IX) at minimum, plus one
    per Network Extension appliance -- and AWS documents "You associate each
    Elastic IP that you want to use with an HCX appliance to the HCX VLAN
    subnet". Associating only the first would leave HCX-IX without a public IP,
    so internet-based migration could not work.

    Reads ``hcxEipAllocationIds`` (all allocated EIPs) and falls back to the
    singular ``hcxEipAllocationId`` for a config written by an older run.

    Idempotent: EIPs already present in the VLAN's ``eipAssociations`` are
    skipped, so a ``--resume`` does not re-associate or error.
    """
    if not config.get("hcxPublic"):
        logger.info("HCX public not enabled; skipping EIP association")
        return 0

    alloc_ids = list(config.get("hcxEipAllocationIds") or [])
    if not alloc_ids:
        single = config.get("hcxEipAllocationId")
        if single:
            alloc_ids = [single]
    if not alloc_ids:
        logger.error(
            "hcxPublic is true but no HCX EIP allocation ids are present in "
            "config (expected hcxEipAllocationIds, or hcxEipAllocationId). "
            "Ensure the aws_config stage provisioned the HCX public EIPs."
        )
        return 1

    env_id = config.get("environmentId")
    if not env_id:
        logger.error(
            "'environmentId' is required in config for associate-hcx-eip"
        )
        return 1

    if dry_run:
        logger.info(
            "DRY RUN — would associate %d EIP(s) %s with HCX VLAN 'hcx' "
            "in environment %s",
            len(alloc_ids), alloc_ids, env_id,
        )
        return 0

    # Skip any EIP the VLAN already carries, so --resume is a no-op.
    already: set[str] = set()
    try:
        for vlan in evs.list_environment_vlans(env_id):
            if (vlan.get("functionName") or "").lower() != "hcx":
                continue
            for assoc in vlan.get("eipAssociations") or []:
                if assoc.get("allocationId"):
                    already.add(assoc["allocationId"])
    except Exception as e:  # noqa: BLE001 — best-effort pre-check
        logger.info("Could not read existing HCX EIP associations (%s); "
                    "attempting all associations", e)

    associated = 0
    for alloc_id in alloc_ids:
        if alloc_id in already:
            logger.info("EIP %s already associated with the HCX VLAN; skipping",
                        alloc_id)
            associated += 1
            continue
        evs.associate_eip_to_vlan(
            environment_id=env_id,
            vlan_name="hcx",
            allocation_id=alloc_id,
        )
        logger.info(
            "EIP %s associated with HCX VLAN in environment %s",
            alloc_id, env_id,
        )
        associated += 1

    logger.info("HCX VLAN has %d associated EIP(s) in environment %s",
                associated, env_id)
    if associated < 2:
        logger.warning(
            "Only %d EIP is associated with the HCX VLAN. HCX Manager and "
            "HCX-IX each need their own public address, so internet-based "
            "migration is likely to fail with fewer than 2.", associated,
        )
    return 0


def run_create_and_attach_ebs(
    aws: AWSClient,
    args: argparse.Namespace,
    config: dict,
    *,
    tfvars: dict | None = None,
) -> int:
    """Create + attach the EVS host's EBS volume.

    Replaces the ``ebs_volume`` Terraform module. Idempotent — looks up
    a volume tagged for this env first, skips create if found, skips
    attach if already correctly attached, raises if attached elsewhere.

    Args:
        aws: AWSClient.
        args: argparse namespace (we use ``dry_run``).
        config: Phase 2 config dict (used for environment_id).
        tfvars: Optional pre-loaded tfvars dict. The chained path
            passes it in-memory; standalone invocations read from disk.
    """
    env_id = config.get("environmentId")
    if not env_id:
        logger.error(
            "'environmentId' is required in config for create-and-attach-ebs"
        )
        return 1

    if tfvars is None:
        tfvars = _load_phase2_tfvars()

    target_instance_id = tfvars.get("target_instance_id") or ""
    availability_zone = tfvars.get("availability_zone") or ""

    if not target_instance_id:
        logger.error(
            "target_instance_id missing from %s — re-run "
            "post-evs-sync-config to populate it.",
            PHASE2_STATE_PATH,
        )
        return 1
    if not availability_zone:
        logger.error(
            "availability_zone missing from %s — re-run "
            "post-evs-sync-config to populate it.",
            PHASE2_STATE_PATH,
        )
        return 1

    manager = EbsVolumeManager(aws.client("ec2"), environment_id=env_id)
    result = manager.ensure_volume_attached(
        availability_zone=availability_zone,
        target_instance_id=target_instance_id,
        dry_run=args.dry_run,
    )
    print(json.dumps(result, indent=2, default=str))
    return 0


def main(argv: list[str] | None = None) -> int:
    """Main entry point."""
    args = parse_args(argv)

    config = load_config(args.config)

    aws = AWSClient(
        region=config.get("region", "us-east-1"),
        profile=args.profile,
        role_arn=args.role_arn,
    )

    if args.action == "post-evs-sync-config":
        return run_post_evs_sync_config(aws, args, config)

    if args.action == "associate-vlan-subnets":
        return run_associate_vlan_subnets(aws, args)

    if args.action == "create-and-attach-ebs":
        return run_create_and_attach_ebs(aws, args, config)

    logger.info("Initialized AWS client: %s", aws)
    logger.info("Account ID: %s", aws.account_id)

    evs = EVSManager(aws)

    if args.action == "associate-hcx-eip":
        return run_associate_hcx_eip(evs, config, args.dry_run)

    if args.action == "create-hosts":
        return run_create_hosts(evs, config, args.dry_run, args.config, args.instance_type)

    if args.action == "create-environment-and-hosts":
        return run_create_environment_and_hosts(
            evs, config, args.dry_run, args.config, args.instance_type,
        )

    actions = {
        "create-environment": run_create_environment,
    }

    return actions[args.action](evs, config, args.dry_run, args.config)


if __name__ == "__main__":
    sys.exit(main())
