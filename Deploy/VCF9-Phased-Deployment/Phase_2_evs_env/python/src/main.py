# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Entry point for the EVS environment deployment tool."""

import argparse
import json
import logging
import os
import sys
from pathlib import Path

from src.aws_client import AWSClient
from src.config_sync import ConfigSync
from src.ebs_volume_manager import EbsVolumeManager
from src.edge_cluster_spec import EdgeClusterSpec
from src.evs_manager import EVSManager
from src.phase2_sync import Phase2Sync
from src.phase3_sync import Phase3Sync
from src.vcf_password_provisioner import ensure_vcf_passwords
from src.vlan_route_table_associator import VlanRouteTableAssociator

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
    repo_root = Path(__file__).resolve().parents[3]
    default2 = repo_root / "Phase_2_evs_env" / "python" / "state.json"
    default3 = repo_root / "Phase_3_VCF9" / "bringup_spec.json"
    default3_edge = repo_root / "Phase_3_VCF9" / "edge_cluster_spec.json"
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
            "pre-evs-sync-config",
            "post-evs-sync-config",
            "associate-vlan-subnets",
            "associate-hcx-eip",
            "create-and-attach-ebs",
            "deploy-environment",
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
        "--tfstate",
        default=None,
        help="Path to the Phase 1 terraform.tfstate file (required for pre-evs-sync-config and deploy-environment)",
    )
    parser.add_argument(
        "--instance-type",
        choices=["i4i.metal", "i7i.metal-24xl"],
        default=None,
        help="EC2 instance type for EVS hosts (required for create-hosts, create-environment-and-hosts, and deploy-environment)",
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
    }
    # Drop None values so the API doesn't receive optional fields that
    # the service model marks as required at the botocore layer.
    params = {k: v for k, v in params.items() if v is not None}

    if dry_run:
        logger.info("DRY RUN — would call create_environment with:")
        print(json.dumps(params, indent=2, default=str))
        return 0

    response = evs.create_environment(**params)
    env = response.get("environment", {})
    env_id = env.get("environmentId")
    logger.info("Environment ID: %s", env_id)
    logger.info("Environment State: %s", env.get("environmentState"))

    # Write the environment ID back to config.json
    if env_id:
        config["environmentId"] = env_id
        with open(config_path, "w") as f:
            json.dump(config, f, indent=2)
            f.write("\n")
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
    if not esx_version:
        target = config.get("vcfInstallerProductVersion") or ""
        if not target:
            logger.error(
                "'esxVersion' and 'vcfInstallerProductVersion' are both empty in "
                "config.json. Set at least one so the ESXi version can be determined."
            )
            return 1
        esx_version = evs.get_latest_esx_version(target, instance_type)

    if dry_run:
        payload = {
            "environmentId": environment_id,
            "hosts": additional_hosts,
            "esxVersion": esx_version,
        }
        logger.info("DRY RUN — would call create_environment_host for %d host(s):", len(additional_hosts))
        print(json.dumps(payload, indent=2, default=str))
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

    Same arguments as the individual actions: needs ``--instance-type`` so
    step 3 doesn't fail. ``--dry-run`` short-circuits each step's API
    call but still does the wait between them in non-dry-run mode (a
    dry run skips all real API calls).

    For operators who want to inspect state between steps (e.g. confirm
    VLAN subnet IDs after CreateEnvironment but before CreateHost), the
    three individual actions remain available.
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

    # run_create_environment wrote environmentId back to config.json on
    # success; re-read so step 3 sees it. (run_create_environment also
    # mutates the in-memory config dict, but reading from disk is the
    # authoritative source.)
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


def run_pre_evs_sync_config(args: argparse.Namespace) -> int:
    """Handle the pre-evs-sync-config action.

    Doesn't need AWS credentials. Reads Phase 1 Terraform state, updates
    ``config.json``, and writes initial Phase 3 spec files with env-ID
    placeholders that ``post-evs-sync-config`` fills in later.
    """
    if not args.tfstate:
        logger.error("--tfstate is required for pre-evs-sync-config")
        return 1

    syncer = ConfigSync(
        tfstate_path=args.tfstate,
        config_path=args.config,
    )
    updated_config = syncer.sync(dry_run=args.dry_run)

    # First pass of the bringup spec. Env ID isn't known yet, so
    # env-derived fields get a placeholder until post-evs-sync-config.
    phase3 = Phase3Sync(
        output_path=PHASE3_BRINGUP_SPEC_PATH,
        config=updated_config,
        environment_id=updated_config.get("environmentId"),
    )
    phase3.sync(dry_run=args.dry_run)

    # First pass of the edge cluster spec. Same env-id placeholder story.
    edge_spec = EdgeClusterSpec(
        output_path=PHASE3_EDGE_CLUSTER_SPEC_PATH,
        config=updated_config,
        environment_id=updated_config.get("environmentId"),
    )
    edge_spec.sync(dry_run=args.dry_run)
    return 0


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

    # Fail fast if any per-host ESXi root secret is missing. Phase 3's
    # bringup uses ``__SECRET:esxi:<host>__`` placeholders that resolve
    # against ``evs!<env>_<host>`` — without those secrets the bringup
    # spec POST blows up at the host commissioning step. EVS creates
    # these secrets when it provisions each host, so a miss here
    # usually means a host failed to land.
    missing_esxi = phase2_result.get("missing_esxi_secrets") or []
    if missing_esxi and not args.dry_run:
        logger.error(
            "Missing ESXi root password secret(s) in AWS Secrets Manager "
            "for env %s: %s. EVS provisions these per-host when each "
            "host lands; check ListEnvironmentHosts for failed hosts.",
            env_id, ", ".join(sorted(missing_esxi)),
        )
        return 1

    # Provision the VCF appliance passwords as Secrets Manager secrets
    # (vCenter root + SSO, NSX root/admin/audit, SDDC Manager x3,
    # Operations admin/master/data/replica/collector, edge appliance,
    # plus 9.0-only fleet manager root + admin). Idempotent: existing
    # secrets are left alone. Phase 3's secret_resolver fetches these
    # at runtime via the placeholders embedded in bringup_spec.json /
    # edge_cluster_spec.json.
    if not args.dry_run:
        sm_client = aws.client("secretsmanager")
        ensure_vcf_passwords(sm_client, env_id, config)
    else:
        from src.vcf_password_provisioner import required_secret_roles
        logger.info(
            "DRY RUN — would provision VCF appliance secrets for roles: %s",
            ", ".join(required_secret_roles(config)),
        )

    # Phase 3: VCF bringup spec (now with env-id-derived names)
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

    Used by the standalone variants of the new
    ``associate-vlan-subnets`` and ``create-and-attach-ebs`` actions, so
    the operator can run them after a previous ``post-evs-sync-config``
    without re-running the full chain. The chained ``deploy-environment``
    path passes the same data in-memory and skips this file read.
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
            "service_access_route_table_id missing from %s — "
            "Phase 1 may not have produced it. Re-run "
            "pre-evs-sync-config or check Phase 1 outputs.",
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
    """Associate the pre-allocated EIP with the HCX VLAN.

    Only runs when ``hcxPublic`` is true and ``hcxEipAllocationId`` is
    present in config (both populated by ``pre-evs-sync-config`` from the
    Phase 1 Terraform outputs). Finds the HCX VLAN by listing the
    environment's VLANs and matching ``functionName == 'hcx'``.

    Idempotent: EVS returns success if the EIP is already associated.
    """
    if not config.get("hcxPublic"):
        logger.info("HCX public not enabled; skipping EIP association")
        return 0

    eip_alloc_id = config.get("hcxEipAllocationId")
    if not eip_alloc_id:
        logger.error(
            "hcxPublic is true but hcxEipAllocationId is missing from "
            "config. Re-run pre-evs-sync-config with a Phase 1 state that "
            "includes the hcx_eip_allocation_id output."
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
            "DRY RUN — would associate EIP %s with HCX VLAN 'hcx' "
            "in environment %s",
            eip_alloc_id, env_id,
        )
        return 0

    evs.associate_eip_to_vlan(
        environment_id=env_id,
        vlan_name="hcx",
        allocation_id=eip_alloc_id,
    )
    logger.info(
        "EIP %s associated with HCX VLAN in environment %s",
        eip_alloc_id, env_id,
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


def run_deploy_environment(args: argparse.Namespace) -> int:
    """One-shot wrapper: pre-sync → create env+hosts → wait → post-sync.

    Sequences all Phase 2 steps end-to-end so the operator runs a single
    command from a fresh Phase 1 state to fully-prepared Phase 3 spec
    files. Each substep delegates to its existing handler so logs and
    behavior match running the steps individually.

    Pipeline:
      1. ``pre-evs-sync-config`` — read Phase 1 state, write initial
         config.json + Phase 3 placeholder specs.
      2. ``create-environment-and-hosts`` — POSTs CreateEnvironment,
         polls until CREATED, then submits CreateEnvironmentHost calls.
      3. Wait for every host to reach the ``CREATED`` state (host
         realization is the long pole — 20-40 min per host, polled in
         parallel).
      4. ``post-evs-sync-config`` — finalize Phase 2 tfvars, fetch
         per-host secrets, regenerate Phase 3 specs with real names.
      5. ``associate-vlan-subnets`` — wire every EVS-created VLAN
         subnet to Phase 1's service-access route table.
      6. ``associate-hcx-eip`` — when HCX public is enabled, associate
         the pre-allocated EIP with the HCX VLAN via EVS API. No-op
         when HCX public is disabled.
      7. ``create-and-attach-ebs`` — create the 256 GB EBS volume that
         hosts the VCF Installer VMFS and attach it to the EVS host
         identified in step 4.

    Total runtime: ~30-50 minutes, dominated by step 3 (host realization).
    """
    if not args.tfstate:
        logger.error("--tfstate is required for deploy-environment")
        return 1
    if not args.instance_type:
        logger.error("--instance-type is required for deploy-environment")
        return 1

    # Step 1: pre-evs-sync-config (no AWS calls).
    logger.info("deploy-environment 1/7: pre-evs-sync-config")
    rc = run_pre_evs_sync_config(args)
    if rc != 0:
        return rc

    # Loading config now that pre-sync has populated it from tfstate.
    config = load_config(args.config)

    aws = AWSClient(
        region=config.get("region", "us-east-1"),
        profile=args.profile,
        role_arn=args.role_arn,
    )
    logger.info("Initialized AWS client: %s", aws)
    logger.info("Account ID: %s", aws.account_id)
    evs = EVSManager(aws)

    # Step 2: create-environment-and-hosts (chained internally).
    logger.info("deploy-environment 2/7: create-environment-and-hosts")
    rc = run_create_environment_and_hosts(
        evs, config, args.dry_run, args.config, args.instance_type,
    )
    if rc != 0:
        return rc

    if args.dry_run:
        logger.info(
            "DRY RUN — skipping host-readiness wait, post-evs-sync-config, "
            "VLAN subnet association, HCX EIP association, and EBS volume "
            "create/attach"
        )
        return 0

    # Reload config — create-environment-and-hosts wrote environmentId.
    config = load_config(args.config)
    env_id = config.get("environmentId")
    if not env_id:
        logger.error(
            "create-environment-and-hosts didn't write an environmentId — "
            "can't continue to host-readiness wait."
        )
        return 1

    # Step 3: wait for hosts to land in CREATED. CreateEnvironmentHost
    # returns immediately with hostState=CREATING and EC2 metal
    # provisioning runs for 20-40 min after that. post-evs-sync-config
    # needs the EC2 instances to actually exist (it tag-filters them),
    # so we have to block here.
    logger.info(
        "deploy-environment 3/7: waiting for hosts in environment %s "
        "to reach CREATED (typical: 20-40 minutes)",
        env_id,
    )
    try:
        evs.wait_for_hosts_ready(env_id)
    except (RuntimeError, TimeoutError) as e:
        logger.error("Host readiness check failed: %s", e)
        return 1

    # Step 4: post-evs-sync-config.
    logger.info("deploy-environment 4/7: post-evs-sync-config")
    rc = run_post_evs_sync_config(aws, args, config)
    if rc != 0:
        return rc

    # Both new steps consume tfvars that step 4 just wrote. Read it
    # once here and pass in-memory so they don't redundantly hit disk.
    tfvars = _load_phase2_tfvars()

    # Step 5: associate-vlan-subnets. Wire every EVS-created VLAN
    # subnet to Phase 1's service-access route table.
    logger.info("deploy-environment 5/7: associate-vlan-subnets")
    rc = run_associate_vlan_subnets(aws, args, tfvars=tfvars)
    if rc != 0:
        return rc

    # Reload config — post-evs-sync-config may have updated it (e.g.
    # cached instance_type into evsInstanceType). The EBS step needs
    # environmentId, which is already there from step 2.
    config = load_config(args.config)

    # Step 6: associate-hcx-eip. When HCX public is enabled, associate
    # the pre-allocated EIP with the HCX VLAN. No-op when hcxPublic is
    # false.
    logger.info("deploy-environment 6/7: associate-hcx-eip")
    rc = run_associate_hcx_eip(evs, config, args.dry_run)
    if rc != 0:
        return rc

    # Step 7: create-and-attach-ebs. Create the 256 GB volume in the
    # host's AZ and attach it at /dev/sdf.
    logger.info("deploy-environment 7/7: create-and-attach-ebs")
    return run_create_and_attach_ebs(aws, args, config, tfvars=tfvars)


def main(argv: list[str] | None = None) -> int:
    """Main entry point."""
    args = parse_args(argv)

    if args.action == "deploy-environment":
        return run_deploy_environment(args)

    if args.action == "pre-evs-sync-config":
        return run_pre_evs_sync_config(args)

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
