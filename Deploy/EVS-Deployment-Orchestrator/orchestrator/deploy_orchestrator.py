#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""EVS + VCF9 Full Deployment Orchestrator.

Single-file, self-contained script that chains Phase 2 (EVS environment
creation), ESXi pre-work (VLAN tagging, VMFS datastore, OVA deployment),
and Phase 3 (VCF bringup + NSX edge cluster) into one unattended run.

Runs on a lightweight Linux EC2 inside the customer's VPC with direct
network access to ESXi hosts, VCF Installer, NSX Manager, and vCenter.

The blueprint.yaml is the ONLY input — no dependency on
an external state file or a pre-existing config.json.

Usage:
    python3 deploy_orchestrator.py --config blueprint.yaml
    python3 deploy_orchestrator.py --config blueprint.yaml --resume
    python3 deploy_orchestrator.py --config blueprint.yaml --start-from phase3_deploy
"""

# === BOOTSTRAP — install dependencies before any third-party imports ===

import argparse
import hashlib
import json
import logging
import os
import re
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote as url_quote

REQUIRED_PACKAGES = [
    "boto3",
    "pyvmomi==9.1.0.0",
    "vcf-installer==9.1.0.0",
    "vcf-nsx==9.1.0.0",
    "requests",
    "pyyaml",
]


def _bootstrap():
    """Install Python dependencies if not already present.

    After installing, re-exec the interpreter: site-packages directories
    created after interpreter startup are not importable in the running
    process, so without the re-exec the very next third-party import
    (boto3) fails with ModuleNotFoundError. The marker file guarantees
    the re-exec happens at most once.
    """
    marker = Path(".evs_orchestrator_deps_installed")
    if marker.exists():
        return
    print("[bootstrap] Installing Python dependencies...")
    subprocess.check_call(
        [sys.executable, "-m", "pip", "install", "--quiet"] + REQUIRED_PACKAGES,
        stdout=subprocess.DEVNULL,
    )
    marker.touch()
    print("[bootstrap] Done. Re-executing with fresh site-packages...")
    sys.stdout.flush()
    os.execv(sys.executable, [sys.executable] + sys.argv)


_bootstrap()

# === Third-party imports (safe after bootstrap) ===

import boto3  # noqa: E402
import requests as http_requests  # noqa: E402
import yaml  # noqa: E402

# === stdout buffering ===
# Logger output is unbuffered (stderr); bare print() is block-buffered (stdout),
# so blobs land out of order in the shared log file. Line-buffer stdout to fix
# ordering globally without touching the ~25 print() sites.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(line_buffering=True)

# === Logging ===

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("orchestrator.log"),
    ],
)
logger = logging.getLogger("orchestrator")

# === Constants ===

SCRIPT_DIR = Path(__file__).resolve().parent
PHASE2_SRC = SCRIPT_DIR / "evs_environment"
PHASE3_SRC = SCRIPT_DIR / "vcf_deployment"
GENERATED_CONFIG_PATH = SCRIPT_DIR / "evs_environment" / "config.json"

# EVS always assigns VLAN ID 20 to the vmManagement subnet (fixed sequential
# assignment, not customer-configurable). The installer OVA's "VM Network" port
# group must be tagged with this VLAN so the appliance lands on the mgmt subnet.
ESXI_PREWORK_VLAN_ID = 20

# Name for the temporary local-disk VMFS datastore used to stage the installer
# OVA before vSAN exists. Purely an internal implementation detail - the
# datastore is created and removed within the same run, so its name has no
# customer-visible effect.
ESXI_PREWORK_VMFS_DATASTORE_NAME = "VCF-Installer-VMFS"


# ============================================================================
# Source path management
# ============================================================================


def _activate_phase2():
    """Put Phase 2 source at the front of sys.path, flush cached modules."""
    target = str(PHASE2_SRC)
    other = str(PHASE3_SRC)
    sys.path = [target] + [p for p in sys.path if p not in (target, other)]
    # Flush any modules loaded from the OTHER phase's directory so Python
    # re-imports from the new sys.path[0]. Without this, a cached 'main'
    # from vcf_deployment stays in sys.modules and shadows evs_environment's
    # 'main' (or vice versa) since both packages have a module named 'main'.
    stale = [k for k in sys.modules
             if hasattr(sys.modules[k], "__file__") and sys.modules[k].__file__
             and other in sys.modules[k].__file__]
    for key in stale:
        del sys.modules[key]


def _activate_phase3():
    """Put Phase 3 source at the front of sys.path, flush cached modules."""
    target = str(PHASE3_SRC)
    other = str(PHASE2_SRC)
    sys.path = [target] + [p for p in sys.path if p not in (target, other)]
    stale = [k for k in sys.modules
             if hasattr(sys.modules[k], "__file__") and sys.modules[k].__file__
             and other in sys.modules[k].__file__]
    for key in stale:
        del sys.modules[key]


# ============================================================================
# Config generation — builds config.json from blueprint.yaml
# ============================================================================


def generate_config_json(config: dict) -> Path:
    """Generate the Phase 2/3 config.json from the orchestrator YAML config.

    This replaces the pre-evs-sync-config step (which normally reads
    terraform.tfstate). All values come from the YAML instead.
    """
    vpc = config["vpc"]
    dns = config["dns"]
    evs = config["evs"]
    sizing = config["sizing"]
    hostnames = config["hostnames"]
    hcx = config.get("hcx", {})
    phase3 = config.get("phase3", {})
    prefix = vpc["cidr_prefix"]

    vlan_cidrs = {
        "vmkManagement": {"cidr": f"{prefix}10.0/24"},
        "vMotion": {"cidr": f"{prefix}20.0/24"},
        "vSan": {"cidr": f"{prefix}30.0/24"},
        "vTep": {"cidr": f"{prefix}40.0/24"},
        "edgeVTep": {"cidr": f"{prefix}50.0/24"},
        "vmManagement": {"cidr": f"{prefix}60.0/24"},
        "hcx": {"cidr": f"{prefix}70.0/24"},
        "nsxUplink": {"cidr": f"{prefix}80.0/24"},
        "expansionVlan1": {"cidr": f"{prefix}90.0/24"},
        "expansionVlan2": {"cidr": f"{prefix}100.0/24"},
    }

    if hcx.get("enabled"):
        # In bootstrap mode, public_cidr defaults to the standard HCX VLAN CIDR;
        # network_acl_id comes from the aws_config stage outputs.
        if hcx.get("public_cidr"):
            vlan_cidrs["hcx"]["cidr"] = hcx["public_cidr"]
        vlan_cidrs["isHcxPublic"] = True
        vlan_cidrs["hcxNetworkAclId"] = hcx["network_acl_id"]

    if "key_name" not in evs or not evs["key_name"]:
        raise RuntimeError(
            "generate_config: evs.key_name is not set. In bootstrap mode "
            "this should have been populated by the aws_config stage's "
            "outputs (_apply_aws_config_outputs) - check that stage ran "
            "and returned a key_name output. In BYO/Terraform mode, set "
            "evs.key_name directly in the blueprint."
        )

    additional_hosts = [
        {
            "hostName": h,
            "keyName": evs["key_name"],
            "instanceType": evs["instance_type"],
        }
        for h in hostnames["esxi"]
    ]

    # Preserve environment identity from a prior run: regenerating this file must
    # never make phase2 create a DUPLICATE EVS environment (3 more metal hosts).
    # esxVersion resolution order (prevents mid-deploy build drift on resume):
    # (1) prior run's resolved value, (2) blueprint evs.esx_version pin,
    # (3) null -> phase2 auto-resolves newest build for evs.vcf_version.
    prior_env_id, prior_esx = None, None
    if GENERATED_CONFIG_PATH.exists():
        try:
            _prior = json.loads(GENERATED_CONFIG_PATH.read_text())
            if _prior.get("environmentName") == evs["environment_name"]:
                prior_env_id = _prior.get("environmentId")
                prior_esx = _prior.get("esxVersion")
                if prior_env_id:
                    logger.info("generate_config: preserving existing environmentId %s", prior_env_id)
        except (json.JSONDecodeError, OSError):
            pass
    esx_version = prior_esx or evs.get("esx_version") or None
    if esx_version and not prior_esx:
        logger.info("generate_config: pinning esxVersion from blueprint: %s", esx_version)

    config_json = {
        "environmentName": evs["environment_name"],
        "vpcId": vpc["id"],
        "serviceAccessSubnetId": vpc["service_access_subnet_id"],
        "serviceAccessRouteTableId": vpc["service_access_route_table_id"],
        # Needed to resolve the IGW-routed public route table at runtime: the
        # HCX public VLAN subnet must be associated with it, not the
        # NAT-routed service-access table (see run_associate_vlan_subnets).
        "publicSubnetId": vpc.get("public_subnet_id", ""),
        "serviceAccessSecurityGroups": {
            "securityGroups": [vpc["security_group_id"]],
        },
        "evsEnvVersion": "SELF_DEPLOYED",
        "termsAccepted": evs["terms_accepted"],
        "initialVlans": vlan_cidrs,
        "region": config["aws"]["region"],
        "environmentId": prior_env_id,
        "esxVersion": esx_version,
        "additionalHosts": additional_hosts,
        "fqdn": dns["fqdn"],
        "fips": False,
        "ceip": phase3.get("ceip", False),
        "vcfInstance": "EVS-Management",
        "ntp": phase3.get("ntp", ["time.aws.com"]),
        "simpleDeployment": evs["simple_deployment"],
        "vcfInstallerProductVersion": evs["vcf_installer_product_version"],
        "vcfSizing": {
            "vcenterSize": sizing["vcenter_size"],
            "vcenterStorageSize": sizing["vcenter_storage_size"],
            "nsxSize": sizing["nsx_size"],
            "operationsApplianceSize": sizing["operations_appliance_size"],
            "operationsCollectorApplianceSize": sizing["operations_collector_appliance_size"],
            "edgeFormFactor": sizing.get("edge_form_factor", "LARGE"),
        },
        "dnsServers": [f"{prefix}0.100", f"{prefix}0.101"],
        "routeServerEndpoint01Ip": vpc["route_server_endpoint_01_ip"],
        "routeServerEndpoint02Ip": vpc["route_server_endpoint_02_ip"],
        "vcfHostnames": {
            "vcenter": hostnames["vcenter"],
            "nsx": hostnames["nsx"],
            "nsx01": hostnames.get("nsx01", "nsx01"),
            "nsx02": hostnames.get("nsx02", "nsx02"),
            "nsx03": hostnames.get("nsx03", "nsx03"),
            "sddc_manager": hostnames["sddc_manager"],
            "cloud_builder": hostnames.get("cloud_builder", "cb"),
            "edge01": hostnames.get("edge01", "edge01"),
            "edge02": hostnames.get("edge02", "edge02"),
            "vcf_ops": hostnames.get("vcf_ops", "vcfops"),
            "vcf_ops_01": hostnames.get("vcf_ops_01", "vcfops01"),
            "vcf_ops_02": hostnames.get("vcf_ops_02", "vcfops02"),
            "vcf_ops_03": hostnames.get("vcf_ops_03", "vcfops03"),
            "vcf_ops_collector": hostnames.get("vcf_ops_collector", "vcfopscol"),
            "vcf_fleet": hostnames.get("vcf_fleet", "vcffleet"),
        },
        "esxiHostnames": hostnames["esxi"],
    }

    if hcx.get("enabled"):
        config_json["hcxPublic"] = True
        config_json["hcxEipAllocationId"] = hcx["eip_allocation_id"]
        # Every allocated EIP must be associated, not just the first. HCX needs
        # a public address per appliance -- Manager and Interconnect (HCX-IX) at
        # minimum, plus one per Network Extension appliance -- and AWS documents
        # "You associate each Elastic IP that you want to use with an HCX
        # appliance to the HCX VLAN subnet". Associating only one leaves HCX-IX
        # without a public IP, so internet-based migration cannot work.
        if hcx.get("eip_allocation_ids"):
            config_json["hcxEipAllocationIds"] = list(hcx["eip_allocation_ids"])

    GENERATED_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    GENERATED_CONFIG_PATH.write_text(json.dumps(config_json, indent=2) + "\n")
    logger.info("Generated config.json at %s", GENERATED_CONFIG_PATH)
    return GENERATED_CONFIG_PATH


# ============================================================================
# Checkpoint
# ============================================================================


class Checkpoint:
    """Persistent stage-level checkpoint for resume capability."""

    def __init__(self, path: Path, config_hash: str):
        self.path = path
        self.config_hash = config_hash
        self.data = self._load()

    def _load(self) -> dict:
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text())
            except json.JSONDecodeError:
                logger.warning(
                    "Checkpoint file %s is corrupted (invalid JSON) — "
                    "starting fresh. This can happen if the process was "
                    "killed mid-write; consider looking at why (disk full? "
                    "OOM-killed mid-save?).", self.path,
                )
                return self._new()
            if data.get("config_hash") != self.config_hash:
                logger.warning("Config changed since last run — starting fresh")
                return self._new()
            return data
        return self._new()

    def _new(self) -> dict:
        return {
            "config_hash": self.config_hash,
            "started_at": _now(),
            "stages": {},
        }

    def is_completed(self, stage_id: str) -> bool:
        return self.data["stages"].get(stage_id, {}).get("status") == "completed"

    def get_result(self, stage_id: str) -> dict | None:
        return self.data["stages"].get(stage_id, {}).get("result")

    def mark_completed(self, stage_id: str, result: dict | None = None):
        self.data["stages"][stage_id] = {
            "status": "completed",
            "completed_at": _now(),
            "result": result,
        }
        self._save()

    def mark_failed(self, stage_id: str, error: str):
        self.data["stages"][stage_id] = {
            "status": "failed",
            "failed_at": _now(),
            "error": error,
        }
        self._save()

    def _save(self):
        # Atomic write: a direct write_text() crashing mid-write leaves corrupt
        # JSON. Write to a temp file in the same dir then os.replace() (atomic on
        # POSIX), so the checkpoint is always the old or new version, never partial.
        tmp_fd, tmp_path = tempfile.mkstemp(
            dir=str(self.path.parent) or ".", prefix=f".{self.path.name}.", suffix=".tmp",
        )
        try:
            with os.fdopen(tmp_fd, "w") as f:
                f.write(json.dumps(self.data, indent=2, default=str) + "\n")
            os.replace(tmp_path, self.path)
        except BaseException:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _notify(config: dict, subject: str, message: str) -> None:
    """Publish a status message to the configured SNS topic, if any.

    Best-effort and silent on failure -- a notification problem must never abort
    the deployment/teardown. Covers manual --resume/--destroy runs that the
    bootstrap CFN's bash watcher misses. topic_arn source order: the
    notification_topic_arn blueprint field, then the NOTIFICATION_TOPIC_ARN env var.
    """
    topic_arn = config.get("notification_topic_arn") or os.environ.get("NOTIFICATION_TOPIC_ARN")
    if not topic_arn:
        return
    # Tag every notification with this run's stack name so multiple
    # deployments publishing to the SAME topic stay distinguishable.
    # bootstrap_stack_name is injected into the blueprint by the CFN
    # user data; fall back to the environment name, then a generic tag.
    run = (config.get("aws", {}).get("bootstrap_stack_name")
           or config.get("evs", {}).get("environment_name") or "evs")
    subject = f"[{run}] {subject}"
    message = f"Stack: {run}\n\n{message}"
    try:
        session = boto3.Session(
            profile_name=config.get("aws", {}).get("profile"),
            region_name=config.get("aws", {}).get("region"),
        )
        session.client("sns").publish(
            TopicArn=topic_arn, Subject=subject[:100], Message=message[:100000],
        )
        logger.info("SNS notification sent: %s", subject)
    except Exception as e:  # noqa: BLE001
        logger.warning("Could not publish SNS notification (%r): %s", subject, e)


# Human-friendly labels for the deployment stages, used in progress
# notifications so operators see meaningful names, not internal stage ids.
_STAGE_LABELS = {
    "aws_config": "Landing zone (NAT gateway, Route Server, security group)",
    "generate_config": "Generate deployment config",
    "validate_dns": "Validate DNS",
    "phase2_deploy": "Create EVS environment + bare-metal hosts",
    "esxi_vlan_tag": "ESXi prep: VLAN tagging",
    "esxi_vmfs_create": "ESXi prep: VMFS datastore",
    "esxi_ova_deploy": "ESXi prep: deploy SDDC Manager OVA",
    "esxi_ova_verify": "ESXi prep: verify OVA deploy",
    "phase3_deploy": "VCF bringup + NSX edge cluster",
}


def _run_stage_with_heartbeat(config, stage_id, stage_fn, checkpoint,
                              position, total, heartbeat_seconds=7200):
    """Run one stage, emitting an SNS "still working" heartbeat every
    ``heartbeat_seconds`` (default 2 hours) for as long as the stage runs.

    Long stages — bare-metal host creation (~50+ min) and VCF bringup (2-4
    hours) — would otherwise go silent between the start and completion
    notifications. The heartbeat is a daemon thread and best-effort: it only
    calls the best-effort _notify and is stopped the instant the stage
    returns or raises, so it can never affect the stage's outcome.
    """
    label = _STAGE_LABELS.get(stage_id, stage_id)
    stop = threading.Event()

    def _beat() -> None:
        elapsed_h = 0
        while not stop.wait(heartbeat_seconds):
            elapsed_h += heartbeat_seconds // 3600
            _notify(
                config,
                f"Still in progress - {label}",
                f"Still working on: {label} ({stage_id}); about {elapsed_h}h "
                f"elapsed on this step. Bare-metal host creation and VCF "
                f"bringup can take several hours -- this is expected. You'll "
                f"get another email when this step completes, or if anything "
                f"fails. No action needed.",
            )

    beater = threading.Thread(target=_beat, name=f"heartbeat-{stage_id}", daemon=True)
    beater.start()
    try:
        return stage_fn(config, checkpoint)
    finally:
        stop.set()


def _aws_error_code(exc: Exception) -> str | None:
    """Return the structured AWS error code for a boto3 ClientError, if any.

    Prefer this over substring-matching str(exc) — message text can change
    across API/SDK versions and isn't a documented contract, while
    response["Error"]["Code"] is. Returns None for non-ClientError
    exceptions (network errors, etc.), which callers should treat as
    "unclassified" rather than assuming any specific error semantics.
    """
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        return response.get("Error", {}).get("Code")
    return None


# ============================================================================
# Helper functions
# ============================================================================


def _get_esxi_password(config: dict, host_name: str | None = None) -> str:
    """Retrieve the ESXi root password from AWS Secrets Manager.

    EVS stores ESXi passwords as plain strings (not JSON).
    If host_name is not provided, uses the host from _resolve_target_host_info.
    The evs!<env>_<host> secret is written asynchronously by the EVS service
    shortly after host creation — retry a bounded number of times if it has
    not appeared yet.
    """
    cfg = json.loads(GENERATED_CONFIG_PATH.read_text())
    env_id = cfg["environmentId"]
    if not host_name:
        host_name = _resolve_target_host_info(config)[1]
    secret_id = f"evs!{env_id}_{host_name}"

    session = boto3.Session(
        profile_name=config["aws"].get("profile"),
        region_name=config["aws"]["region"],
    )
    sm = session.client("secretsmanager")
    resp = None
    for attempt in range(10):  # EVS writes this secret asynchronously
        try:
            resp = sm.get_secret_value(SecretId=secret_id)
            break
        except sm.exceptions.ResourceNotFoundException:
            if attempt == 9:
                raise
            logger.info("Secret %s not available yet (EVS writes it async) — retry %d/9",
                        secret_id, attempt + 1)
            time.sleep(15)
    secret_string = resp["SecretString"]

    # EVS stores ESXi passwords as plain strings; appliance secrets as JSON
    try:
        parsed = json.loads(secret_string)
        return parsed.get("password", secret_string)
    except json.JSONDecodeError:
        return secret_string


def _get_installer_password(config: dict) -> str:
    """Retrieve the SDDC Manager local password from Secrets Manager.

    Same async-write race as _get_esxi_password: EVS/Phase 2 writes this
    secret shortly after host creation, not synchronously — retry a
    bounded number of times if it hasn't landed yet instead of crashing
    on the very first check.
    """
    cfg = json.loads(GENERATED_CONFIG_PATH.read_text())
    env_id = cfg["environmentId"]
    secret_id = f"evs-{env_id}_sddcManagerLocal"

    session = boto3.Session(
        profile_name=config["aws"].get("profile"),
        region_name=config["aws"]["region"],
    )
    sm = session.client("secretsmanager")
    resp = None
    for attempt in range(10):  # written asynchronously, same as _get_esxi_password
        try:
            resp = sm.get_secret_value(SecretId=secret_id)
            break
        except sm.exceptions.ResourceNotFoundException:
            if attempt == 9:
                raise
            logger.info("Secret %s not available yet (written async) — retry %d/9",
                        secret_id, attempt + 1)
            time.sleep(15)
    secret = json.loads(resp["SecretString"])
    return secret["password"]


_cached_target_host_info = None


def _resolve_target_host_info(config: dict) -> tuple:
    """Find the ESXi host that has the EBS volume attached.

    Returns (ip, hostname) tuple. Caches the result so repeated calls
    (from multiple stages) don't re-query AWS.

    Looks up the EBS volume by tag (ManagedBy=phase2-automation + EnvironmentId),
    finds which EC2 instance it's attached to, maps that to an ESXi hostname via
    the instance Name tag, then resolves the hostname via DNS.
    """
    import socket
    global _cached_target_host_info

    if _cached_target_host_info:
        return _cached_target_host_info

    cfg = json.loads(GENERATED_CONFIG_PATH.read_text())
    env_id = cfg.get("environmentId")

    session = boto3.Session(
        profile_name=config["aws"].get("profile"),
        region_name=config["aws"]["region"],
    )
    ec2 = session.client("ec2")

    # Find the EBS volume tagged for this environment (paginated — an account
    # with many volumes can span pages).
    volumes = []
    for page in ec2.get_paginator("describe_volumes").paginate(
        Filters=[
            {"Name": "tag:ManagedBy", "Values": ["phase2-automation"]},
            {"Name": "tag:EnvironmentId", "Values": [env_id]},
        ]
    ):
        volumes.extend(page.get("Volumes", []))
    # Only volumes with an active attachment count; stale/unattached leftovers
    # from earlier runs must not be selected.
    volumes = [v for v in volumes if v.get("Attachments")]

    if not volumes:
        raise RuntimeError(
            f"No EBS volume found with ManagedBy=phase2-automation and "
            f"EnvironmentId={env_id}. Ensure Phase 2 step 7 completed."
        )
    if len(volumes) > 1:
        logger.warning(
            "Multiple attached phase2-automation volumes for %s (%s); using "
            "the first. Investigate leftovers from a prior partial run.",
            env_id, [v["VolumeId"] for v in volumes],
        )

    volume = volumes[0]
    attachments = volume.get("Attachments", [])
    if not attachments:
        raise RuntimeError(f"EBS volume {volume['VolumeId']} is not attached to any instance")

    instance_id = attachments[0]["InstanceId"]

    # Get the instance's Name tag to determine which ESXi host it is
    instances = ec2.describe_instances(InstanceIds=[instance_id])
    instance = instances["Reservations"][0]["Instances"][0]
    name_tag = ""
    for tag in instance.get("Tags", []):
        if tag["Key"] == "Name":
            name_tag = tag["Value"]
            break

    # Extract hostname from Name tag like "DoNotDelete-EVS-[env-xxx]-[esxi01]"
    host_name = None
    for h in config["hostnames"]["esxi"]:
        if h in name_tag:
            host_name = h
            break

    if not host_name:
        raise RuntimeError(
            f"Could not determine ESXi hostname from instance {instance_id} "
            f"(Name tag: {name_tag})"
        )

    # Resolve the management IP via DNS
    fqdn = f"{host_name}.{config['dns']['fqdn']}"
    try:
        ip = socket.gethostbyname(fqdn)
        logger.info("EBS volume on %s (%s) -> %s", host_name, instance_id, ip)
        _cached_target_host_info = (ip, host_name)
        return _cached_target_host_info
    except socket.gaierror:
        raise RuntimeError(
            f"Could not resolve {fqdn} — ensure DNS is working and "
            f"the EC2 instance uses the VPC DNS server"
        )


def _resolve_target_host_ip(config: dict) -> str:
    """Return the management IP of the ESXi host with the EBS volume."""
    return _resolve_target_host_info(config)[0]


def _unverified_ssl_context() -> ssl.SSLContext:
    """SSLContext for connecting to ESXi/VCF appliance self-signed certs.

    These certs have no common CA, so verification is disabled (CERT_NONE).
    TLS pinning was tried and removed — it repeatedly failed these appliances
    (mid-bringup cert rotation, VMCA-issued chains, firstboot regeneration) in
    a private, account-controlled VPC where the threat was narrow.
    """
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _fetch_vmca_ca_bundle(host: str, port: int = 443) -> str | None:
    """Fetch VMCA's root CA cert from a VCF appliance's served TLS chain.

    After bringup, SDDC Manager (and NSX) serve their full chain: leaf +
    self-signed VMCA root (subject == issuer), the common trust anchor for all
    VCF appliances here. Extracts the root, writes it to a temp file, and returns
    the path (usable as requests' verify= or pyVmomi load_verify_locations).

    Returns None if the chain can't be fetched or no self-signed root is found
    (caller falls back to unverified).
    """
    try:
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        with socket.create_connection((host, port), timeout=15) as sock:
            with ctx.wrap_socket(sock, server_hostname=host) as tls:
                # Use the private chain API (stable since Python 3.10)
                try:
                    chain = tls._sslobj.get_unverified_chain()  # noqa: SLF001
                except AttributeError:
                    return None
                if not chain or len(chain) < 2:
                    return None
                # Find the self-signed root (subject == issuer)
                for cert in reversed(chain):
                    pem = cert.public_bytes()
                    # Parse subject/issuer to check if self-signed
                    info = cert.get_info()
                    subject = info.get("subject", "")
                    issuer = info.get("issuer", "")
                    if subject and subject == issuer:
                        fd, path = tempfile.mkstemp(
                            prefix="evs-vmca-ca-", suffix=".pem")
                        with os.fdopen(fd, "w") as f:
                            f.write(pem)
                        return path
                # No self-signed root found in chain — can't build a
                # usable CA bundle without it (requests needs a root
                # that terminates the chain)
                logger.warning("_fetch_vmca_ca_bundle: chain from %s has "
                               "%d certs but none are self-signed", host, len(chain))
                return None
    except (OSError, ssl.SSLError, Exception) as e:  # noqa: BLE001
        logger.warning("_fetch_vmca_ca_bundle: failed to fetch CA from %s: %s", host, e)
        return None


def _parse_s3_uri(uri: str) -> tuple[str, str]:
    """Split an "s3://bucket/key/with/slashes" URI into (bucket, key)."""
    if not uri.startswith("s3://"):
        raise ValueError(f"Expected an s3:// URI, got: {uri}")
    bucket, _, key = uri[len("s3://"):].partition("/")
    if not bucket or not key:
        raise ValueError(f"Malformed S3 URI (need s3://bucket/key): {uri}")
    return bucket, key


def _ensure_ovftool(config: dict, work_dir: Path) -> Path:
    """Download and install ovftool from S3 if not already present.

    Supports .zip (Linux) and .msi (Windows) formats. The S3 key's file
    extension determines the install method.
    """
    # Check if ovftool is already on PATH or in our install dir
    if shutil.which("ovftool"):
        return Path(shutil.which("ovftool"))

    ovftool_dir = work_dir / "ovftool"
    # Match the wrapper script by exact NAME — candidate.stem would also match
    # "ovftool.bin" (stem strips the suffix), and the raw .bin cannot locate
    # its bundled libraries without the wrapper.
    ovftool_candidates = list(ovftool_dir.glob("**/ovftool*")) if ovftool_dir.exists() else []
    for candidate in ovftool_candidates:
        if candidate.name == "ovftool" and candidate.is_file():
            return candidate

    prework = config["esxi_prework"]
    bucket, s3_key = _parse_s3_uri(prework["ovftool_s3_uri"])
    if s3_key.lower().endswith(".msi") and os.name != "nt":
        raise RuntimeError(
            "esxi_prework.ovftool_s3_uri points at a Windows .msi but this "
            "runner is Linux — stage the Linux ovftool .zip and update the blueprint."
        )
    filename = Path(s3_key).name
    local_path = work_dir / filename

    if not local_path.exists():
        logger.info("Downloading ovftool from S3 (%s)...", prework["ovftool_s3_uri"])
        session = boto3.Session(
            profile_name=config["aws"].get("profile"),
            region_name=config["aws"]["region"],
        )
        s3 = session.client("s3")
        s3.download_file(bucket, s3_key, str(local_path))

    ovftool_dir.mkdir(parents=True, exist_ok=True)

    if filename.endswith(".msi"):
        # Windows MSI — silent install using shell=True so msiexec
        # handles quoted paths correctly
        logger.info("Installing ovftool MSI...")
        msi_path = str(local_path.resolve())
        # Let msiexec install to its default location (Program Files)
        cmd = f'msiexec /i "{msi_path}" /quiet /norestart'
        subprocess.run(cmd, shell=True, check=True, timeout=120)

        # ovftool installs to Program Files by default
        default_path = Path(os.environ.get("ProgramFiles", "C:\\Program Files")) / "VMware" / "VMware OVF Tool" / "ovftool.exe"
        if default_path.exists():
            logger.info("ovftool installed at %s", default_path)
            return default_path

        # Search common locations
        for search_dir in [ovftool_dir, Path("C:\\Program Files"), Path("C:\\Program Files (x86)")]:
            candidates = list(search_dir.glob("**/ovftool.exe"))
            if candidates:
                logger.info("ovftool found at %s", candidates[0])
                return candidates[0]

        raise RuntimeError("ovftool.exe not found after MSI install")

    elif filename.endswith(".zip"):
        # Linux/cross-platform zip
        logger.info("Extracting ovftool zip...")
        with zipfile.ZipFile(local_path) as zf:
            zf.extractall(ovftool_dir)
        # Match files only: the zip has a *directory* named "ovftool" that globs
        # before the script (executing it fails "Permission denied"). zipfile also
        # drops mode bits, so restore exec on the wrapper AND sibling binaries.
        ovftool_candidates = [p for p in ovftool_dir.glob("**/ovftool") if p.is_file()]
        if not ovftool_candidates:
            raise RuntimeError("ovftool binary not found after zip extraction")
        ovftool_bin = ovftool_candidates[0]
        for sibling in ovftool_bin.parent.iterdir():
            if sibling.is_file():
                sibling.chmod(0o755)
        return ovftool_bin

    else:
        raise RuntimeError(f"Unsupported ovftool format: {filename} (expected .msi or .zip)")


def _redact_ovftool_log_in_place(log_path: Path, secrets: list[str]) -> None:
    """Scrub known secret values out of an ovftool log file, in place.

    ovftool's verbose log echoes injected OVF property passwords and the
    vi://root:<pw>@host URL in plaintext, which would otherwise ship to
    CloudWatch. Best-effort: on read/write failure, logs a warning rather than
    raising -- a failed redaction must not fail the OVA deploy stage.
    """
    try:
        text = log_path.read_text(errors="replace")
    except OSError as e:
        logger.warning("Could not read %s for redaction: %s", log_path, e)
        return

    for secret in secrets:
        if secret:
            text = text.replace(secret, "***REDACTED***")
            text = text.replace(url_quote(secret, safe=""), "***REDACTED***")
    # Belt-and-suspenders for the vi://root:<password>@host URL shape even if
    # the exact secret string didn't match verbatim (e.g. re-quoted
    # differently by ovftool's own echo).
    text = re.sub(r"vi://root:[^@]*@", "vi://root:***REDACTED***@", text)

    try:
        log_path.write_text(text)
    except OSError as e:
        logger.warning("Could not write redacted %s: %s", log_path, e)


def _ensure_ova(config: dict, work_dir: Path) -> Path:
    """Download OVA from S3 if not already present."""
    prework = config["esxi_prework"]
    bucket, s3_key = _parse_s3_uri(prework["ova_s3_uri"])
    ova_filename = Path(s3_key).name
    ova_path = work_dir / ova_filename

    if ova_path.exists():
        return ova_path

    logger.info("Downloading OVA from S3 (%s)...", prework["ova_s3_uri"])
    session = boto3.Session(
        profile_name=config["aws"].get("profile"),
        region_name=config["aws"]["region"],
    )
    s3 = session.client("s3")
    s3.download_file(bucket, s3_key, str(ova_path))
    logger.info("OVA downloaded: %s (%.1f GB)", ova_path.name, ova_path.stat().st_size / (1024**3))
    return ova_path


# ============================================================================
# Stage implementations
# ============================================================================


def stage_generate_config(config: dict, checkpoint: Checkpoint) -> dict:
    """Generate config.json from orchestrator YAML — replaces pre-evs-sync-config."""
    config_path = generate_config_json(config)
    return {"config_path": str(config_path)}


def stage_validate_dns(config: dict, checkpoint: Checkpoint) -> dict:
    """Validate that all hostnames in the config resolve via DNS.

    Catches mismatches between blueprint.yaml hostnames and
    what Phase 1 Terraform actually created in Route 53 — before the
    bringup spec is submitted to the VCF Installer.
    """
    import socket

    fqdn = config["dns"]["fqdn"]
    hostnames = config["hostnames"]

    # Collect all hostnames that need DNS records
    all_hosts = []

    # ESXi hosts
    for h in hostnames["esxi"]:
        all_hosts.append(h)

    # VCF component hostnames
    component_keys = [
        "vcenter", "nsx", "nsx01", "nsx02", "nsx03",
        "sddc_manager", "cloud_builder", "edge01", "edge02",
        "vcf_ops", "vcf_ops_01", "vcf_ops_02", "vcf_ops_03",
        "vcf_ops_collector", "vcf_fleet",
    ]
    for key in component_keys:
        val = hostnames.get(key)
        if val:
            all_hosts.append(val)

    # Resolve each and report
    resolved = {}
    failed = []

    for host in all_hosts:
        host_fqdn = f"{host}.{fqdn}"
        try:
            ip = socket.gethostbyname(host_fqdn)
            resolved[host_fqdn] = ip
        except socket.gaierror:
            failed.append(host_fqdn)

    if failed:
        logger.error("DNS validation failed - the following FQDNs do not resolve:")
        for f in failed:
            logger.error("  [FAIL] %s", f)
        logger.error(
            "Ensure these hostnames match the A records in your Route 53 "
            "private hosted zone. Update blueprint.yaml hostnames "
            "or create the missing DNS records."
        )
        raise RuntimeError(
            f"DNS validation failed: {len(failed)} hostname(s) do not resolve: "
            + ", ".join(failed)
        )

    logger.info("All %d hostnames resolve successfully", len(resolved))
    return {"resolved_count": len(resolved), "hostnames": resolved}


def stage_phase2_deploy(config: dict, checkpoint: Checkpoint) -> dict:
    """Run Phase 2 steps 2-7 (skipping pre-evs-sync-config since we generate config.json ourselves).

    Steps executed:
      2. create-environment-and-hosts (create env, wait for CREATED, add hosts)
      3. wait for hosts to reach CREATED
      4. post-evs-sync-config (find EC2 instance, VLAN subnets, provision secrets, write specs)
      5. associate-vlan-subnets
      6. associate-hcx-eip (no-op if HCX disabled)
      7. create-and-attach-ebs
    """
    _activate_phase2()
    from main import (
        load_config,
        run_create_environment_and_hosts,
        run_create_hosts,
        run_post_evs_sync_config,
        run_associate_vlan_subnets,
        run_associate_hcx_eip,
        run_create_and_attach_ebs,
        _load_phase2_tfvars,
    )
    from aws_client import AWSClient
    from evs_manager import EVSManager

    config_path = str(GENERATED_CONFIG_PATH)
    cfg = load_config(config_path)

    aws = AWSClient(
        region=cfg.get("region", "us-east-1"),
        profile=config["aws"].get("profile"),
        role_arn=config["aws"].get("role_arn"),
    )
    evs = EVSManager(aws)

    env_id = cfg.get("environmentId")

    if env_id:
        # Environment already exists — skip create, but reconcile the host set.
        logger.info("phase2_deploy: environment %s already exists, skipping create", env_id)

        # Reconcile: hosts are created one at a time and fail independently (EC2
        # capacity, vCPU quota). EVS keeps a failed host's hostName reserved, so
        # its record must be deleted before that name can be recreated -- else
        # every resume re-fails on the same dead host in wait_for_hosts_ready.
        hosts = evs.list_environment_hosts(env_id)
        failed = [
            h for h in hosts
            if (h.get("hostState") or "").upper() in {"FAILED", "CREATE_FAILED"}
        ]
        if failed:
            for h in failed:
                logger.info(
                    "phase2_deploy: removing failed host '%s' (state=%s, reason=%s)",
                    h.get("hostName"), h.get("hostState"),
                    h.get("stateDetails") or "not reported",
                )
                evs.delete_environment_host(env_id, h.get("hostName"))
            evs.wait_for_hosts_absent(env_id, [h.get("hostName") for h in failed])
            hosts = evs.list_environment_hosts(env_id)

        expected = [h.get("hostName") for h in (cfg.get("additionalHosts") or [])]
        present = {h.get("hostName") for h in hosts}
        missing = [name for name in expected if name not in present]

        if missing:
            logger.info(
                "phase2_deploy: creating %d missing host(s): %s",
                len(missing), ", ".join(missing),
            )
            # Wait for env to be in CREATED state first
            evs.wait_for_environment_state(env_id, "CREATED")
            missing_cfg = {
                **cfg,
                "additionalHosts": [
                    h for h in (cfg.get("additionalHosts") or [])
                    if h.get("hostName") in missing
                ],
            }
            rc = run_create_hosts(
                evs, missing_cfg, dry_run=False,
                config_path=config_path, instance_type=config["evs"]["instance_type"],
            )
            if rc != 0:
                raise RuntimeError(f"create-hosts failed (rc={rc})")
        else:
            logger.info("phase2_deploy: all %d expected host(s) present", len(hosts))
    else:
        # Step 2: create-environment-and-hosts
        logger.info("phase2_deploy 1/6: create-environment-and-hosts")
        rc = run_create_environment_and_hosts(
            evs, cfg, False, config_path, config["evs"]["instance_type"],
        )
        if rc != 0:
            raise RuntimeError(f"create-environment-and-hosts failed (rc={rc})")

        # Reload config — environmentId was written back
        cfg = load_config(config_path)
        env_id = cfg.get("environmentId")
        if not env_id:
            raise RuntimeError("environmentId not found after create-environment-and-hosts")

    # Step 3: wait for hosts
    logger.info("phase2_deploy 2/6: waiting for hosts to reach CREATED")
    evs.wait_for_hosts_ready(env_id)

    # Step 4: post-evs-sync-config
    logger.info("phase2_deploy 3/6: post-evs-sync-config")
    args = argparse.Namespace(
        action="post-evs-sync-config",
        config=config_path,
        profile=config["aws"].get("profile"),
        role_arn=config["aws"].get("role_arn"),
        dry_run=False,
        instance_type=config["evs"]["instance_type"],
    )
    rc = run_post_evs_sync_config(aws, args, cfg)
    if rc != 0:
        raise RuntimeError(f"post-evs-sync-config failed (rc={rc})")

    # Reload state for steps 5-7
    tfvars = _load_phase2_tfvars()

    # Step 5: associate-vlan-subnets
    logger.info("phase2_deploy 4/6: associate-vlan-subnets")
    rc = run_associate_vlan_subnets(aws, args, tfvars=tfvars)
    if rc != 0:
        raise RuntimeError(f"associate-vlan-subnets failed (rc={rc})")

    # Reload config
    cfg = load_config(config_path)

    # Step 6: associate-hcx-eip
    logger.info("phase2_deploy 5/6: associate-hcx-eip")
    rc = run_associate_hcx_eip(evs, cfg, False)
    if rc != 0:
        raise RuntimeError(f"associate-hcx-eip failed (rc={rc})")

    # Step 7: create-and-attach-ebs
    logger.info("phase2_deploy 6/6: create-and-attach-ebs")
    rc = run_create_and_attach_ebs(aws, args, cfg, tfvars=tfvars)
    if rc != 0:
        raise RuntimeError(f"create-and-attach-ebs failed (rc={rc})")

    cfg = load_config(config_path)
    return {"environment_id": cfg.get("environmentId")}


def stage_esxi_vlan_tag(config: dict, checkpoint: Checkpoint) -> dict:
    """Set VLAN ID on the 'VM Network' port group of the target ESXi host."""
    from pyVim import connect
    from pyVmomi import vim  # noqa: F401

    host_ip = _resolve_target_host_ip(config)
    password = _get_esxi_password(config)
    vlan_id = ESXI_PREWORK_VLAN_ID

    ctx = _unverified_ssl_context()
    si = connect.SmartConnect(host=host_ip, user="root", pwd=password, sslContext=ctx)
    try:
        content = si.content
        host = content.rootFolder.childEntity[0].hostFolder.childEntity[0].host[0]
        ns = host.configManager.networkSystem

        for pg in ns.networkInfo.portgroup:
            if pg.spec.name == "VM Network":
                if pg.spec.vlanId == vlan_id:
                    logger.info("VM Network already tagged with VLAN %d", vlan_id)
                    return {"status": "already_tagged", "vlan_id": vlan_id}
                spec = pg.spec
                spec.vlanId = vlan_id
                ns.UpdatePortGroup("VM Network", spec)
                logger.info("Tagged VM Network with VLAN %d", vlan_id)
                return {"status": "tagged", "vlan_id": vlan_id}

        raise RuntimeError("Port group 'VM Network' not found on host")
    finally:
        connect.Disconnect(si)


def stage_esxi_vmfs_create(config: dict, checkpoint: Checkpoint) -> dict:
    """Create a VMFS 6 datastore on the 256 GB EBS-backed NVMe device."""
    from pyVim import connect
    from pyVmomi import vim  # noqa: F401

    host_ip = _resolve_target_host_ip(config)
    password = _get_esxi_password(config)
    ds_name = ESXI_PREWORK_VMFS_DATASTORE_NAME

    ctx = _unverified_ssl_context()
    si = connect.SmartConnect(host=host_ip, user="root", pwd=password, sslContext=ctx)
    try:
        content = si.content
        host = content.rootFolder.childEntity[0].hostFolder.childEntity[0].host[0]
        ds_system = host.configManager.datastoreSystem

        for ds in host.datastore:
            if ds.name == ds_name:
                logger.info("Datastore '%s' already exists", ds_name)
                return {"status": "already_exists", "datastore": ds_name}

        disks = ds_system.QueryAvailableDisksForVmfs()
        target_disk = None
        # The EBS volume is attached moments before this stage runs; ESXi does
        # not see new devices until its storage subsystem rescans. Rescan and
        # retry for up to ~5 minutes before declaring the device missing.
        storage_system = host.configManager.storageSystem
        deadline = time.time() + 300
        while target_disk is None:
            for disk in disks:
                capacity_gb = disk.capacity.block * disk.capacity.blockSize / (1024**3)
                if 250 <= capacity_gb <= 260:
                    target_disk = disk
                    break
            if target_disk or time.time() >= deadline:
                break
            logger.info("~256 GB device not visible yet — rescanning HBAs and retrying...")
            try:
                storage_system.RescanAllHba()
                storage_system.RescanVmfs()
            except Exception as rescan_err:  # noqa: BLE001
                logger.warning("rescan failed (will still retry): %s", rescan_err)
            time.sleep(20)
            disks = ds_system.QueryAvailableDisksForVmfs()

        if not target_disk:
            raise RuntimeError(
                "No ~256 GB NVMe device found for VMFS creation after 5 minutes "
                "of rescanning. Ensure the EBS volume is attached (Phase 2 step 7)."
            )

        options = ds_system.QueryVmfsDatastoreCreateOptions(target_disk.devicePath)
        if not options:
            raise RuntimeError(f"No VMFS create options for {target_disk.devicePath}")

        create_spec = options[0].spec
        create_spec.vmfs.volumeName = ds_name

        ds_system.CreateVmfsDatastore(create_spec)
        logger.info("Created VMFS datastore '%s' on %s", ds_name, target_disk.devicePath)
        return {"status": "created", "datastore": ds_name, "device": target_disk.devicePath}
    finally:
        connect.Disconnect(si)


def stage_esxi_ova_deploy(config: dict, checkpoint: Checkpoint) -> dict:
    """Download OVA + ovftool from S3 and deploy VCF Installer via ovftool."""
    from pyVim import connect

    work_dir = SCRIPT_DIR / "evs_ova"
    work_dir.mkdir(parents=True, exist_ok=True)

    host_ip = _resolve_target_host_ip(config)
    password = _get_esxi_password(config)
    ds_name = ESXI_PREWORK_VMFS_DATASTORE_NAME
    vm_name = config["hostnames"]["sddc_manager"]

    # Idempotency: check if VM already exists on the host
    ctx = _unverified_ssl_context()
    si = connect.SmartConnect(host=host_ip, user="root", pwd=password, sslContext=ctx)
    try:
        content = si.content
        host = content.rootFolder.childEntity[0].hostFolder.childEntity[0].host[0]
        for vm in host.vm:
            if vm.name == vm_name:
                if vm.runtime.powerState == "poweredOn":
                    logger.info("VM '%s' already exists and is powered on", vm_name)
                    return {"status": "already_deployed", "vm": vm_name}
                logger.info("VM '%s' exists but powered off — powering on", vm_name)
                from pyVim.task import WaitForTask
                # WaitForTask (raises on error) instead of fire-and-forget
                # PowerOnVM_Task(): a power-on failure surfaces here, not as a
                # confusing timeout in esxi_ova_verify 40 min later.
                WaitForTask(vm.PowerOnVM_Task(), si=si)
                return {"status": "powered_on", "vm": vm_name}
    finally:
        connect.Disconnect(si)

    ovftool_bin = _ensure_ovftool(config, work_dir)
    ova_path = _ensure_ova(config, work_dir)

    fqdn = config["dns"]["fqdn"]
    prefix = config["vpc"]["cidr_prefix"]
    installer_ip = f"{prefix}60.12"
    gateway = f"{prefix}60.1"
    dns_servers = f"{prefix}0.100,{prefix}0.101"
    installer_password = _get_installer_password(config)

    ntp_servers = ",".join(config.get("phase3", {}).get("ntp", ["time.aws.com"]))

    cmd = [
        str(ovftool_bin),
        # Core flags
        f"--name={vm_name}",
        "--acceptAllEulas",
        f"--datastore={ds_name}",
        "--allowExtraConfig",
        "--diskMode=thin",
        "--machineOutput",
        "--powerOn",
        "--noSSLVerify",
        # Logging
        "--X:logLevel=verbose",
        "--X:logToConsole",
        # Retry/reconnect
        "--X:connectionFileTransferRetryCount=3",
        "--X:connectionReconnectCount=5",
        "--X:connectionRetryCount=3",
        "--X:connectionReconnectDelay=5000",
        "--X:connectionReconnectDelayDouble",
        # OVF property injection
        "--X:injectOvfEnv",
        f"--prop:ROOT_PASSWORD={installer_password}",
        f"--prop:LOCAL_USER_PASSWORD={installer_password}",
        f"--prop:VCF_PASSWORD={installer_password}",
        f"--prop:BASIC_AUTH_PASSWORD={installer_password}",
        f"--prop:BACKUP_PASSWORD={installer_password}",
        f"--prop:vami.hostname={vm_name}.{fqdn}",
        f"--prop:guestinfo.ntp={ntp_servers}",
        f"--prop:vami.ip0.SDDC-Manager={installer_ip}",
        "--prop:vami.netmask0.SDDC-Manager=255.255.255.0",
        f"--prop:vami.gateway.SDDC-Manager={gateway}",
        f"--prop:vami.domain.SDDC-Manager={fqdn}",
        f"--prop:vami.searchpath.SDDC-Manager={fqdn}",
        f"--prop:vami.DNS.SDDC-Manager={dns_servers}",
        # Source and target
        str(ova_path),
        f"vi://root:{url_quote(password, safe='')}@{host_ip}/",
    ]

    logger.info("Deploying OVA via ovftool to %s...", host_ip)
    # Stream ovftool output to a file, not capture_output: verbose mode produces
    # gigabytes that capture_output buffers in memory, OOM-killing the
    # orchestrator on small runners (observed: 3.5 GB RSS killed on a 4 GB host).
    ovftool_log = work_dir / "ovftool.log"
    try:
        with open(ovftool_log, "w") as lf:
            result = subprocess.run(cmd, stdout=lf, stderr=subprocess.STDOUT, timeout=3600)
    finally:
        # ovftool's verbose log echoes the OVF property passwords and the vi://
        # URL (ESXi root password) in plaintext. Scrub in place, in finally: this
        # MUST run even if subprocess.run() raised (timeout, OOM, interrupt), or a
        # plaintext credential dump is left on disk and tailed into CloudWatch.
        if ovftool_log.exists():
            _redact_ovftool_log_in_place(ovftool_log, secrets=[installer_password, password])

    if result.returncode != 0:
        try:
            log_tail = ovftool_log.read_text(errors="replace")[-2000:]
        except OSError:
            log_tail = "(ovftool log unreadable)"
        logger.error("ovftool output tail:\n%s", log_tail)
        raise RuntimeError(
            f"ovftool failed with exit code {result.returncode} "
            f"(full log: {ovftool_log})"
        )

    logger.info("OVA deployed successfully: %s at %s", vm_name, installer_ip)
    return {"status": "deployed", "vm": vm_name, "ip": installer_ip}


def stage_esxi_ova_verify(config: dict, checkpoint: Checkpoint) -> dict:
    """Wait for the VCF Installer API to be fully ready.

    Polls the /v1/tokens endpoint (not just nginx) to ensure the backend
    Java services are up and accepting authentication requests.
    Also sets VCF_INSTALLER_PASSWORD env var so Phase 3 can consume it.
    """
    prefix = config["vpc"]["cidr_prefix"]
    installer_ip = f"{prefix}60.12"
    # Connect by the appliance's real hostname, not raw IP, for consistency with
    # every other stage. The cert's Subject/SAN is for the hostname, but that
    # only matters once verification is on (this probe uses verify=False).
    installer_host = f"{config['hostnames']['sddc_manager']}.{config['dns']['fqdn']}"
    api_url = f"https://{installer_host}/v1/tokens"

    # Ensure VCF_INSTALLER_PASSWORD is set for Phase 3
    if not os.environ.get("VCF_INSTALLER_PASSWORD"):
        installer_password = _get_installer_password(config)
        os.environ["VCF_INSTALLER_PASSWORD"] = installer_password
        logger.info("Set VCF_INSTALLER_PASSWORD from Secrets Manager")

    logger.info("Waiting for VCF Installer API at %s (%s) to become ready (up to 40 min)...",
                installer_host, installer_ip)
    deadline = time.time() + 2400  # 40 minutes (VCF 9 firstboot can take 15-30)

    while time.time() < deadline:
        try:
            # verify=False deliberately: this loop polls an appliance still
            # firstbooting, which regenerates its self-signed cert partway
            # through, so the cert seen on a later probe differs from the
            # first — pinning inside the wait loop is self-defeating.
            resp = http_requests.post(
                api_url,
                json={"username": "admin@local",
                      "password": os.environ.get("VCF_INSTALLER_PASSWORD", "probe")},
                verify=False,  # noqa: S501 — intentional: appliance is mid-firstboot, no CA exists yet, cert regenerates during boot (confirmed live)
                timeout=15,
            )
            # 401 = API is up and rejecting bad credentials (ready)
            # 400 = API is up and validating input (ready)
            # 200 = unlikely with wrong password but still means ready
            if resp.status_code in (200, 400, 401):
                logger.info("VCF Installer API is ready (HTTP %d)", resp.status_code)
                return {"status": "ready", "url": api_url, "http_code": resp.status_code}
            # 502/503 = nginx up but backend not ready yet
            logger.info("  Installer not ready yet (HTTP %d), retrying...", resp.status_code)
        except (http_requests.ConnectionError, http_requests.Timeout,
                OSError, ssl.SSLError):
            # The installer isn't listening yet, or DNS for installer_host
            # isn't resolving yet - the normal case for most of this loop.
            logger.info("  Installer not reachable yet, retrying...")
        time.sleep(30)

    raise RuntimeError(f"VCF Installer API at {installer_host} ({installer_ip}) did not become ready within 40 minutes")


def stage_phase3_deploy(config: dict, checkpoint: Checkpoint) -> dict:
    """Run Phase 3: depot, bringup, cleanup, edge cluster, connector."""
    # On --resume/--start-from in a fresh process, stage_esxi_ova_verify (which
    # normally exports VCF_INSTALLER_PASSWORD) is skipped — resolve it from
    # Secrets Manager so Phase 3 never falls back to an interactive prompt.
    if not os.environ.get("VCF_INSTALLER_PASSWORD"):
        try:
            os.environ["VCF_INSTALLER_PASSWORD"] = _get_installer_password(config)
            logger.info("Set VCF_INSTALLER_PASSWORD from Secrets Manager")
        except Exception as e:  # noqa: BLE001
            logger.warning("Could not resolve installer password yet: %s", e)

    _activate_phase3()
    from main import _handle_pipeline

    fqdn = config["dns"]["fqdn"]
    p3 = config["phase3"]
    sddc_mgr = config["hostnames"]["sddc_manager"]

    depot_token = _resolve_depot_token(config)
    if depot_token:
        os.environ["VCF_DEPOT_TOKEN"] = depot_token

    installer_host = f"{sddc_mgr}.{fqdn}"
    nsx_host = f"{config['hostnames']['nsx']}.{fqdn}"
    vcenter_host = f"{config['hostnames']['vcenter']}.{fqdn}"

    # TLS verification strategy:
    # - Pre-bringup: verify=False -- the appliance is still the VCF Installer
    #   with a firstboot self-signed cert, no CA exists yet.
    # - Post-bringup: verify against VMCA's root CA, fetched from SDDC Manager's
    #   chain once bringup completes and VMCA certs are in place.
    # The upgrade runs inside _handle_pipeline via the _upgrade_tls_after_bringup
    # callback, which mutates the shared verify_tls_by_host dict in place.
    verify_tls_by_host: dict[str, bool | str] = {
        installer_host: False,  # noqa: S501 — pre-bringup, no CA exists yet
        nsx_host: False,
        vcenter_host: False,
    }

    def _upgrade_tls_after_bringup() -> None:
        """Called by _handle_pipeline after bringup completes successfully.
        Fetches VMCA CA and upgrades all three hosts to real verification."""
        vmca_bundle = _fetch_vmca_ca_bundle(installer_host)
        if vmca_bundle:
            logger.info("phase3_deploy: VMCA CA bundle obtained from %s — "
                        "upgrading to real certificate verification",
                        installer_host)
            verify_tls_by_host[installer_host] = vmca_bundle
            verify_tls_by_host[nsx_host] = vmca_bundle
            verify_tls_by_host[vcenter_host] = vmca_bundle
        else:
            logger.warning(
                "phase3_deploy: could not obtain VMCA CA bundle after "
                "bringup — post-bringup connections remain unverified")

    args = argparse.Namespace(
        action="deploy-vcf-and-edge",
        installer_host=installer_host,
        installer_username="admin@local",
        installer_password=None,
        depot_token=depot_token,
        depot_username=None,
        depot_password=None,
        target_version=p3["target_version"],
        wait=True,
        bundle_id=None,
        bundle_product_type=None,
        bundle_type=None,
        applicable_for_version=None,
        nsx_manager_host=nsx_host,
        nsx_manager_username="admin",
        nsx_manager_password=None,
        vcenter_host=vcenter_host,
        vcenter_username="administrator@vsphere.local",
        vcenter_password=None,
        cluster_name=None,
        spec_path=None,
        workflow_id=None,
        aws_profile=config["aws"].get("profile"),
        aws_region=config["aws"]["region"],
        no_secrets_manager=False,
        connector_secret_arn=None,
        verify_tls=verify_tls_by_host[installer_host],
        verify_tls_by_host=verify_tls_by_host,
        post_bringup_hook=_upgrade_tls_after_bringup,
        dry_run=False,
    )

    rc = _handle_pipeline(args)
    if rc != 0:
        raise RuntimeError(f"Phase 3 deploy-vcf-and-edge exited with code {rc}")
    return {"status": "complete"}


def _resolve_depot_token(config: dict) -> str | None:
    """Resolve the Broadcom depot token from env var or Secrets Manager."""
    token = os.environ.get("VCF_DEPOT_TOKEN")
    if token:
        return token

    secret_id = config.get("phase3", {}).get("depot_token_secret")
    if not secret_id:
        return None

    session = boto3.Session(
        profile_name=config["aws"].get("profile"),
        region_name=config["aws"]["region"],
    )
    sm = session.client("secretsmanager")
    try:
        resp = sm.get_secret_value(SecretId=secret_id)
        secret = resp["SecretString"]
        try:
            return json.loads(secret).get("token", secret)
        except json.JSONDecodeError:
            return secret
    except sm.exceptions.ResourceNotFoundException:
        logger.warning("Depot token secret '%s' not found in Secrets Manager", secret_id)
        return None
    except Exception as e:
        # AccessDenied / throttling / transient — do NOT silently return None;
        # that makes depot sync fail much later with a confusing symptom that
        # masks the real (fixable/retryable) cause.
        logger.error(
            "Failed to read depot token secret '%s' (%s) — resolve this before "
            "retrying", secret_id, e,
        )
        raise


# ============================================================================
# Stage registry and main orchestration loop
# ============================================================================

# ---------------------------------------------------------------------------
# Stage: aws_config -- build the non-minimal landing zone (bootstrap mode)
#
# When vpc.create: true, deploy a second runtime-generated CFN stack with
# everything past the bootstrap minimum (NAT, EVS SG, key pair, Route 53,
# Route Server, optional TGW). The stack itself is the state -- teardown works
# from any machine even if this runner is replaced; outputs merge into the
# in-memory config on resume and the blueprint file is never modified.
# ---------------------------------------------------------------------------

# blueprint hostnames key -> (default shortname, ip suffix). Mirrors Phase 1.
AWS_CONFIG_COMPONENT_RECORDS = [
    ("vcenter", "vc", "60.10"),
    ("nsx", "nsx", "60.11"),
    ("sddc_manager", "sddcm", "60.12"),
    ("cloud_builder", "cb", "60.13"),
    ("edge01", "edge01", "60.14"),
    ("edge02", "edge02", "60.15"),
    ("nsx01", "nsx01", "60.16"),
    ("nsx02", "nsx02", "60.17"),
    ("nsx03", "nsx03", "60.18"),
    ("vcf_ops", "vcfops", "60.19"),
    ("vcf_ops_01", "vcfops01", "60.20"),
    ("vcf_ops_02", "vcfops02", "60.21"),
    ("vcf_ops_03", "vcfops03", "60.22"),
    ("vcf_ops_collector", "vcfopscol", "60.23"),
    ("vcf_fleet", "vcffleet", "60.24"),
    (None, "vsp-platform", "60.25"),
    (None, "vsp-instance", "60.26"),
    (None, "vsp-fleet", "60.27"),
    (None, "vcf-vidb", "60.28"),
    (None, "vcf-license", "60.29"),
    (None, "vcf-logs", "60.30"),
    (None, "vcf-auto-platform", "60.31"),
    (None, "vcf-auto", "60.32"),
    (None, "vcf-sddcm01", "60.33"),
    (None, "ops-mgr", "60.34"),
    (None, "ops-collector", "60.35"),
    (None, "ops-mgr-replica", "60.36"),
    (None, "ops-mgr-data01", "60.37"),
]

AWS_CONFIG_TAGS = [
    {"Key": "ManagedBy", "Value": "evs-orchestrator"},
    {"Key": "Project", "Value": "evs-vcf9-automation"},
]


def _aws_config_stack_name(config: dict) -> str:
    # Derive from the bootstrap stack name (always unique per CFN constraint)
    # rather than environment_name (which customers can reuse across environments).
    # Naming: <bootstrap-stack-name>-amazon-evs-<vcf-version>-infrastructure
    # (suffix pattern so the two stacks sort together in the CFN console).
    bsn = config.get("aws", {}).get("bootstrap_stack_name")
    if bsn:
        safe = "".join(c if c.isalnum() or c == "-" else "-" for c in bsn).strip("-")
    else:
        # Fallback for non-bootstrap (Terraform/manual) runs that don't have a stack name
        env_name = config["evs"]["environment_name"]
        safe = "".join(c if c.isalnum() or c == "-" else "-" for c in env_name).strip("-")
    version = str(config.get("evs", {}).get("vcf_version", "")).strip()
    version_safe = "".join(c if c.isalnum() else "-" for c in version).strip("-")
    suffix = f"-amazon-evs-{version_safe}-infrastructure" if version_safe else "-amazon-evs-infrastructure"
    # Keep the suffix intact if the combined name exceeds CFN's 128-char limit.
    return f"{safe[:128 - len(suffix)]}{suffix}"


def _aws_config_key_name(config: dict) -> str:
    """Derive the EC2 key pair name for bootstrap mode.

    Uses the bootstrap stack name (guaranteed unique by CloudFormation) to
    avoid collisions when two deployments share the same environment_name.
    Falls back to environment_name for non-bootstrap (Terraform/manual) runs.
    """
    bsn = config.get("aws", {}).get("bootstrap_stack_name")
    if bsn:
        safe = "".join(c if c.isalnum() or c == "-" else "-" for c in bsn).strip("-")
    else:
        env_name = config["evs"]["environment_name"]
        safe = "".join(c if c.isalnum() or c == "-" else "-" for c in env_name).strip("-")
    return f"EVS-KeyPair-{safe}"[:255]


def _aws_config_preflight(config: dict) -> None:
    for required in ("id", "service_access_subnet_id", "service_access_route_table_id",
                     "public_subnet_id", "cidr_prefix"):
        if not config["vpc"].get(required):
            raise RuntimeError(f"aws_config: vpc.{required} missing from blueprint "
                               "(should be set by the bootstrap user data overlay)")


def _build_aws_config_template(config: dict) -> dict:
    """Render the aws_config CloudFormation template from the blueprint."""
    vpc = config["vpc"]
    prefix = vpc["cidr_prefix"]
    fqdn = config["dns"]["fqdn"].rstrip(".")
    octets = prefix.strip(".").split(".")
    reverse_zone_name = f"{octets[1]}.{octets[0]}.in-addr.arpa"
    region = config["aws"]["region"]
    hostnames = config.get("hostnames", {})
    tags = AWS_CONFIG_TAGS

    def named_tags(name):
        return tags + [{"Key": "Name", "Value": name}]

    resources = {
        # --- Route 53 -------------------------------------------------------
        "ForwardZone": {
            "Type": "AWS::Route53::HostedZone",
            "Properties": {
                "Name": fqdn,
                "HostedZoneConfig": {"Comment": "Forward lookup zone for EVS"},
                "VPCs": [{"VPCId": vpc["id"], "VPCRegion": region}],
            },
        },
        "ReverseZone": {
            "Type": "AWS::Route53::HostedZone",
            "Properties": {
                "Name": reverse_zone_name,
                "HostedZoneConfig": {"Comment": "Reverse lookup zone for EVS"},
                "VPCs": [{"VPCId": vpc["id"], "VPCRegion": region}],
            },
        },
        # --- EVS security group ----------------------------------------------
        "EvsSecurityGroup": {
            "Type": "AWS::EC2::SecurityGroup",
            "Properties": {
                "GroupDescription": "EVS service access security group (R53 resolver + EVS hosts)",
                "VpcId": vpc["id"],
                "SecurityGroupIngress": [{
                    "IpProtocol": "-1",
                    "CidrIp": f"{prefix}0.0/16",
                    "Description": "All traffic from VPC CIDR",
                }],
                "Tags": named_tags("EVS-Service-Access-SG"),
            },
        },
        "EvsSgSelfIngress": {
            "Type": "AWS::EC2::SecurityGroupIngress",
            "Properties": {
                "GroupId": {"Fn::GetAtt": ["EvsSecurityGroup", "GroupId"]},
                "SourceSecurityGroupId": {"Fn::GetAtt": ["EvsSecurityGroup", "GroupId"]},
                "IpProtocol": "-1",
                "Description": "All traffic from self",
            },
        },
        # --- Inbound resolver (fixed IPs the DHCP set points at) ------------
        "ResolverEndpoint": {
            "Type": "AWS::Route53Resolver::ResolverEndpoint",
            "Properties": {
                "Name": "R53InboundRslvr",
                "Direction": "INBOUND",
                "SecurityGroupIds": [{"Fn::GetAtt": ["EvsSecurityGroup", "GroupId"]}],
                "IpAddresses": [
                    {"SubnetId": vpc["service_access_subnet_id"], "Ip": f"{prefix}0.100"},
                    {"SubnetId": vpc["service_access_subnet_id"], "Ip": f"{prefix}0.101"},
                ],
            },
        },
        # --- DHCP options (associated only after the resolver exists) -------
        "DhcpOptions": {
            "Type": "AWS::EC2::DHCPOptions",
            "Properties": {
                "DomainName": fqdn,
                "DomainNameServers": [f"{prefix}0.100", f"{prefix}0.101"],
                "NtpServers": ["169.254.169.123"],
                "Tags": named_tags("EVS-DHCP-OpsSet"),
            },
        },
        "DhcpOptionsAssociation": {
            "Type": "AWS::EC2::VPCDHCPOptionsAssociation",
            "DependsOn": "ResolverEndpoint",
            "Properties": {
                "VpcId": vpc["id"],
                "DhcpOptionsId": {"Ref": "DhcpOptions"},
            },
        },
        # --- NAT gateway (egress for VLAN subnets / depot sync) -------------
        "NatEip": {
            "Type": "AWS::EC2::EIP",
            "Properties": {"Domain": "vpc", "Tags": named_tags("EVS-NAT-EIP")},
        },
        "NatGateway": {
            "Type": "AWS::EC2::NatGateway",
            "Properties": {
                "AllocationId": {"Fn::GetAtt": ["NatEip", "AllocationId"]},
                "SubnetId": vpc["public_subnet_id"],
                "ConnectivityType": "public",
                "Tags": named_tags("VPC-NatGW"),
            },
        },
        "ServiceAccessNatRoute": {
            "Type": "AWS::EC2::Route",
            "Properties": {
                "RouteTableId": vpc["service_access_route_table_id"],
                "DestinationCidrBlock": "0.0.0.0/0",
                "NatGatewayId": {"Ref": "NatGateway"},
            },
        },
        # --- EC2 key pair (private key auto-stored in SSM) ------------------
        # KeyName is DERIVED here, not read from config["evs"]["key_name"]: this
        # resource PRODUCES that value (merged back by _apply_aws_config_outputs).
        # Reading from config would be circular -- nothing sets it before now.
        "EvsKeyPair": {
            "Type": "AWS::EC2::KeyPair",
            "Properties": {
                "KeyName": _aws_config_key_name(config),
                "KeyType": "rsa",
                "KeyFormat": "pem",
                "Tags": named_tags(_aws_config_key_name(config)),
            },
        },
        # --- VPC Route Server ------------------------------------------------
        "RouteServer": {
            "Type": "AWS::EC2::RouteServer",
            "Properties": {
                "AmazonSideAsn": 65022,
                "PersistRoutes": "enable",
                "PersistRoutesDuration": 5,
                "Tags": named_tags("EVS-Route-Server"),
            },
        },
        "RouteServerAssociation": {
            "Type": "AWS::EC2::RouteServerAssociation",
            "Properties": {
                "RouteServerId": {"Ref": "RouteServer"},
                "VpcId": vpc["id"],
            },
        },
        "RouteServerEndpoint01": {
            "Type": "AWS::EC2::RouteServerEndpoint",
            "DependsOn": "RouteServerAssociation",
            "Properties": {
                "RouteServerId": {"Ref": "RouteServer"},
                "SubnetId": vpc["service_access_subnet_id"],
                "Tags": named_tags("EVS-RouteServer-Endpoint01"),
            },
        },
        "RouteServerEndpoint02": {
            "Type": "AWS::EC2::RouteServerEndpoint",
            "DependsOn": "RouteServerAssociation",
            "Properties": {
                "RouteServerId": {"Ref": "RouteServer"},
                "SubnetId": vpc["service_access_subnet_id"],
                "Tags": named_tags("EVS-RouteServer-Endpoint02"),
            },
        },
        "RouteServerPeer01": {
            "Type": "AWS::EC2::RouteServerPeer",
            "Properties": {
                "RouteServerEndpointId": {"Ref": "RouteServerEndpoint01"},
                "PeerAddress": f"{prefix}80.10",
                "BgpOptions": {"PeerAsn": 65000, "PeerLivenessDetection": "bgp-keepalive"},
                "Tags": named_tags("EVS-RouteServer-Peer01"),
            },
        },
        "RouteServerPeer02": {
            "Type": "AWS::EC2::RouteServerPeer",
            "Properties": {
                "RouteServerEndpointId": {"Ref": "RouteServerEndpoint02"},
                "PeerAddress": f"{prefix}80.11",
                "BgpOptions": {"PeerAsn": 65000, "PeerLivenessDetection": "bgp-keepalive"},
                "Tags": named_tags("EVS-RouteServer-Peer02"),
            },
        },
        "RouteServerPropagationServiceAccess": {
            "Type": "AWS::EC2::RouteServerPropagation",
            "DependsOn": "RouteServerAssociation",
            "Properties": {
                "RouteServerId": {"Ref": "RouteServer"},
                "RouteTableId": vpc["service_access_route_table_id"],
            },
        },
    }

    if vpc.get("public_subnet_route_table_id"):
        resources["RouteServerPropagationPublic"] = {
            "Type": "AWS::EC2::RouteServerPropagation",
            "DependsOn": "RouteServerAssociation",
            "Properties": {
                "RouteServerId": {"Ref": "RouteServer"},
                "RouteTableId": vpc["public_subnet_route_table_id"],
            },
        }

    if config.get("aws_config", {}).get("create_tgw"):
        resources["TransitGateway"] = {
            "Type": "AWS::EC2::TransitGateway",
            "Properties": {
                "Description": "TGW for EVS",
                "AutoAcceptSharedAttachments": "disable",
                "DefaultRouteTableAssociation": "enable",
                "Tags": named_tags("EVS-TGW"),
            },
        }
        resources["TransitGatewayAttachment"] = {
            "Type": "AWS::EC2::TransitGatewayAttachment",
            "Properties": {
                "TransitGatewayId": {"Ref": "TransitGateway"},
                "VpcId": vpc["id"],
                "SubnetIds": [vpc["service_access_subnet_id"]],
                "Tags": named_tags("EVS-TGW-VPC-Attachment"),
            },
        }

    # --- HCX resources (Network ACL + optional EIP for HCX appliance) --------
    if config.get("hcx", {}).get("enabled"):
        # Only create a CFN EIP when IPAM wasn't used (BYO path without IPAM).
        # When IPAM-provisioned, EIPs are pre-allocated outside CFN.
        if not config.get("hcx", {}).get("eip_allocation_ids"):
            resources["HcxEip"] = {
                "Type": "AWS::EC2::EIP",
                "Properties": {"Domain": "vpc", "Tags": named_tags("EVS-HCX-EIP")},
            }
        resources["HcxNetworkAcl"] = {
            "Type": "AWS::EC2::NetworkAcl",
            "Properties": {
                "VpcId": vpc["id"],
                "Tags": named_tags("EVS-HCX-NACL"),
            },
        }
        # Allow inbound HTTPS (443) and IPSec NAT-T (4500) from anywhere
        for i, (port, proto) in enumerate([(443, "6"), (4500, "17"), (4500, "6")]):
            resources[f"HcxNaclInbound{i}"] = {
                "Type": "AWS::EC2::NetworkAclEntry",
                "Properties": {
                    "NetworkAclId": {"Ref": "HcxNetworkAcl"},
                    "RuleNumber": 100 + i,
                    "Protocol": proto,
                    "PortRange": {"From": port, "To": port},
                    "CidrBlock": "0.0.0.0/0",
                    "Egress": False,
                    "RuleAction": "allow",
                },
            }
        # Allow all outbound
        resources["HcxNaclOutbound"] = {
            "Type": "AWS::EC2::NetworkAclEntry",
            "Properties": {
                "NetworkAclId": {"Ref": "HcxNetworkAcl"},
                "RuleNumber": 100,
                "Protocol": "-1",
                "CidrBlock": "0.0.0.0/0",
                "Egress": True,
                "RuleAction": "allow",
            },
        }

    # --- DNS records (rendered per-blueprint: this is why the template is
    #     generated at runtime rather than shipped statically) ---------------
    records = []
    for i, host in enumerate(hostnames.get("esxi", ["esxi01", "esxi02", "esxi03", "esxi04"])):
        records.append((host, f"10.{11 + i}"))
    for key, default, suffix in AWS_CONFIG_COMPONENT_RECORDS:
        records.append(((hostnames.get(key) if key else None) or default, suffix))

    for n, (host, suffix) in enumerate(records):
        ip = f"{prefix}{suffix}"
        ptr = ".".join(reversed(suffix.split("."))) + f".{octets[1]}.{octets[0]}.in-addr.arpa"
        resources[f"FwdRecord{n:02d}"] = {
            "Type": "AWS::Route53::RecordSet",
            "Properties": {
                "HostedZoneId": {"Ref": "ForwardZone"},
                "Name": f"{host}.{fqdn}.",
                "Type": "A", "TTL": "300",
                "ResourceRecords": [ip],
            },
        }
        resources[f"PtrRecord{n:02d}"] = {
            "Type": "AWS::Route53::RecordSet",
            "Properties": {
                "HostedZoneId": {"Ref": "ReverseZone"},
                "Name": f"{ptr}.",
                "Type": "PTR", "TTL": "300",
                "ResourceRecords": [f"{host}.{fqdn}."],
            },
        }

    # --- Optional Windows jumpbox ----------------------------------------
    # Created only when jumpbox.enabled: true. Public subnet with a public IP for
    # RDP, but the SG ships with NO inbound rules -- customer adds a 3389 rule
    # scoped to their IP (see README). Same VPC = private DNS + routing just work.
    jumpbox = config.get("jumpbox", {}) or {}
    if jumpbox.get("enabled") is True:
        jb_type = str(jumpbox.get("instance_type") or "t3.xlarge")
        # Prefer the RUNNER's key pair so one key covers SSH to the runner AND
        # RDP to the jumpbox (what the README documents). In bootstrap mode the
        # template passes it through as aws.runner_key_name -- the customer's
        # KeyPairName when supplied, else the auto-created <stack>-runner-key.
        #
        # Fall back to EvsKeyPair (the key EVS requires for the ESXi hosts) when
        # runner_key_name is absent: BYO/Terraform runs have no bootstrap stack,
        # and a stack created by an older template won't supply it. That keeps
        # the jumpbox launchable either way instead of failing on an empty
        # KeyName.
        runner_key = str((config.get("aws", {}) or {}).get("runner_key_name") or "").strip()
        jb_key_ref = runner_key if runner_key else {"Ref": "EvsKeyPair"}
        logger.info(
            "Jumpbox key pair: %s",
            f"{runner_key} (shared with the runner)" if runner_key
            else "EVS-KeyPair-<stack> (runner key unavailable — BYO/older template)",
        )
        resources["JumpboxSecurityGroup"] = {
            "Type": "AWS::EC2::SecurityGroup",
            "Properties": {
                "GroupDescription": (
                    "Windows jumpbox - NO inbound rules by default. Add an RDP "
                    "(3389) rule scoped to your IP to connect."
                ),
                "VpcId": vpc["id"],
                "SecurityGroupEgress": [
                    {"IpProtocol": "-1", "CidrIp": "0.0.0.0/0"}
                ],
                "Tags": named_tags("evs-jumpbox-sg"),
            },
        }
        resources["Jumpbox"] = {
            "Type": "AWS::EC2::Instance",
            "Properties": {
                "InstanceType": jb_type,
                # Latest Windows Server 2022 AMI, resolved at deploy time.
                "ImageId": "{{resolve:ssm:/aws/service/ami-windows-latest/Windows_Server-2022-English-Full-Base}}",
                "KeyName": jb_key_ref,  # see jb_key_ref comment above
                "NetworkInterfaces": [{
                    "DeviceIndex": "0",
                    "AssociatePublicIpAddress": True,
                    "SubnetId": vpc["public_subnet_id"],
                    "GroupSet": [{"Fn::GetAtt": ["JumpboxSecurityGroup", "GroupId"]}],
                }],
                "BlockDeviceMappings": [{
                    "DeviceName": "/dev/sda1",
                    "Ebs": {"VolumeSize": 60, "VolumeType": "gp3", "DeleteOnTermination": True},
                }],
                "Tags": named_tags("evs-jumpbox"),
            },
        }

    template = {
        "AWSTemplateFormatVersion": "2010-09-09",
        "Description": "VCF deployment infrastructure (NAT gateway, DNS zones, "
                       "Route Server, security group, key pair). Created automatically "
                       "by the EVS Deployment Orchestrator — do not delete this stack directly.",
        "Resources": resources,
        "Outputs": {
            "SecurityGroupId": {"Value": {"Fn::GetAtt": ["EvsSecurityGroup", "GroupId"]}},
            **({
                "JumpboxInstanceId": {"Value": {"Ref": "Jumpbox"}},
                "JumpboxPublicIp": {"Value": {"Fn::GetAtt": ["Jumpbox", "PublicIp"]}},
                "JumpboxSecurityGroupId": {"Value": {"Fn::GetAtt": ["JumpboxSecurityGroup", "GroupId"]}},
            } if jumpbox.get("enabled") is True else {}),
            "RouteServerEndpoint01Ip": {"Value": {"Fn::GetAtt": ["RouteServerEndpoint01", "EniAddress"]}},
            "RouteServerEndpoint02Ip": {"Value": {"Fn::GetAtt": ["RouteServerEndpoint02", "EniAddress"]}},
            "KeyName": {"Value": {"Ref": "EvsKeyPair"}},
            "NatGatewayId": {"Value": {"Ref": "NatGateway"}},
            "ForwardZoneId": {"Value": {"Ref": "ForwardZone"}},
        },
    }

    if config.get("hcx", {}).get("enabled"):
        # Only output the CFN-created EIP if we didn't use IPAM
        if not config.get("hcx", {}).get("eip_allocation_ids"):
            template["Outputs"]["HcxEipAllocationId"] = {
                "Value": {"Fn::GetAtt": ["HcxEip", "AllocationId"]},
            }
        template["Outputs"]["HcxNetworkAclId"] = {
            "Value": {"Ref": "HcxNetworkAcl"},
        }

    return template


def _aws_config_cfn(config: dict):
    session = boto3.Session(
        profile_name=config["aws"].get("profile"),
        region_name=config["aws"]["region"],
    )
    return session.client("cloudformation")


def _associate_hcx_cidr_with_vpc(config: dict, cidr: str) -> None:
    """Associate a BYO HCX /28 with the VPC as an additional CIDR block.

    The auto-IPAM path does this inside _provision_hcx_ipam, but that function is
    skipped when the customer supplies hcx.public_cidr. EVS requires every VLAN
    CIDR to fall within a VPC CIDR block. Idempotent — an already-associated
    block is left alone.
    """
    region = config["aws"]["region"]
    vpc_id = config["vpc"]["id"]
    session = boto3.Session(
        profile_name=config["aws"].get("profile"),
        region_name=region,
    )
    ec2 = session.client("ec2")

    vpc_detail = ec2.describe_vpcs(VpcIds=[vpc_id])["Vpcs"][0]
    existing = {
        a["CidrBlock"]: (a.get("CidrBlockState", {}) or {}).get("State")
        for a in vpc_detail.get("CidrBlockAssociationSet", [])
    }
    if existing.get(cidr) == "associated":
        logger.info("aws_config: BYO HCX CIDR %s already associated with %s",
                    cidr, vpc_id)
        return
    if cidr in existing:
        logger.info("aws_config: BYO HCX CIDR %s association in state %s — waiting",
                    cidr, existing[cidr])
    else:
        logger.info("aws_config: associating BYO HCX CIDR %s with %s "
                    "(EVS requires VLAN CIDRs to sit inside a VPC CIDR block)",
                    cidr, vpc_id)
        try:
            ec2.associate_vpc_cidr_block(VpcId=vpc_id, CidrBlock=cidr)
        except ec2.exceptions.ClientError as e:
            raise RuntimeError(
                f"Could not associate hcx.public_cidr {cidr} with VPC {vpc_id}: "
                f"{_aws_error_code(e)}. A BYO HCX CIDR must be address space this "
                f"account can associate to a VPC (e.g. an Amazon-provided block "
                f"provisioned into an IPAM pool, or your own BYOIP range), and it "
                f"must not overlap an existing VPC CIDR."
            ) from e

    for _ in range(30):
        time.sleep(5)
        vpc_detail = ec2.describe_vpcs(VpcIds=[vpc_id])["Vpcs"][0]
        assoc = [a for a in vpc_detail.get("CidrBlockAssociationSet", [])
                 if a["CidrBlock"] == cidr]
        if assoc and (assoc[0].get("CidrBlockState", {}) or {}).get("State") == "associated":
            logger.info("aws_config: BYO HCX CIDR %s associated", cidr)
            return
    raise RuntimeError(
        f"Timed out waiting for hcx.public_cidr {cidr} to reach 'associated' on "
        f"VPC {vpc_id}; CreateEnvironment would fail VLAN CIDR validation."
    )


def _provision_hcx_ipam(config: dict) -> dict:
    """Provision IPAM, public /28 CIDR, EIPs, and VPC CIDR association for HCX internet.

    Called when hcx.enabled but no hcx.public_cidr (auto-provision rather than BYO).

    Steps:
      1. Create an IPAM (free tier) if one doesn't already exist.
      2. Create a public IPv4 pool in the IPAM with service=EC2, source=amazon.
      3. Provision a /28 CIDR from the pool (async -- poll until PROVISIONED).
      4. Add the /28 as an additional CIDR on the VPC.
      5. Allocate 3 EIPs from the pool (skip first 2 and last IP as reserved).
      6. Return the CIDR and EIP allocation IDs.

    Idempotent: checks for existing IPAM/pool/CIDR before creating. Tags all
    resources with the stack name for cleanup.

    Returns:
        {"cidr": "x.x.x.x/28", "eip_allocation_ids": [...], "ipam_pool_id": "..."}
    """
    region = config["aws"]["region"]
    vpc_id = config["vpc"]["id"]
    stack_name = _aws_config_stack_name(config)
    tag_name = f"EVS-HCX-{stack_name}"

    session = boto3.Session(
        profile_name=config["aws"].get("profile"),
        region_name=region,
    )
    ec2 = session.client("ec2")

    # 1. Find or create IPAM (limit is often 1 per account, so reuse any existing)
    all_ipams = ec2.describe_ipams().get("Ipams", [])
    active_ipams = [i for i in all_ipams if i.get("State") == "create-complete"]

    if active_ipams:
        ipam_id = active_ipams[0]["IpamId"]
        logger.info("  Reusing existing IPAM: %s", ipam_id)
    else:
        try:
            ipam = ec2.create_ipam(
                OperatingRegions=[{"RegionName": region}],
                TagSpecifications=[{
                    "ResourceType": "ipam",
                    "Tags": [{"Key": "Name", "Value": tag_name}],
                }],
            )["Ipam"]
            ipam_id = ipam["IpamId"]
            logger.info("  Created IPAM: %s", ipam_id)
        except ec2.exceptions.ClientError as e:
            if "ResourceLimitExceeded" in str(e):
                # Account limit reached but describe didn't find one — might be
                # in a non-complete state. Try again without state filter.
                all_ipams = ec2.describe_ipams().get("Ipams", [])
                if all_ipams:
                    ipam_id = all_ipams[0]["IpamId"]
                    logger.info("  Reusing IPAM (limit reached): %s", ipam_id)
                else:
                    raise
            else:
                raise

    # 2. Find or create public IPv4 pool
    scopes = ec2.describe_ipam_scopes(
        Filters=[{"Name": "ipam-id", "Values": [ipam_id]},
                 {"Name": "ipam-scope-type", "Values": ["public"]}]
    )["IpamScopes"]
    public_scope_id = scopes[0]["IpamScopeId"] if scopes else None

    if not public_scope_id:
        # Wait for IPAM to be ready
        for _ in range(30):
            ipam_detail = ec2.describe_ipams(IpamIds=[ipam_id])["Ipams"][0]
            if ipam_detail.get("State") == "create-complete":
                break
            time.sleep(5)
        scopes = ec2.describe_ipam_scopes(
            Filters=[{"Name": "ipam-id", "Values": [ipam_id]},
                     {"Name": "ipam-scope-type", "Values": ["public"]}]
        )["IpamScopes"]
        public_scope_id = scopes[0]["IpamScopeId"]

    # Pool selection. Amazon-provided contiguous public blocks are limited
    # (default 2/region), so prefer a pool that already holds a usable
    # provisioned CIDR over provisioning fresh, in order:
    #   1. this stack's own tagged pool, if its CIDR is provisioned
    #   2. an orphaned EVS-HCX-* pool with a provisioned CIDR not on any VPC
    #      (leaked by a failed run) -- retagged for this stack
    #   3. this stack's own tagged pool without a CIDR (provision below)
    #   4. a brand-new pool
    all_vpc_cidrs = set()
    for _page in ec2.get_paginator("describe_vpcs").paginate():
        for v in _page.get("Vpcs", []):
            for a in v.get("CidrBlockAssociationSet", []):
                if a.get("CidrBlockState", {}).get("State") in ("associated", "associating"):
                    all_vpc_cidrs.add(a["CidrBlock"])

    def _provisioned_cidrs(pool_id_):
        return [x["Cidr"] for x in ec2.get_ipam_pool_cidrs(
            IpamPoolId=pool_id_).get("IpamPoolCidrs", [])
            if x.get("State") == "provisioned"]

    _all_pools = []
    for _page in ec2.get_paginator("describe_ipam_pools").paginate():
        _all_pools.extend(_page.get("IpamPools", []))

    own_pool = None
    orphan_pool = None
    for cand in _all_pools:
        if cand.get("State") != "create-complete":
            continue
        name = next((t["Value"] for t in cand.get("Tags", []) if t["Key"] == "Name"), "")
        if name == tag_name:
            own_pool = cand
        elif name.startswith("EVS-HCX-") and orphan_pool is None:
            # Orphan = provisioned CIDR not associated with any VPC
            if any(x not in all_vpc_cidrs for x in _provisioned_cidrs(cand["IpamPoolId"])):
                orphan_pool = cand

    # Own pool is usable with ANY provisioned CIDR (on resume it may already
    # be associated with our VPC — step 4 handles both cases idempotently).
    if own_pool and _provisioned_cidrs(own_pool["IpamPoolId"]):
        pool_id = own_pool["IpamPoolId"]
        logger.info("  HCX IPAM pool already exists with a usable CIDR: %s", pool_id)
    elif orphan_pool is not None:
        pool_id = orphan_pool["IpamPoolId"]
        logger.info(
            "  Reclaiming orphaned HCX IPAM pool %s (its provisioned CIDR is "
            "unused; retagging for this stack)", pool_id,
        )
        ec2.create_tags(Resources=[pool_id],
                        Tags=[{"Key": "Name", "Value": tag_name}])
        # Retag this stack's empty/failed pool out of the way so it isn't
        # matched next run (it holds no provisioned CIDR).
        if own_pool:
            ec2.create_tags(Resources=[own_pool["IpamPoolId"]],
                            Tags=[{"Key": "Name", "Value": f"{tag_name}-superseded"}])
    elif own_pool:
        pool_id = own_pool["IpamPoolId"]
        logger.info("  HCX IPAM pool already exists: %s", pool_id)
    else:
        pool = ec2.create_ipam_pool(
            IpamScopeId=public_scope_id,
            AddressFamily="ipv4",
            Locale=region,
            PublicIpSource="amazon",
            AwsService="ec2",
            TagSpecifications=[{
                "ResourceType": "ipam-pool",
                "Tags": [{"Key": "Name", "Value": tag_name}],
            }],
        )["IpamPool"]
        pool_id = pool["IpamPoolId"]
        logger.info("  Created IPAM pool: %s", pool_id)
        # Wait for pool to be ready
        for _ in range(30):
            pool_state = ec2.describe_ipam_pools(
                IpamPoolIds=[pool_id]
            )["IpamPools"][0].get("State")
            if pool_state == "create-complete":
                break
            time.sleep(5)

    # 3. Provision /28 CIDR from pool (if not already provisioned)
    existing_cidrs = ec2.get_ipam_pool_cidrs(IpamPoolId=pool_id).get("IpamPoolCidrs", [])
    provisioned = [c for c in existing_cidrs if c.get("State") == "provisioned"]

    if provisioned:
        cidr = provisioned[0]["Cidr"]
        logger.info("  HCX CIDR already provisioned: %s", cidr)
    else:
        ec2.provision_ipam_pool_cidr(IpamPoolId=pool_id, NetmaskLength=28)
        logger.info("  Provisioning /28 CIDR from IPAM pool (may take 1-2 min)...")
        cidr = None
        for _ in range(60):
            time.sleep(5)
            cidrs = ec2.get_ipam_pool_cidrs(IpamPoolId=pool_id).get("IpamPoolCidrs", [])
            prov = [c for c in cidrs if c.get("State") == "provisioned"]
            if prov:
                cidr = prov[0]["Cidr"]
                break
            failed = [c for c in cidrs if c.get("State") == "failed-provision"]
            if failed:
                reason = (failed[0].get("FailureReason") or {}).get(
                    "Message", "no reason reported")
                raise RuntimeError(
                    f"HCX IPAM: /28 CIDR provisioning FAILED: {reason}. "
                    f"Amazon-provided contiguous public blocks are limited "
                    f"(default 2 per region) — free one by tearing down an old "
                    f"HCX deployment (disassociate the /28 from its VPC, "
                    f"deprovision it from its IPAM pool, delete the pool), or "
                    f"request a limit increase, or set hcx.public_cidr to bring "
                    f"your own /28 instead."
                )
        if not cidr:
            raise RuntimeError("HCX IPAM: timed out waiting for /28 CIDR provisioning")
        logger.info("  HCX CIDR provisioned: %s", cidr)

    # 4. Add CIDR to VPC (if not already associated)
    vpc_detail = ec2.describe_vpcs(VpcIds=[vpc_id])["Vpcs"][0]
    vpc_cidrs = [a["CidrBlock"] for a in vpc_detail.get("CidrBlockAssociationSet", [])]
    if cidr not in vpc_cidrs:
        ec2.associate_vpc_cidr_block(VpcId=vpc_id, CidrBlock=cidr)
        logger.info("  Associated %s as additional VPC CIDR", cidr)
        # Wait for association
        for _ in range(30):
            time.sleep(5)
            vpc_detail = ec2.describe_vpcs(VpcIds=[vpc_id])["Vpcs"][0]
            assoc = [a for a in vpc_detail.get("CidrBlockAssociationSet", [])
                     if a["CidrBlock"] == cidr]
            if assoc and assoc[0].get("CidrBlockState", {}).get("State") == "associated":
                break
    else:
        logger.info("  VPC already has CIDR %s", cidr)

    # 5. Allocate the EIPs from inside the /28 (shared with the BYO-CIDR path).
    eip_allocation_ids = _allocate_hcx_eips_in_cidr(
        ec2, cidr, tag_name, ipam_pool_id=pool_id,
    )

    return {
        "cidr": cidr,
        "eip_allocation_ids": eip_allocation_ids,
        "ipam_pool_id": pool_id,
    }


def _resolve_ipam_pool_for_cidr(ec2, cidr: str) -> str | None:
    """Return the IPAM pool that owns cidr, or None if no pool provisioned it.

    A bring-your-own HCX CIDR normally comes from an IPAM pool the customer
    provisioned, but they only tell us the CIDR, not the pool. EIPs inside that
    range can only be allocated by naming the owning pool, so look it up.
    Returns None for a CIDR that isn't IPAM-backed (e.g. plain BYOIP), which the
    caller handles by allocating on address alone.
    """
    try:
        pools = ec2.describe_ipam_pools().get("IpamPools", [])
    except Exception as e:  # noqa: BLE001 — best-effort lookup
        logger.info("  Could not list IPAM pools (%s); will allocate by address only", e)
        return None
    for p in pools:
        if p.get("AddressFamily") != "ipv4":
            continue
        pid = p.get("IpamPoolId")
        try:
            entries = ec2.get_ipam_pool_cidrs(IpamPoolId=pid).get("IpamPoolCidrs", [])
        except Exception:  # noqa: BLE001
            continue
        if any(c.get("Cidr") == cidr for c in entries):
            logger.info("  BYO HCX CIDR %s is owned by IPAM pool %s", cidr, pid)
            return pid
    logger.info("  No IPAM pool owns %s — allocating by address only", cidr)
    return None


def _allocate_hcx_eips_in_cidr(ec2, cidr: str, tag_name: str,
                               ipam_pool_id: str | None = None) -> list[str]:
    """Allocate the 3 HCX EIPs (Manager/IX/NE) from inside cidr.

    EVS requires every HCX EIP to fall within the HCX VLAN CIDR and rejects
    AssociateEipToVlan otherwise ("IP address X is not within the VLAN CIDR
    block"), so these can never come from the standard Amazon pool. Shared by
    the auto-IPAM path and the BYO-CIDR path -- while only auto allocated them,
    BYO silently got a standard-pool address and failed at association.

    EVS reserves the first two addresses and the last, so allocation starts at
    .3. Tags each EIP Name=<tag_name>-<role>, which is what teardown matches to
    release them.
    """
    import ipaddress
    network = ipaddress.IPv4Network(cidr)
    # .0=network, .1=gateway(reserved), .2=reserved, last=broadcast
    usable_ips = [str(network.network_address + i) for i in range(3, 6)]
    labels = ["manager", "ix", "ne"]

    # Reuse ANY unassociated EIP inside this CIDR, not just ones carrying this
    # stack's tags — a previous deployment of the same /28 leaves its EIPs
    # behind, and re-allocating those addresses fails with an overlap error.
    existing_alloc_ids: list[str] = []
    for e in ec2.describe_addresses().get("Addresses", []):
        try:
            in_cidr = ipaddress.IPv4Address(e.get("PublicIp", "0.0.0.0")) in network
        except ValueError:
            in_cidr = False
        if in_cidr and not e.get("AssociationId") and len(existing_alloc_ids) < 3:
            existing_alloc_ids.append(e["AllocationId"])
            ec2.create_tags(Resources=[e["AllocationId"]], Tags=[{
                "Key": "Name",
                "Value": f"{tag_name}-{labels[len(existing_alloc_ids) - 1]}",
            }])
            logger.info("  Reusing existing HCX EIP %s (%s)",
                        e["PublicIp"], labels[len(existing_alloc_ids) - 1])

    if len(existing_alloc_ids) >= 3:
        logger.info("  HCX EIPs already allocated: %s", existing_alloc_ids[:3])
        return existing_alloc_ids[:3]

    eip_allocation_ids = list(existing_alloc_ids)
    for i, ip in enumerate(usable_ips):
        if i < len(existing_alloc_ids):
            continue
        kwargs = {
            "Domain": "vpc",
            "Address": ip,
            "TagSpecifications": [{
                "ResourceType": "elastic-ip",
                "Tags": [{"Key": "Name", "Value": f"{tag_name}-{labels[i]}"}],
            }],
        }
        if ipam_pool_id:
            kwargs["IpamPoolId"] = ipam_pool_id
        try:
            resp = ec2.allocate_address(**kwargs)
        except ec2.exceptions.ClientError as e:
            raise RuntimeError(
                f"Could not allocate HCX EIP {ip} from {cidr}: "
                f"{_aws_error_code(e)}. EVS requires each HCX EIP to sit inside "
                f"the HCX VLAN CIDR, so this address space must be allocatable in "
                f"this account — an Amazon-provided range provisioned into an IPAM "
                f"pool, or your own BYOIP range."
            ) from e
        eip_allocation_ids.append(resp["AllocationId"])
        logger.info("  Allocated HCX EIP: %s (%s)", resp["PublicIp"], labels[i])

    return eip_allocation_ids


def stage_aws_config(config: dict, checkpoint: Checkpoint) -> dict:
    """Deploy the aws_config CloudFormation stack (bootstrap mode only).

    Runs whenever --vpc-create was passed at all (bootstrap-create OR
    bootstrap-BYO): both need the landing zone created inside vpc.id. Only true
    non-bootstrap (Phase 1 Terraform / manual) mode skips this. Create-or-update
    semantics make re-runs and resume safe; the stack is the durable state.
    """
    if not config.get("_bootstrap_mode"):
        logger.info("aws_config: not running under the CFN bootstrap (--vpc-create "
                    "not passed) — skipping (Phase 1 Terraform / manual landing zone)")
        return {"skipped": True}

    _aws_config_preflight(config)

    # --- HCX internet connectivity: provision IPAM + public /28 if needed ---
    hcx = config.get("hcx", {})
    if hcx.get("enabled") and not hcx.get("public_cidr"):
        logger.info("aws_config: HCX internet enabled without BYO public_cidr — "
                    "provisioning IPAM and /28 public CIDR automatically")
        hcx_result = _provision_hcx_ipam(config)
        hcx["public_cidr"] = hcx_result["cidr"]
        hcx["eip_allocation_ids"] = hcx_result["eip_allocation_ids"]
        hcx["ipam_pool_id"] = hcx_result["ipam_pool_id"]
        config["hcx"] = hcx
        logger.info("aws_config: HCX IPAM provisioned — CIDR %s, %d EIPs",
                    hcx["public_cidr"], len(hcx["eip_allocation_ids"]))
    elif hcx.get("enabled") and hcx.get("public_cidr"):
        # BYO path skips _provision_hcx_ipam, including the VPC CIDR association
        # it does at step 4 and the in-CIDR EIP allocation at step 5. EVS requires
        # VLAN CIDRs to sit inside a VPC CIDR block, AND each HCX EIP to sit inside
        # the VLAN CIDR, so both must happen here too — otherwise the infra stack
        # falls back to a standard-pool EIP and AssociateEipToVlan fails with
        # "IP address X is not within the VLAN CIDR block".
        _associate_hcx_cidr_with_vpc(config, hcx["public_cidr"])
        _byo_cidr = hcx["public_cidr"]
        _byo_ec2 = boto3.Session(
            profile_name=config["aws"].get("profile"),
            region_name=config["aws"]["region"],
        ).client("ec2")
        hcx["eip_allocation_ids"] = _allocate_hcx_eips_in_cidr(
            _byo_ec2, _byo_cidr,
            f"EVS-HCX-{_aws_config_stack_name(config)}",
            ipam_pool_id=_resolve_ipam_pool_for_cidr(_byo_ec2, _byo_cidr),
        )
        config["hcx"] = hcx
        logger.info("aws_config: BYO HCX CIDR %s — allocated %d EIP(s) inside it",
                    _byo_cidr, len(hcx["eip_allocation_ids"]))

    cfn = _aws_config_cfn(config)
    stack_name = _aws_config_stack_name(config)
    template_body = json.dumps(_build_aws_config_template(config), separators=(",", ":"))
    logger.info("aws_config: deploying stack %s (%d KB template)",
                stack_name, len(template_body) // 1024)

    status = None
    try:
        status = cfn.describe_stacks(StackName=stack_name)["Stacks"][0]["StackStatus"]
    except cfn.exceptions.ClientError as e:
        # Only a genuine "stack doesn't exist" ValidationError means status=None
        # (proceed to create) is correct. Any other code (throttling, AccessDenied,
        # transient describe failure) must NOT be treated as "no stack" -- that
        # would create_stack over a healthy stack or mask a real permissions error.
        code = _aws_error_code(e)
        if code == "ValidationError" and "does not exist" in str(e):
            pass  # genuinely does not exist yet — proceed to create
        else:
            raise

    if status in ("ROLLBACK_COMPLETE", "ROLLBACK_FAILED", "CREATE_FAILED", "DELETE_FAILED"):
        logger.info("aws_config: stack in %s — deleting before re-create", status)
        cfn.delete_stack(StackName=stack_name)
        cfn.get_waiter("stack_delete_complete").wait(StackName=stack_name)
        status = None
    elif status == "CREATE_IN_PROGRESS":
        # A previous run was killed mid-create (e.g. runner replaced, process
        # killed). CloudFormation rejects a second create_stack outright -
        # wait on the in-flight operation instead of issuing a new call.
        logger.info("aws_config: stack already CREATE_IN_PROGRESS from an "
                    "earlier run — waiting for it to finish instead of "
                    "starting a new create")
        cfn.get_waiter("stack_create_complete").wait(
            StackName=stack_name, WaiterConfig={"Delay": 30, "MaxAttempts": 80})
        status = cfn.describe_stacks(StackName=stack_name)["Stacks"][0]["StackStatus"]
    elif status is not None and status.endswith("_IN_PROGRESS"):
        logger.info("aws_config: stack already %s from an earlier run — "
                    "waiting for it to settle before continuing", status)
        cfn.get_waiter("stack_update_complete").wait(
            StackName=stack_name, WaiterConfig={"Delay": 30, "MaxAttempts": 80})
        status = cfn.describe_stacks(StackName=stack_name)["Stacks"][0]["StackStatus"]

    if status is None:
        # NOTE: no stack-level Tags — CloudFormation propagates them to every
        # resource, colliding with the identical per-resource tags (the
        # RouteServer handler rejects duplicate tag keys).
        cfn.create_stack(StackName=stack_name, TemplateBody=template_body)
        logger.info("aws_config: stack create initiated — waiting (5-10 min)")
        cfn.get_waiter("stack_create_complete").wait(
            StackName=stack_name, WaiterConfig={"Delay": 30, "MaxAttempts": 80})
    else:
        try:
            cfn.update_stack(StackName=stack_name, TemplateBody=template_body)
            logger.info("aws_config: stack update initiated — waiting")
            cfn.get_waiter("stack_update_complete").wait(
                StackName=stack_name, WaiterConfig={"Delay": 30, "MaxAttempts": 80})
        except cfn.exceptions.ClientError as e:
            if "No updates are to be performed" not in str(e):
                raise
            logger.info("aws_config: stack already up to date")

    outputs = {o["OutputKey"]: o["OutputValue"]
               for o in cfn.describe_stacks(StackName=stack_name)["Stacks"][0].get("Outputs", [])}
    result = {
        "stack_name": stack_name,
        "security_group_id": outputs.get("SecurityGroupId"),
        "route_server_endpoint_01_ip": outputs.get("RouteServerEndpoint01Ip"),
        "route_server_endpoint_02_ip": outputs.get("RouteServerEndpoint02Ip"),
        "key_name": outputs.get("KeyName"),
        "nat_gateway_id": outputs.get("NatGatewayId"),
        "forward_zone_id": outputs.get("ForwardZoneId"),
        "hcx_eip_allocation_id": outputs.get("HcxEipAllocationId"),
        "hcx_network_acl_id": outputs.get("HcxNetworkAclId"),
        # IPAM-provisioned HCX values come from _provision_hcx_ipam, not a CFN
        # output (only the BYO-CIDR path emits HcxEipAllocationId). Persist them
        # into the CHECKPOINTED result so a fresh-process --resume can recover
        # them -- they're never written to the blueprint (would invalidate the
        # config hash), so otherwise generate_config falls back to the wrong CIDR
        # and crashes with KeyError 'eip_allocation_id'.
        "hcx_public_cidr": hcx.get("public_cidr") if hcx.get("ipam_pool_id") else None,
        "hcx_eip_allocation_ids": hcx.get("eip_allocation_ids"),
        "hcx_ipam_pool_id": hcx.get("ipam_pool_id"),
    }
    for required_out in ("security_group_id", "route_server_endpoint_01_ip",
                         "route_server_endpoint_02_ip", "key_name"):
        if not result.get(required_out):
            raise RuntimeError(f"aws_config: stack output missing {required_out}")

    _apply_aws_config_outputs(config, result)

    # Wait for the new private zone to be resolvable from this instance so the
    # validate_dns stage doesn't fail on propagation lag (usually seconds).
    import socket
    probe = f"{config.get('hostnames', {}).get('sddc_manager', 'sddcm')}.{config['dns']['fqdn']}"
    for attempt in range(30):
        try:
            socket.gethostbyname(probe)
            break
        except socket.gaierror:
            if attempt == 29:
                logger.warning("aws_config: %s still not resolving after 5 min — "
                               "validate_dns may fail; check VPC DNS settings", probe)
            time.sleep(10)

    logger.info("aws_config: landing zone complete — SG %s, RouteServer endpoints %s / %s",
                result["security_group_id"], result["route_server_endpoint_01_ip"],
                result["route_server_endpoint_02_ip"])
    return result


def _apply_aws_config_outputs(config: dict, result: dict) -> None:
    """Merge aws_config outputs into the in-memory config for later stages.

    Never writes to the blueprint file (would invalidate the checkpoint's
    config hash). Called both when the stage runs and on resume.
    """
    if not result or result.get("skipped"):
        return
    vpc = config.setdefault("vpc", {})
    if result.get("security_group_id"):
        vpc["security_group_id"] = result["security_group_id"]
    if result.get("route_server_endpoint_01_ip"):
        vpc["route_server_endpoint_01_ip"] = result["route_server_endpoint_01_ip"]
    if result.get("route_server_endpoint_02_ip"):
        vpc["route_server_endpoint_02_ip"] = result["route_server_endpoint_02_ip"]
    if result.get("key_name"):
        config.setdefault("evs", {})["key_name"] = result["key_name"]
    # HCX outputs (only when hcx.enabled). Two paths:
    # (a) BYO-CIDR: HcxEip CFN resource -> HcxEipAllocationId stack output.
    # (b) IPAM auto-provisioned: no CFN output, so stage_aws_config persisted the
    #     values into `result` (hcx_public_cidr/eip_allocation_ids/ipam_pool_id)
    #     so a fresh-process --resume can restore them here.
    if result.get("hcx_eip_allocation_id"):
        config.setdefault("hcx", {})["eip_allocation_id"] = result["hcx_eip_allocation_id"]
    elif result.get("hcx_eip_allocation_ids"):
        hcx_cfg = config.setdefault("hcx", {})
        hcx_cfg["eip_allocation_ids"] = result["hcx_eip_allocation_ids"]
        hcx_cfg["eip_allocation_id"] = result["hcx_eip_allocation_ids"][0]
        if result.get("hcx_public_cidr"):
            hcx_cfg["public_cidr"] = result["hcx_public_cidr"]
        if result.get("hcx_ipam_pool_id"):
            hcx_cfg["ipam_pool_id"] = result["hcx_ipam_pool_id"]
    elif config.get("hcx", {}).get("eip_allocation_ids"):
        # Same-process fallback (stage just ran in THIS process — config
        # already has the values from stage_aws_config's own assignment).
        config["hcx"]["eip_allocation_id"] = config["hcx"]["eip_allocation_ids"][0]
    if result.get("hcx_network_acl_id"):
        config.setdefault("hcx", {})["network_acl_id"] = result["hcx_network_acl_id"]


STAGES = [
    ("aws_config", stage_aws_config),
    ("generate_config", stage_generate_config),
    ("validate_dns", stage_validate_dns),
    ("phase2_deploy", stage_phase2_deploy),
    ("esxi_vlan_tag", stage_esxi_vlan_tag),
    ("esxi_vmfs_create", stage_esxi_vmfs_create),
    ("esxi_ova_deploy", stage_esxi_ova_deploy),
    ("esxi_ova_verify", stage_esxi_ova_verify),
    ("phase3_deploy", stage_phase3_deploy),
]

STAGE_IDS = [s[0] for s in STAGES]

ESXI_PREWORK_STAGES = {"esxi_vlan_tag", "esxi_vmfs_create", "esxi_ova_deploy", "esxi_ova_verify"}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="EVS + VCF9 Full Deployment Orchestrator",
    )
    parser.add_argument(
        "--config",
        required=True,
        help="Path to blueprint.yaml",
    )
    parser.add_argument(
        "--resume",
        action="store_true",
        default=False,
        help="Resume from last checkpoint, skipping completed stages",
    )
    parser.add_argument(
        "--start-from",
        default=None,
        choices=STAGE_IDS,
        help="Start (or resume) from this specific stage",
    )
    parser.add_argument(
        "--skip-prework",
        action="store_true",
        default=False,
        help="Skip ESXi pre-work stages (VLAN tag, VMFS, OVA deploy, verify)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Validate config and show what would run without executing",
    )
    parser.add_argument(
        "--destroy",
        action="store_true",
        default=False,
        help="Tear down the EVS environment: delete hosts, EBS volume, secrets, and environment",
    )
    parser.add_argument(
        "--vpc-create",
        default=None,
        choices=["true", "false"],
        help=(
            "Whether the aws_config stage should build a new landing zone "
            "(DNS/NAT/security group/Route Server) inside vpc.id. Passed "
            "directly by the bootstrap CFN user data from its own "
            "CreateNetwork condition - never set this by hand or infer it "
            "from vpc.id (vpc.id is always non-empty by the time this "
            "script runs in bootstrap mode, whether newly created or "
            "customer-supplied, so its presence can't tell the two apart). "
            "Omit entirely for non-bootstrap (Phase 1 Terraform) runs."
        ),
    )
    return parser.parse_args(argv)


def _normalize_config(config: dict, vpc_create: bool | None = None) -> None:
    """Derive/rename fields so the rest of the orchestrator sees one stable
    internal schema regardless of the customer-facing blueprint. Mutates config
    in place; must run immediately after load, before anything reads these.

    - aws.region: derived from aws.availability_zone (strip trailing AZ letter).
    - vpc.create: from the --vpc-create flag, NOT inferred from vpc.id. In
      bootstrap mode vpc.id is always non-empty (new OR customer-supplied), so
      only CloudFormation's CreateNetwork condition, passed through as
      --vpc-create, can tell the two apart. Defaults to False if never passed.
    - config["_bootstrap_mode"]: whether --vpc-create was passed AT ALL, distinct
      from vpc["create"]. Needed because bootstrap-BYO (--vpc-create false) and
      Terraform BYO (never passed) both give vpc.create=False but validate
      differently: bootstrap-BYO still runs aws_config to create the landing zone
      and gets key_name/security_group_id/route_server_*_ip as OUTPUTS, whereas
      Terraform/manual needs them pre-supplied. _validate_config and
      stage_aws_config both key off _bootstrap_mode, not vpc.create alone.
    - evs.vcf_version <-> vcf_installer_product_version: keep both populated
      (internal code still keys on the old name).
    - phase3.target_version: derived from evs.vcf_version (e.g. "9.0.2.0" -> "9.0.2").
    """
    aws_cfg = config.setdefault("aws", {})
    az = aws_cfg.get("availability_zone")
    if az and not aws_cfg.get("region"):
        aws_cfg["region"] = az[:-1] if az and az[-1].isalpha() else az

    config["_bootstrap_mode"] = vpc_create is not None

    vpc = config.setdefault("vpc", {})
    vpc["create"] = bool(vpc_create)

    evs = config.setdefault("evs", {})
    # Normalize vcf_version to the full 4-part form the VCF installer
    # requires on the bringup spec. The installer's release catalog keys on
    # e.g. "9.1.0.0" — a 3-part "9.1.0" fails bringup validation with
    # "No release data found for version 9.1.0".
    for key in ("vcf_version", "vcf_installer_product_version"):
        v = evs.get(key)
        if v and re.match(r"^\d+\.\d+\.\d+$", str(v).strip()):
            evs[key] = f"{str(v).strip()}.0"
    if evs.get("vcf_version") and not evs.get("vcf_installer_product_version"):
        evs["vcf_installer_product_version"] = evs["vcf_version"]
    elif evs.get("vcf_installer_product_version") and not evs.get("vcf_version"):
        evs["vcf_version"] = evs["vcf_installer_product_version"]

    phase3 = config.setdefault("phase3", {})
    # phase3.ntp: accept a single server as a bare string and normalize it
    # to a one-element list so downstream code can always iterate it.
    if isinstance(phase3.get("ntp"), str):
        phase3["ntp"] = [phase3["ntp"]]
    if not phase3.get("target_version") and evs.get("vcf_version"):
        # "9.0.2.0" -> "9.0.2" (bundle filter uses major.minor.patch only)
        parts = evs["vcf_version"].split(".")
        phase3["target_version"] = ".".join(parts[:3]) if len(parts) >= 3 else evs["vcf_version"]


def _validate_config(config: dict):
    """Fail fast if required config keys are missing.

    In bootstrap mode (--vpc-create passed at all) the SG, key pair, and route
    server IPs are OUTPUTS of the aws_config stage, so they're validated after
    it runs; this mode instead requires vpc.public_subnet_id (for the NAT
    gateway). Includes bootstrap-BYO, which still builds the landing zone inside
    the existing VPC. Only true non-bootstrap (Phase 1 Terraform / manual) mode
    has no aws_config stage and needs those fields pre-supplied in the blueprint.
    """
    bootstrap_mode = bool(config.get("_bootstrap_mode"))

    required_paths = [
        ("aws.region", config.get("aws", {}).get("region")),
        ("vpc.cidr_prefix", config.get("vpc", {}).get("cidr_prefix")),
        ("vpc.service_access_subnet_id", config.get("vpc", {}).get("service_access_subnet_id")),
        ("vpc.service_access_route_table_id", config.get("vpc", {}).get("service_access_route_table_id")),
        ("dns.fqdn", config.get("dns", {}).get("fqdn")),
        ("evs.environment_name", config.get("evs", {}).get("environment_name")),
        ("evs.instance_type", config.get("evs", {}).get("instance_type")),
        ("evs.vcf_version", config.get("evs", {}).get("vcf_version")),
        ("evs.terms_accepted", config.get("evs", {}).get("terms_accepted") is not None),
        ("evs.simple_deployment", config.get("evs", {}).get("simple_deployment") is not None),
        ("hostnames.esxi", config.get("hostnames", {}).get("esxi")),
        ("hostnames.vcenter", config.get("hostnames", {}).get("vcenter")),
        ("hostnames.nsx", config.get("hostnames", {}).get("nsx")),
        ("hostnames.sddc_manager", config.get("hostnames", {}).get("sddc_manager")),
        ("sizing.vcenter_size", config.get("sizing", {}).get("vcenter_size")),
        ("sizing.vcenter_storage_size", config.get("sizing", {}).get("vcenter_storage_size")),
        ("sizing.nsx_size", config.get("sizing", {}).get("nsx_size")),
        ("sizing.operations_appliance_size", config.get("sizing", {}).get("operations_appliance_size")),
        ("sizing.operations_collector_appliance_size",
         config.get("sizing", {}).get("operations_collector_appliance_size")),
        # phase3.depot_token_secret is injected from the DepotSecretName CFN
        # parameter — not required in the blueprint (but still validated if present)
    ]
    if bootstrap_mode:
        required_paths.extend([
            ("vpc.public_subnet_id", config.get("vpc", {}).get("public_subnet_id")),
            ("aws.bootstrap_stack_name", config.get("aws", {}).get("bootstrap_stack_name")),
        ])
    else:
        # Non-bootstrap (Phase 1 Terraform) mode: vpc.id, key_name, and the
        # security group / route server IPs are all customer/Terraform-
        # provided up front, not auto-injected at runtime.
        required_paths.extend([
            ("vpc.id", config.get("vpc", {}).get("id")),
            ("evs.key_name", config.get("evs", {}).get("key_name")),
            ("vpc.security_group_id", config.get("vpc", {}).get("security_group_id")),
            ("vpc.route_server_endpoint_01_ip", config.get("vpc", {}).get("route_server_endpoint_01_ip")),
            ("vpc.route_server_endpoint_02_ip", config.get("vpc", {}).get("route_server_endpoint_02_ip")),
        ])

    missing = [name for name, val in required_paths if not val]
    if missing:
        raise ValueError(f"Missing required config values: {', '.join(missing)}")

    # --- Format and value validation (fail fast on bad parameters) ---
    errors = []
    evs = config.get("evs", {})
    vpc = config.get("vpc", {})
    dns_cfg = config.get("dns", {})
    sizing = config.get("sizing", {})
    hostnames_cfg = config.get("hostnames", {})

    # vpc.cidr_prefix: must be "X.Y." (two octets 0-255 with trailing dot)
    cidr_prefix = vpc.get("cidr_prefix", "")
    if cidr_prefix:
        m = re.match(r"^(\d{1,3})\.(\d{1,3})\.$", cidr_prefix)
        if not m or int(m.group(1)) > 255 or int(m.group(2)) > 255:
            errors.append(f"vpc.cidr_prefix must be two octets (0-255) with trailing dot (e.g. '10.20.'), got: '{cidr_prefix}'")

    # dns.fqdn: valid domain name — must have at least one dot, no trailing dot,
    # only letters/digits/hyphens per label, no leading/trailing hyphen per label.
    fqdn = dns_cfg.get("fqdn", "").strip()
    if fqdn and (
        "." not in fqdn
        or fqdn.endswith(".")
        or not re.match(r"^[a-zA-Z0-9]([a-zA-Z0-9\-]*[a-zA-Z0-9])?(\.[a-zA-Z0-9]([a-zA-Z0-9\-]*[a-zA-Z0-9])?)+$", fqdn)
    ):
        errors.append(f"dns.fqdn must be a valid domain name with at least one dot, got: '{fqdn}'")

    # evs.instance_type: must be a supported bare-metal type
    instance_type = evs.get("instance_type", "")
    # Mirrors the InstanceType enum in the EVS API model. i7i.metal-48xl is
    # NOT an EVS-supported type despite existing in EC2 -- CreateEnvironment
    # rejects it, so catch it here rather than ~50 minutes in.
    supported_instance_types = {"i4i.metal", "i7i.metal-24xl"}
    if instance_type and instance_type not in supported_instance_types:
        errors.append(f"evs.instance_type must be one of {sorted(supported_instance_types)}, got: '{instance_type}'")

    # evs.vcf_version: must match X.Y.Z.W pattern AND start with a supported
    # major.minor prefix. New versions require code changes (bundle pins,
    # OVA, spec builder), so rejecting unknown versions here prevents a
    # multi-hour failure deep in phase 3.
    supported_vcf_prefixes = ("9.0.2", "9.1.0")
    vcf_version = evs.get("vcf_version", "").strip()
    if vcf_version and not re.match(r"^\d+\.\d+\.\d+(\.\d+)?$", vcf_version):
        errors.append(f"evs.vcf_version must be a version like '9.0.2.0' or '9.1.0.0', got: '{vcf_version}'")
    elif vcf_version and not any(vcf_version.startswith(p) for p in supported_vcf_prefixes):
        errors.append(
            f"evs.vcf_version '{vcf_version}' is not a supported version. "
            f"Supported prefixes: {list(supported_vcf_prefixes)}. "
            f"A new version requires updated bundle pins and OVA."
        )

    # evs.esx_version: optional pin for the exact ESXi build; if omitted, phase2
    # auto-resolves the newest for evs.vcf_version's major.minor. Format-check
    # only here -- whether the build is offered for the chosen instance_type is
    # checked live against the get-versions catalog in phase2 (needs credentials).
    esx_version = evs.get("esx_version", "").strip() if evs.get("esx_version") else ""
    if esx_version and not esx_version.startswith("ESXi-"):
        errors.append(
            f"evs.esx_version must be a full ESXi build string starting with "
            f"'ESXi-' (e.g. 'ESXi-9.1.0.0100.25433460'), got: '{esx_version}'"
        )

    # evs.terms_accepted: must be True (not just present)
    if evs.get("terms_accepted") is not True:
        errors.append("evs.terms_accepted must be true (you must accept the EVS terms of service)")

    # evs.environment_name: the EVS CreateEnvironment API only accepts
    # [a-zA-Z0-9_-]+ (1-100 chars) — dots, spaces, etc. are rejected by the
    # service. Catch it here instead of ~15 minutes in at phase2.
    env_name = str(evs.get("environment_name", "")).strip()
    if env_name and not re.match(r"^[a-zA-Z0-9_-]{1,100}$", env_name):
        errors.append(
            f"evs.environment_name '{env_name}' is invalid. EVS only allows "
            f"letters, digits, hyphens, and underscores (1-100 chars) — no "
            f"dots or spaces. Try: '{re.sub(r'[^a-zA-Z0-9_-]', '-', env_name)[:100]}'"
        )

    # evs.simple_deployment: must be a boolean
    if not isinstance(evs.get("simple_deployment"), bool):
        errors.append(f"evs.simple_deployment must be true or false, got: '{evs.get('simple_deployment')}'")

    # jumpbox: optional; if present, enabled must be a boolean
    jumpbox = config.get("jumpbox", {}) or {}
    if jumpbox and not isinstance(jumpbox, dict):
        # A scalar/list (e.g. "jumpbox: true" instead of "jumpbox:\n  enabled:
        # true") would crash with a raw AttributeError on .get() below rather
        # than a clean validation error -- a plausible mistake from the README.
        errors.append(
            f"jumpbox must be a mapping (e.g. 'jumpbox:\\n  enabled: true'), "
            f"got: {jumpbox!r}"
        )
        jumpbox = {}
    if jumpbox and not isinstance(jumpbox.get("enabled", False), bool):
        errors.append(f"jumpbox.enabled must be true or false, got: '{jumpbox.get('enabled')}'")
    # Reject unknown jumpbox fields rather than silently ignoring them: the
    # jumpbox always uses the stack's own key pair (no per-jumpbox key_name), so
    # a blueprint setting a nonexistent field would otherwise be ignored.
    _known_jumpbox_keys = {"enabled", "instance_type"}
    unknown_jumpbox_keys = set(jumpbox.keys()) - _known_jumpbox_keys
    if unknown_jumpbox_keys:
        errors.append(
            f"jumpbox has unknown field(s): {sorted(unknown_jumpbox_keys)}. "
            f"Supported fields: {sorted(_known_jumpbox_keys)}. The jumpbox always "
            f"uses the stack's own key pair — there is no per-jumpbox key_name."
        )

    # sizing: validate enum values
    valid_sizing = {
        "vcenter_size": {"tiny", "small", "medium", "large", "xlarge"},
        "vcenter_storage_size": {"lstorage", "xlstorage"},
        "nsx_size": {"medium", "large", "xlarge"},
        "operations_collector_appliance_size": {"small", "standard"},
    }
    # operations_appliance_size valid values depend on deployment SHAPE, not VCF
    # version: simple mode allows xsmall/small/medium/large/xlarge; HA mode only
    # medium/large/xlarge (its 3-node VCF Operations cluster needs larger sizes).
    if evs.get("simple_deployment", True):
        valid_sizing["operations_appliance_size"] = {"xsmall", "small", "medium", "large", "xlarge"}
    else:
        valid_sizing["operations_appliance_size"] = {"medium", "large", "xlarge"}
    for field, allowed in valid_sizing.items():
        val = sizing.get(field, "")
        if val and val.lower() not in allowed:
            errors.append(f"sizing.{field} must be one of {sorted(allowed)}, got: '{val}'")
        elif val:
            # Normalize case in place so downstream consumers always see the
            # canonical lowercase form: a case-sensitive installer API would
            # otherwise fail hours into phase 3 despite passing this check.
            sizing[field] = val.lower()

    edge_ff = sizing.get("edge_form_factor", "")
    if not edge_ff:
        errors.append("sizing.edge_form_factor is required (SMALL/MEDIUM/LARGE/XLARGE)")
    elif edge_ff.upper() not in {"SMALL", "MEDIUM", "LARGE", "XLARGE"}:
        errors.append(f"sizing.edge_form_factor must be SMALL/MEDIUM/LARGE/XLARGE, got: '{edge_ff}'")
    else:
        # edgeFormFactor is documented as uppercase; normalize here so downstream
        # consumers (config.json's vcfSizing.edgeFormFactor) always see the
        # canonical uppercase form regardless of blueprint capitalization.
        sizing["edge_form_factor"] = edge_ff.upper()

    # hostnames.esxi: must be a list of at least 3 unique strings (EVS minimum).
    # NOTE: botocore 1.43.2 declares hosts min:4 (HostInfoForCreateList), but
    # server-side EVS historically accepts 3 — keep the permissive >=3 check
    # until 4 is confirmed required.
    esxi_hosts = hostnames_cfg.get("esxi", [])
    if not isinstance(esxi_hosts, list):
        errors.append(f"hostnames.esxi must be a list, got: {type(esxi_hosts).__name__}")
    elif len(esxi_hosts) < 3:
        errors.append(f"hostnames.esxi must have at least 3 hosts (EVS minimum), got {len(esxi_hosts)}")
    else:
        # HA mode (simple_deployment: false) requires at least 4 hosts
        # for the 3 NSX Manager nodes. Simple mode works with 3.
        if not evs.get("simple_deployment", True) and len(esxi_hosts) < 4:
            errors.append(
                f"HA mode (simple_deployment: false) requires at least 4 ESXi "
                f"hosts (VCF deploys 3 NSX Manager nodes which need the extra "
                f"capacity), got {len(esxi_hosts)}. Either add a 4th host or "
                f"set simple_deployment: true for a single-NSX-node deployment."
            )
        for h in esxi_hosts:
            if not isinstance(h, str) or len(h) > 63 or not re.match(r"^[a-zA-Z0-9]([a-zA-Z0-9\-]*[a-zA-Z0-9])?$", h):
                errors.append(f"hostnames.esxi entries must be valid hostnames (1-63 chars, alphanumeric + hyphens, no leading/trailing hyphen), got: '{h}'")
                break
        if len(esxi_hosts) != len(set(esxi_hosts)):
            dupes = [h for h in esxi_hosts if esxi_hosts.count(h) > 1]
            errors.append(f"hostnames.esxi entries must be unique, found duplicate(s): {sorted(set(dupes))}")

    # hostnames.vcenter/nsx/sddc_manager/edge01/edge02/vcf_ops: these become DNS
    # labels under dns.fqdn (e.g. "vc.<fqdn>") but previously had no local
    # validation -- a bad one surfaced later as a confusing DNS/appliance
    # failure instead of a clean error here. Mirror the same rule as
    # hostnames.esxi (1-63 chars, letters/digits/hyphens, no leading/trailing
    # hyphen) for every short-name field that's present, and also check the
    # COMBINED "<name>.<fqdn>" string against DNS limits (63/label, 253 total)
    # -- neither fqdn nor the short name is validated at the combined length
    # elsewhere.
    _HOSTNAME_LABEL_RE = re.compile(r"^[a-zA-Z0-9]([a-zA-Z0-9\-]*[a-zA-Z0-9])?$")
    _short_name_fields = ["vcenter", "nsx", "sddc_manager", "edge01", "edge02", "vcf_ops"]
    for field in _short_name_fields:
        short = hostnames_cfg.get(field)
        if short is None:
            continue  # optional -- omitted fields get sensible defaults downstream
        if not isinstance(short, str) or len(short) > 63 or not _HOSTNAME_LABEL_RE.match(short):
            errors.append(
                f"hostnames.{field} must be a valid hostname label (1-63 chars, "
                f"alphanumeric + hyphens, no leading/trailing hyphen), got: '{short}'"
            )
        elif fqdn:  # only checkable once dns.fqdn itself passed its own validation
            combined = f"{short}.{fqdn}"
            if len(combined) > 253:
                errors.append(
                    f"hostnames.{field} combined with dns.fqdn ('{combined}') is "
                    f"{len(combined)} chars, exceeding the 253-char DNS FQDN limit"
                )
    if fqdn and isinstance(esxi_hosts, list):
        for h in esxi_hosts:
            if isinstance(h, str) and len(f"{h}.{fqdn}") > 253:
                errors.append(
                    f"hostnames.esxi entry '{h}' combined with dns.fqdn exceeds "
                    f"the 253-char DNS FQDN limit"
                )

    # Subnet/VPC ID format validation
    for field_name, value in [
        ("vpc.public_subnet_id", vpc.get("public_subnet_id", "")),
        ("vpc.service_access_subnet_id", vpc.get("service_access_subnet_id", "")),
        ("vpc.service_access_route_table_id", vpc.get("service_access_route_table_id", "")),
    ]:
        if value and not re.match(r"^(subnet|rtb)-[a-f0-9]+$", value):
            errors.append(f"{field_name} must be a valid AWS resource ID, got: '{value}'")

    if not bootstrap_mode:
        vpc_id = vpc.get("id", "")
        if vpc_id and not re.match(r"^vpc-[a-f0-9]+$", vpc_id):
            errors.append(f"vpc.id must be a valid VPC ID (vpc-xxx), got: '{vpc_id}'")
        sg_id = vpc.get("security_group_id", "")
        if sg_id and not re.match(r"^sg-[a-f0-9]+$", sg_id):
            errors.append(f"vpc.security_group_id must be a valid SG ID (sg-xxx), got: '{sg_id}'")
        for ip_field in ("route_server_endpoint_01_ip", "route_server_endpoint_02_ip"):
            ip_val = vpc.get(ip_field, "")
            if ip_val and not re.match(r"^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$", ip_val):
                errors.append(f"vpc.{ip_field} must be a valid IPv4 address, got: '{ip_val}'")

    # phase3.depot_token_secret: validated as non-empty (required_paths handles that)
    # No naming restriction — the CFN DepotSecretName parameter scopes IAM access.

    # phase3.ntp: VCF rejects link-local addresses (169.254.x.x)
    ntp_servers = config.get("phase3", {}).get("ntp", [])
    if isinstance(ntp_servers, str):
        ntp_servers = [ntp_servers]
    for ntp in ntp_servers:
        if isinstance(ntp, str) and ntp.startswith("169.254."):
            errors.append(f"phase3.ntp: VCF rejects link-local NTP addresses ('{ntp}'). "
                          f"Use 'time.aws.com' or a routable NTP server instead.")

    # hcx.enabled and phase3.ceip: must be an actual bool, not just a
    # truthy value — a YAML string like "false" is truthy in Python and
    # would otherwise silently ENABLE HCX (provisioning public EIPs) or
    # CEIP telemetry despite the user's clear intent to disable it.
    hcx_enabled_raw = config.get("hcx", {}).get("enabled")
    if hcx_enabled_raw is not None and not isinstance(hcx_enabled_raw, bool):
        errors.append(
            f"hcx.enabled must be a YAML boolean (true/false, unquoted), "
            f"got: {hcx_enabled_raw!r}"
        )
    ceip_raw = config.get("phase3", {}).get("ceip")
    if ceip_raw is not None and not isinstance(ceip_raw, bool):
        errors.append(
            f"phase3.ceip must be a YAML boolean (true/false, unquoted), "
            f"got: {ceip_raw!r}"
        )

    # hcx: when enabled and public_cidr is provided (BYO), validate it.
    # When public_cidr is NOT provided, we provision via IPAM automatically.
    if hcx_enabled_raw is True:
        # Non-bootstrap (Terraform/manual) mode has no aws_config stage to inject
        # the HCX network ACL + EIP allocation, and generate_config dereferences
        # hcx.network_acl_id / hcx.eip_allocation_id directly. Require them up
        # front here so a missing value is a clear validation error rather than a
        # later KeyError. In bootstrap mode (_bootstrap_mode True) aws_config
        # provides these, so this is skipped — no effect on the
        # CloudFormation path.
        if not bootstrap_mode:
            for _hcx_field in ("network_acl_id", "eip_allocation_id"):
                if not config.get("hcx", {}).get(_hcx_field):
                    errors.append(
                        f"hcx.{_hcx_field} is required when hcx.enabled in "
                        "non-bootstrap mode (deploy_orchestrator.py run without "
                        "--vpc-create); the CloudFormation bootstrap flow "
                        "provides it automatically."
                    )
        hcx_cidr = config.get("hcx", {}).get("public_cidr")
        if hcx_cidr:
            # BYO path: validate the customer-provided CIDR
            if not re.match(r"^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}/28$", hcx_cidr):
                errors.append(f"hcx.public_cidr must be a /28 CIDR (e.g. '203.0.113.0/28'), got: '{hcx_cidr}'")
            else:
                # Reject RFC 1918 private ranges — EVS requires genuinely public IPs
                ip_part = hcx_cidr.split("/")[0]
                octets = [int(o) for o in ip_part.split(".")]
                if (octets[0] == 10
                    or (octets[0] == 172 and 16 <= octets[1] <= 31)
                    or (octets[0] == 192 and octets[1] == 168)):
                    errors.append(f"hcx.public_cidr must be a PUBLIC /28 CIDR (not RFC 1918 private), got: '{hcx_cidr}'")
        # else: no public_cidr provided → will be auto-provisioned via IPAM in stage_aws_config

    if errors:
        raise ValueError("Blueprint validation failed:\n  • " + "\n  • ".join(errors))

    if not PHASE2_SRC.exists():
        raise FileNotFoundError(f"Phase 2 source not found: {PHASE2_SRC}")
    if not PHASE3_SRC.exists():
        raise FileNotFoundError(f"Phase 3 source not found: {PHASE3_SRC}")


# EC2 instance families covered by the "Running On-Demand Standard instances"
# vCPU quota. Other families (G, P, HPC, ...) have their own quotas.
_STANDARD_INSTANCE_FAMILIES = {"a", "c", "d", "h", "i", "m", "r", "t", "z"}
_STANDARD_VCPU_QUOTA_CODE = "L-1216C47A"


def _instance_family(instance_type: str) -> str:
    """'i4i.metal' -> 'i', 't3.large' -> 't', 'hpc6a.48xlarge' -> 'hpc'."""
    letters = ""
    for ch in instance_type.split(".")[0]:
        if not ch.isalpha():
            break
        letters += ch
    return letters.lower()


def _vcpu_quota_preflight(config: dict) -> None:
    """Fail fast if the account lacks vCPU quota for the requested hosts.

    Without this, an over-quota deploy gets ~45 min in before EC2 refuses the
    final host (EVS launches one at a time, so earlier hosts already bill). Sized
    from the live EC2 API, not a hardcoded table. Any lookup failure degrades to
    a skip with a warning -- a preflight problem must never block a valid deploy.
    """
    region = config["aws"]["region"]
    instance_type = config["evs"]["instance_type"]
    host_count = len(config.get("hostnames", {}).get("esxi") or [])
    if not host_count:
        return

    if _instance_family(instance_type) not in _STANDARD_INSTANCE_FAMILIES:
        logger.info(
            "vCPU preflight skipped: %s is outside the Standard instance "
            "families, which are quota-tracked separately", instance_type,
        )
        return

    try:
        session = boto3.Session(
            profile_name=config["aws"].get("profile"), region_name=region,
        )
        ec2 = session.client("ec2")
        quotas = session.client("service-quotas")

        resp = ec2.describe_instance_types(InstanceTypes=[instance_type])
        vcpus_each = resp["InstanceTypes"][0]["VCpuInfo"]["DefaultVCpus"]

        quota = int(quotas.get_service_quota(
            ServiceCode="ec2", QuotaCode=_STANDARD_VCPU_QUOTA_CODE,
        )["Quota"]["Value"])

        running: list[str] = []
        paginator = ec2.get_paginator("describe_instances")
        for page in paginator.paginate(Filters=[
            {"Name": "instance-state-name", "Values": ["running", "pending"]},
        ]):
            for reservation in page.get("Reservations", []):
                for inst in reservation.get("Instances", []):
                    running.append(inst["InstanceType"])

        sizes: dict[str, int] = {}
        distinct = sorted(set(running))
        if distinct:
            resp = ec2.describe_instance_types(InstanceTypes=distinct)
            sizes = {
                t["InstanceType"]: t["VCpuInfo"]["DefaultVCpus"]
                for t in resp["InstanceTypes"]
            }

        in_use = 0
        unknown = set()
        for itype in running:
            if _instance_family(itype) not in _STANDARD_INSTANCE_FAMILIES:
                continue
            if itype in sizes:
                in_use += sizes[itype]
            else:
                unknown.add(itype)
        if unknown:
            logger.warning(
                "vCPU preflight skipped: could not size running instance "
                "type(s) %s", ", ".join(sorted(unknown)),
            )
            return
    except Exception as e:
        logger.warning("vCPU preflight skipped (could not query quota/usage): %s", e)
        return

    # On --resume mid-phase2, hosts already CREATED from a prior partial run are
    # already counted in `in_use` (running EC2 instances), so counting them again
    # in `needed` would double-count and falsely refuse the resume over quota.
    # Subtract them if an environment already exists.
    already_created = 0
    if GENERATED_CONFIG_PATH.exists():
        try:
            env_id = json.loads(GENERATED_CONFIG_PATH.read_text()).get("environmentId")
            if env_id:
                evs = boto3.client("evs", region_name=region)
                hosts_resp = evs.list_environment_hosts(environmentId=env_id)
                already_created = len(hosts_resp.get("environmentHosts", []))
                if already_created:
                    logger.info(
                        "vCPU preflight: %d of %d blueprint host(s) already "
                        "created in environment %s — not counting them as "
                        "new demand", already_created, host_count, env_id,
                    )
        except Exception as e:  # noqa: BLE001
            # Same "never block a valid deployment over a preflight
            # problem" principle as the rest of this function — worst
            # case we overcount (safe direction: more conservative, not
            # less) rather than crash.
            logger.warning(
                "vCPU preflight: could not check for already-created "
                "hosts (%s); counting all %d blueprint hosts as new "
                "demand", e, host_count,
            )
    remaining_needed = max(0, host_count - already_created) * vcpus_each
    after = in_use + remaining_needed
    if after > quota:
        raise ValueError(
            f"Not enough EC2 vCPU quota for {host_count} x {instance_type} "
            f"({already_created} already created): "
            f"needs {remaining_needed} more vCPUs, {in_use} already in use "
            f"in {region}, limit is {quota} (over by {after - quota}). "
            f"Raise quota {_STANDARD_VCPU_QUOTA_CODE} (Running On-Demand "
            f"Standard instances) to at least {after} in {region}, then "
            f"relaunch. Failing now instead of after ~45 minutes and "
            f"{host_count - 1} billing host(s)."
        )

    logger.info(
        "vCPU preflight OK: %d x %s (%d already created) needs %d more "
        "vCPUs, %d/%d after launch",
        host_count, instance_type, already_created, remaining_needed,
        after, quota,
    )


def _destroy_aws_config(config: dict) -> None:
    """Delete the aws_config CloudFormation stack.

    The stack is the durable record of what the aws_config stage created, so
    this works from a replacement runner or any machine with AWS credentials.
    """
    cfn = _aws_config_cfn(config)
    stack_name = _aws_config_stack_name(config)
    logger.info("destroy aws_config: deleting stack %s ...", stack_name)
    try:
        cfn.describe_stacks(StackName=stack_name)
    except cfn.exceptions.ClientError as e:
        # Same distinction as stage_aws_config: only a genuine "does not exist"
        # means nothing to delete. Throttling/AccessDenied/ExpiredToken must NOT
        # be swallowed -- returning would report DESTROY COMPLETE (and a SUCCEEDED
        # notification) while the NAT gateway, EIP, Route Server, key pair bill.
        code = _aws_error_code(e)
        if code == "ValidationError" and "does not exist" in str(e):
            logger.info("destroy aws_config: stack %s not found — nothing to do", stack_name)
            return
        raise
    # Re-point the VPC at default DHCP options first: CloudFormation cannot
    # delete an AWS::EC2::DHCPOptions that is still associated with the VPC.
    try:
        if config.get("vpc", {}).get("id"):
            session = boto3.Session(profile_name=config["aws"].get("profile"),
                                    region_name=config["aws"]["region"])
            session.client("ec2").associate_dhcp_options(
                DhcpOptionsId="default", VpcId=config["vpc"]["id"])
            logger.info("destroy aws_config: VPC re-pointed at default DHCP options")
    except Exception as e:  # noqa: BLE001
        logger.warning("destroy aws_config: DHCP disassociation failed (continuing): %s", e)
    cfn.delete_stack(StackName=stack_name)
    cfn.get_waiter("stack_delete_complete").wait(
        StackName=stack_name, WaiterConfig={"Delay": 30, "MaxAttempts": 60})
    logger.info("destroy aws_config: stack deleted. Bootstrap stack resources "
                "(VPC, subnets, IAM, runner) remain — delete the bootstrap "
                "CloudFormation stack to remove them.")


def _delete_environment_connectors(evs_client, env_id: str) -> None:
    """Delete any environment connectors (e.g. VCF Operations Manager).

    delete_environment is rejected while any connector is attached. Waits for
    each deletion to complete before returning.

    KNOWN LIMITATION: AWS requires all entitlements on the same vCenter removed
    before a connector can be deleted; this function does not do that. Such a
    failure is logged as a warning and destroy continues (it resurfaces clearly
    at the delete_environment call, blocked by the same root cause).
    """
    try:
        resp = evs_client.list_environment_connectors(environmentId=env_id)
    except Exception as e:  # noqa: BLE001
        logger.warning("Could not list environment connectors: %s", e)
        return

    connectors = resp.get("connectors", [])
    if not connectors:
        return

    for c in connectors:
        connector_id = c.get("connectorId")
        logger.info("  Deleting environment connector %s (type=%s)...",
                    connector_id, c.get("type"))
        try:
            evs_client.delete_environment_connector(
                environmentId=env_id, connectorId=connector_id,
            )
        except Exception as e:  # noqa: BLE001
            logger.warning("  Could not delete connector %s: %s", connector_id, e)
            continue

        deadline = time.time() + 600  # 10 min max per connector
        while time.time() < deadline:
            try:
                remaining = evs_client.list_environment_connectors(environmentId=env_id)
                still_there = [
                    x for x in remaining.get("connectors", [])
                    if x.get("connectorId") == connector_id
                ]
                if not still_there:
                    logger.info("  Connector %s deleted", connector_id)
                    break
            except Exception as e:  # noqa: BLE001
                logger.warning("  Error checking connector %s state: %s", connector_id, e)
                break
            time.sleep(15)
        else:
            logger.warning("  Timed out waiting for connector %s to delete "
                           "(continuing - delete_environment will report the "
                           "real blocker if this is still attached)", connector_id)


def _delete_environment_with_retry(evs_client, env_id: str) -> None:
    """Call delete_environment, retrying while the environment is CREATING.

    A CREATING environment rejects DeleteEnvironment until it settles. On
    rejection, the environment's ACTUAL state (from get_environment) decides what
    to do -- NOT error-message substrings: EVS's ValidationException can mention
    DELETING even when CREATING, which a prior version misread and hung on for 30
    min. Only ResourceNotFoundException is still classified from the error code.
    """
    deadline = time.time() + 1800  # 30 min max waiting for CREATING to resolve
    while True:
        try:
            evs_client.delete_environment(environmentId=env_id)
            logger.info("  Environment deletion initiated. Waiting for completion...")
            return
        except Exception as e:  # noqa: BLE001
            code = _aws_error_code(e)
            if code == "ResourceNotFoundException" or "not found" in str(e).lower():
                logger.info("  Environment already deleted")
                return

            # The delete was rejected - ask the service what state the
            # environment is actually in and decide from that.
            try:
                state = (evs_client.get_environment(environmentId=env_id)
                         .get("environment", {}).get("environmentState", "UNKNOWN"))
            except Exception as ge:  # noqa: BLE001
                if _aws_error_code(ge) == "ResourceNotFoundException":
                    logger.info("  Environment already deleted")
                    return
                logger.error("Failed to delete environment (code=%s): %s "
                             "(and get_environment also failed: %s)", code, e, ge)
                sys.exit(1)

            if state in ("DELETING", "DELETED"):
                logger.info("  Environment already deleting (state=%s)", state)
                return
            if state == "CREATING":
                if time.time() >= deadline:
                    logger.error("  Environment %s is still CREATING after 30 min "
                                 "- giving up. Re-run --destroy once it finishes "
                                 "creating.", env_id)
                    sys.exit(1)
                logger.info("  Environment %s is still CREATING - waiting to retry "
                            "delete...", env_id)
                time.sleep(60)
                continue
            if time.time() < deadline:
                # Stable-but-unexpected rejection (e.g. CREATED with a
                # transient service error): retry rather than give up.
                logger.warning("  delete_environment rejected while environment "
                               "is %s (code=%s): %s - retrying...", state, code, e)
                time.sleep(60)
                continue
            logger.error("Failed to delete environment (state=%s, code=%s): %s",
                         state, code, e)
            sys.exit(1)


def _destroy_aws_config_if_present(config: dict) -> None:
    """Delete the aws_config stack if bootstrap mode says it should exist,
    OR if a stack matching its name happens to exist anyway.

    The second check is a deliberate safety net: a manual --destroy that omits
    --vpc-create leaves _bootstrap_mode False, so the aws_config stack (NAT, EIP,
    Route Server, key pair -- all still billing) would otherwise be skipped
    silently. A cheap describe_stacks by deterministic name closes that gap.
    """
    if config.get("_bootstrap_mode"):
        _destroy_aws_config(config)
        return

    stack_name = _aws_config_stack_name(config)
    try:
        cfn = _aws_config_cfn(config)
        cfn.describe_stacks(StackName=stack_name)
    except cfn.exceptions.ClientError as e:
        # Same distinction as stage_aws_config/_destroy_aws_config: only a genuine
        # "does not exist" means nothing orphaned. A transient/permissions error
        # must not be swallowed as "doesn't exist" -- that false-negative is what
        # would let an orphaned landing zone (all billing) go undetected.
        code = _aws_error_code(e)
        if code == "ValidationError" and "does not exist" in str(e):
            return  # genuinely doesn't exist — nothing orphaned
        raise

    logger.warning(
        "destroy: --vpc-create was not passed (or this is a non-bootstrap "
        "run), but a stack named %s exists — this looks like an orphaned "
        "aws_config landing zone from a bootstrap run. Deleting it now to "
        "avoid stranding its NAT gateway/EIP/Route Server/key pair.",
        stack_name,
    )
    _destroy_aws_config(config)


class _DestroyIssueTracker(logging.Handler):
    """Counts WARNING/ERROR records during run_destroy so a teardown that only
    logged per-resource warnings is reported as PARTIAL (FAILED SNS + non-zero
    exit) instead of a false SUCCEEDED (HI-3/LOW-20)."""

    def __init__(self):
        super().__init__(level=logging.WARNING)
        self.count = 0
        self.messages = []

    def emit(self, record):
        self.count += 1
        if len(self.messages) < 20:
            self.messages.append(record.getMessage())


def _teardown_hcx_networking(config: dict, phase: str, environment_id: str | None = None) -> None:
    """HCX networking teardown for the on-runner --destroy path (HI-4), mirroring
    destroy.py. phase 'eips' releases the HCX public EIPs (run BEFORE
    delete_environment, which EVS rejects while the public subnet has EIP
    associations); phase 'ipam' disassociates the /28 from the VPC, deprovisions
    it, and deletes the pool (run AFTER the env is gone). No-op for BYO
    public_cidr or non-HCX deployments (no EVS-HCX-<stack> resources exist)."""
    region = config["aws"]["region"]
    session = boto3.Session(profile_name=config["aws"].get("profile"), region_name=region)
    ec2 = session.client("ec2")
    stack_name = _aws_config_stack_name(config)
    if not stack_name:
        return
    tag = f"EVS-HCX-{stack_name}"
    if phase == "eips":
        # The association between an HCX EIP and the public HCX VLAN is made
        # through EVS's OWN API (AssociateEipToVlan), not a plain EC2
        # association. ec2:ReleaseAddress alone can leave the EIP permanently
        # stuck ("in use" per EC2, but ec2:DisassociateAddress reports the
        # association "not found") -- evs:DisassociateEipFromVlan (using the
        # real associationId from evs:ListEnvironmentVlans's hcx entry) clears
        # it. Confirmed live: every HCX EIP left "phantom" by EC2-only release
        # released immediately once this was called first.
        evs_assoc_by_alloc: dict[str, str] = {}
        env_id = environment_id
        if env_id:
            try:
                evs_client = session.client("evs")
                vlans = evs_client.list_environment_vlans(environmentId=env_id).get(
                    "environmentVlans", [])
                for v in vlans:
                    if v.get("functionName") == "hcx":
                        for a in v.get("eipAssociations", []) or []:
                            evs_assoc_by_alloc[a["allocationId"]] = a["associationId"]
            except Exception as e:  # noqa: BLE001
                logger.warning("  HCX: could not list environment VLANs (%s); "
                               "falling back to EC2-only disassociate/release", e)
        try:
            addrs = ec2.describe_addresses().get("Addresses", [])
        except Exception as e:  # noqa: BLE001
            logger.warning("  HCX: could not list EIPs: %s", e)
            return
        for a in addrs:
            n = next((t["Value"] for t in a.get("Tags", []) if t["Key"] == "Name"), "")
            if not n.startswith(tag):
                continue
            ip = a.get("PublicIp", "?")
            evs_assoc_id = evs_assoc_by_alloc.get(a["AllocationId"])
            if evs_assoc_id and env_id:
                try:
                    session.client("evs").disassociate_eip_from_vlan(
                        environmentId=env_id, vlanName="hcx", associationId=evs_assoc_id,
                    )
                    logger.info("  HCX EIP %s: disassociated from the hcx VLAN via EVS", ip)
                except Exception as e:  # noqa: BLE001
                    logger.warning("  HCX EIP %s: evs:DisassociateEipFromVlan failed (%s); "
                                   "trying EC2-level release anyway", ip, e)
            try:
                if a.get("AssociationId"):
                    try:
                        ec2.disassociate_address(AssociationId=a["AssociationId"])
                    except Exception:  # noqa: BLE001
                        pass
                ec2.release_address(AllocationId=a["AllocationId"])
                logger.info("  Released HCX EIP %s", ip)
            except Exception as e:  # noqa: BLE001
                logger.warning("  HCX EIP %s NOT released (%s) — likely EVS-held; free "
                               "it and re-run to reclaim the IPAM block", ip, e)
        return
    # phase == "ipam"
    try:
        pools = [p for pg in ec2.get_paginator("describe_ipam_pools").paginate()
                 for p in pg.get("IpamPools", [])
                 if next((t["Value"] for t in p.get("Tags", []) if t["Key"] == "Name"), "") == tag]
    except Exception as e:  # noqa: BLE001
        logger.warning("  HCX: could not list IPAM pools: %s", e)
        return
    if not pools:
        return  # BYO public_cidr or non-HCX — nothing to reclaim
    vpc_id = config.get("vpc", {}).get("id")
    vpc_assocs = {}
    if vpc_id:
        try:
            v = ec2.describe_vpcs(VpcIds=[vpc_id])["Vpcs"][0]
            vpc_assocs = {a["CidrBlock"]: a["AssociationId"]
                          for a in v.get("CidrBlockAssociationSet", [])
                          if a.get("CidrBlockState", {}).get("State") in ("associated", "associating")}
        except Exception:  # noqa: BLE001
            pass
    for p in pools:
        pid = p["IpamPoolId"]
        try:
            cidrs = [c["Cidr"] for c in ec2.get_ipam_pool_cidrs(IpamPoolId=pid)
                     .get("IpamPoolCidrs", []) if c.get("State") == "provisioned"]
        except Exception:  # noqa: BLE001
            cidrs = []
        for cidr in cidrs:
            if cidr in vpc_assocs:
                try:
                    ec2.disassociate_vpc_cidr_block(AssociationId=vpc_assocs[cidr])
                    time.sleep(5)
                except Exception as e:  # noqa: BLE001
                    logger.warning("  Could not disassociate HCX CIDR %s: %s", cidr, e)
            try:
                ec2.deprovision_ipam_pool_cidr(IpamPoolId=pid, Cidr=cidr)
                for _ in range(12):
                    time.sleep(10)
                    left = ec2.get_ipam_pool_cidrs(IpamPoolId=pid).get("IpamPoolCidrs", [])
                    if all(x.get("Cidr") != cidr or x.get("State") == "deprovisioned" for x in left):
                        break
            except Exception as e:  # noqa: BLE001
                logger.warning("  Could not deprovision HCX CIDR %s (%s) — a stuck EIP "
                               "may still hold it; free it and re-run", cidr, e)
        try:
            ec2.delete_ipam_pool(IpamPoolId=pid)
            logger.info("  Deleted HCX IPAM pool %s", pid)
        except Exception as e:  # noqa: BLE001
            logger.warning("  Could not delete HCX IPAM pool %s (%s) — stuck allocation; "
                           "frees once the EIP is released", pid, e)


def run_destroy(config: dict):
    """Tear down the EVS environment and all associated resources.

    Deletion order:
      1. Delete all ESXi hosts (waits for each to terminate)
      2. Detach and delete the EBS volume (if it still exists)
      3. Delete VCF appliance secrets from Secrets Manager
      4. Delete any environment connectors (blocks step 5 otherwise)
      5. Delete the EVS environment (also removes VLAN subnets)
      6. Delete the aws_config landing zone stack (if present)
    """
    _issues = _DestroyIssueTracker()
    logging.getLogger().addHandler(_issues)
    cfg = {}
    if GENERATED_CONFIG_PATH.exists():
        try:
            cfg = json.loads(GENERATED_CONFIG_PATH.read_text())
        except (OSError, ValueError) as e:
            logger.warning(
                "Could not read/parse %s (%s) — falling back to "
                "config['evs']['environment_id'] so teardown can still proceed",
                GENERATED_CONFIG_PATH, e,
            )
    env_id = cfg.get("environmentId") or config.get("evs", {}).get("environment_id")

    if not env_id:
        logger.warning("No environmentId found in config.json — skipping environment teardown")
        _destroy_aws_config_if_present(config)
        return

    region = config["aws"]["region"]
    session = boto3.Session(
        profile_name=config["aws"].get("profile"),
        region_name=region,
    )

    evs_client = session.client("evs")
    ec2_client = session.client("ec2")
    sm_client = session.client("secretsmanager")

    def _list_all_hosts():
        """List all hosts across pages (nextToken) so none is missed."""
        items, token = [], None
        while True:
            kw = {"environmentId": env_id}
            if token:
                kw["nextToken"] = token
            resp = evs_client.list_environment_hosts(**kw)
            items.extend(resp.get("environmentHosts", []))
            token = resp.get("nextToken")
            if not token:
                return items

    # Step 1: Delete all hosts
    logger.info("destroy 1/5: Deleting hosts for environment %s...", env_id)
    hosts = []
    try:
        hosts = _list_all_hosts()
        for host in hosts:
            host_name = host.get("hostName", "unknown")
            logger.info("  Deleting host: %s", host_name)
            try:
                evs_client.delete_environment_host(
                    environmentId=env_id,
                    hostName=host_name,
                )
            except Exception as e:
                logger.warning("  Could not delete host %s: %s", host_name, e)
    except Exception as e:
        logger.warning("Could not list/delete hosts: %s", e)

    # Wait for hosts to be deleted
    if hosts:
        logger.info("  Waiting for hosts to be deleted (this may take several minutes)...")
        deadline = time.time() + 1800  # 30 min max
        consecutive_errors = 0
        hosts_confirmed_deleted = False
        while time.time() < deadline:
            try:
                all_hosts = _list_all_hosts()
                consecutive_errors = 0
                remaining = [
                    h for h in all_hosts
                    # DELETE_FAILED is not a real EVS HostState (enum: CREATING/
                    # CREATED/UPDATING/DELETING/DELETED/CREATE_FAILED/UPDATE_FAILED);
                    # only DELETED means a host is gone, so a failed deletion stays
                    # in `remaining` and the loop times out + exits -- fail-safe.
                    if h.get("hostState") != "DELETED"
                ]
                if not remaining:
                    logger.info("  All hosts deleted")
                    hosts_confirmed_deleted = True
                    break
                logger.info("  %d host(s) still deleting...", len(remaining))
            except Exception as e:  # noqa: BLE001
                consecutive_errors += 1
                logger.warning("  Error checking host deletion status (attempt %d): %s",
                               consecutive_errors, e)
                if consecutive_errors >= 5:
                    logger.error("  Giving up on host-deletion polling after 5 "
                                 "consecutive errors - hosts may still be deleting "
                                 "in the background. Re-run --destroy to confirm "
                                 "before proceeding, or check the EVS console.")
                    sys.exit(1)
            time.sleep(60)
        else:
            if not hosts_confirmed_deleted:
                logger.error("  Timed out waiting for hosts to delete (30 min). "
                             "Aborting destroy - proceeding to delete the EVS "
                             "environment now could orphan the EBS volume. "
                             "Re-run --destroy once the hosts finish deleting.")
                sys.exit(1)

    # Step 2: Detach and delete EBS volume
    logger.info("destroy 2/5: Removing EBS volume...")
    try:
        volumes = []
        for _vol_page in ec2_client.get_paginator("describe_volumes").paginate(
            Filters=[
                {"Name": "tag:ManagedBy", "Values": ["phase2-automation"]},
                {"Name": "tag:EnvironmentId", "Values": [env_id]},
            ]
        ):
            volumes.extend(_vol_page.get("Volumes", []))
    except Exception as e:  # noqa: BLE001
        logger.warning("Could not list EBS volumes to remove: %s", e)
        volumes = []

    for vol in volumes:
        # Isolated per-volume: one volume's failure must not abort the rest -
        # the old code wrapped the entire loop in one try/except, so a
        # failure on the first volume silently skipped every volume after it.
        vol_id = vol["VolumeId"]
        try:
            if vol.get("Attachments"):
                logger.info("  Detaching volume %s...", vol_id)
                ec2_client.detach_volume(VolumeId=vol_id, Force=True)
                waiter = ec2_client.get_waiter("volume_available")
                waiter.wait(VolumeIds=[vol_id], WaiterConfig={"Delay": 10, "MaxAttempts": 30})
            logger.info("  Deleting volume %s...", vol_id)
            ec2_client.delete_volume(VolumeId=vol_id)
            logger.info("  Volume %s deleted", vol_id)
        except Exception as e:  # noqa: BLE001
            logger.warning("  Could not remove EBS volume %s (continuing with "
                           "any remaining volumes): %s", vol_id, e)

    # Step 3: Delete VCF appliance secrets. Two naming patterns: evs-<env_id>_*
    # (orchestrator-created appliance passwords) and evs!<env_id>_* (EVS-created
    # ESXi host passwords, note "!" not "-"). Query and delete both, and verify
    # afterward rather than assuming EVS cleaned the second up.
    logger.info("destroy 3/5: Deleting VCF appliance and ESXi host secrets...")
    secret_names_deleted = []
    for name_filter in (f"evs-{env_id}", f"evs!{env_id}"):
        try:
            paginator = sm_client.get_paginator("list_secrets")
            secrets_to_delete = [
                secret["Name"]
                for page in paginator.paginate(Filters=[{"Key": "name", "Values": [name_filter]}])
                for secret in page.get("SecretList", [])
            ]
        except Exception as e:  # noqa: BLE001
            logger.warning("Could not list secrets matching '%s*': %s", name_filter, e)
            continue

        for secret_name in secrets_to_delete:
            # Exact-env guard: the list_secrets name filter is a PREFIX match, so
            # it also returns a DIFFERENT environment whose id merely starts with
            # this one. Only delete this env's own secrets: the bare filter, or
            # filter + "_<role/host>".
            if not (secret_name == name_filter or secret_name.startswith(name_filter + "_")):
                logger.info("  Skipping %s (belongs to a different environment)", secret_name)
                continue
            # Isolated per-secret: one secret's failure must not abort the
            # rest, same reasoning as the per-volume fix above.
            try:
                logger.info("  Deleting secret (30-day recovery): %s", secret_name)
                # Recoverable delete rather than ForceDeleteWithoutRecovery so a
                # partial/accidental teardown is reversible and a stray prefix
                # match is never irreversibly destroyed.
                sm_client.delete_secret(SecretId=secret_name)
                secret_names_deleted.append(secret_name)
            except Exception as e:  # noqa: BLE001
                if "scheduled for deletion" in str(e):
                    logger.info("  %s already scheduled for deletion", secret_name)
                    secret_names_deleted.append(secret_name)
                    continue
                logger.warning("  Could not delete secret %s (continuing with "
                               "any remaining secrets): %s", secret_name, e)

    # Step 4: Delete any environment connectors (blocks delete_environment
    # otherwise, and the AWS CLI has no direct delete-environment-connector
    # command - this is the only place this can be handled).
    logger.info("destroy 4/5: Deleting environment connectors (if any)...")
    _delete_environment_connectors(evs_client, env_id)

    # Release HCX public EIPs first (HI-4) — EVS rejects delete_environment while
    # the public subnet still has EIP associations. No-op if not HCX.
    _teardown_hcx_networking(config, "eips", environment_id=env_id)
    # Step 5: Delete the EVS environment and wait for completion
    logger.info("destroy 5/5: Deleting EVS environment %s...", env_id)
    _delete_environment_with_retry(evs_client, env_id)

    # Poll until environment is fully deleted
    deadline = time.time() + 1800  # 30 min max
    while time.time() < deadline:
        try:
            resp = evs_client.get_environment(environmentId=env_id)
            state = resp.get("environment", {}).get("environmentState", "UNKNOWN")
            if state == "DELETED":
                logger.info("  Environment %s fully deleted", env_id)
                break
            logger.info("  Environment state: %s — waiting...", state)
        except Exception as e:
            if _aws_error_code(e) == "ResourceNotFoundException" or "not found" in str(e).lower():
                logger.info("  Environment %s no longer exists — deleted", env_id)
                break
            logger.warning("  Error checking environment state: %s", e)
        time.sleep(60)
    else:
        logger.error("  Timed out waiting for environment deletion (30 min). "
                     "Aborting destroy — deleting the aws-config stack now would fail "
                     "with DependencyViolation (EVS service ENIs still reference its "
                     "security group). Re-run --destroy once the environment is gone.")
        _notify(config, "EVS destroy FAILED",
                f"Environment {env_id} deletion timed out (30 min); teardown aborted "
                f"and resources may still be billing. Re-run --destroy once the "
                f"environment shows DELETED.")
        sys.exit(1)

    # Step 6: remove the aws_config landing zone (bootstrap OR an orphaned
    # non-bootstrap stack — see _destroy_aws_config_if_present). Bootstrap-CFN
    # resources (VPC, subnets, IGW, IAM, runner) are left for the stack delete.
    _destroy_aws_config_if_present(config)

    # Reclaim the HCX IPAM /28 + pool now the environment is gone (HI-4).
    # No-op for BYO-public-cidr or non-HCX deployments.
    _teardown_hcx_networking(config, "ipam")

    # Verify EVS actually cleaned up the evs!<env_id>_* ESXi host secrets on
    # its own, rather than just asserting it in the closing summary below.
    leftover_host_secrets = []
    try:
        paginator = sm_client.get_paginator("list_secrets")
        leftover_host_secrets = [
            secret["Name"]
            for page in paginator.paginate(Filters=[{"Key": "name", "Values": [f"evs!{env_id}"]}])
            for secret in page.get("SecretList", [])
        ]
    except Exception as e:  # noqa: BLE001
        logger.warning("Could not verify ESXi host secret cleanup: %s", e)

    if leftover_host_secrets:
        logger.warning("  %d ESXi host secret(s) still present after environment "
                       "deletion (expected EVS to remove these automatically): %s "
                       "— deleting them explicitly.",
                       len(leftover_host_secrets), leftover_host_secrets)
        for secret_name in leftover_host_secrets:
            try:
                sm_client.delete_secret(SecretId=secret_name, ForceDeleteWithoutRecovery=True)
            except Exception as e:  # noqa: BLE001
                logger.warning("  Could not delete leftover secret %s: %s", secret_name, e)

    # Clean up local state
    checkpoint_path = Path(config.get("checkpoint_path", "./orchestrator_state.json"))
    if checkpoint_path.exists():
        checkpoint_path.unlink()
        logger.info("Removed checkpoint file: %s", checkpoint_path)

    logger.info("=" * 60)
    logger.info("DESTROY COMPLETE")
    logger.info("  Environment %s deleted", env_id)
    logger.info("  VCF appliance secrets deleted: %s", secret_names_deleted or "(none found)")
    logger.info("  EBS volume(s) removed")
    if leftover_host_secrets:
        logger.info("  ESXi host secrets (evs!%s_*): %d were NOT auto-removed by "
                    "EVS and were deleted explicitly (see warning above)",
                    env_id, len(leftover_host_secrets))
    else:
        logger.info("  ESXi host secrets (evs!%s_*): verified none remain", env_id)
    logger.info("=" * 60)

    logging.getLogger().removeHandler(_issues)
    if _issues.count:
        logger.error("Teardown finished with %d warning(s)/error(s) — some resources "
                     "may remain and may still be billing.", _issues.count)
        _notify(
            config, "EVS destroy PARTIAL/FAILED",
            f"Environment {env_id} teardown finished with {_issues.count} "
            f"warning(s)/error(s); some resources may remain and may still be "
            f"billing. Review the orchestrator log. First issues: "
            f"{'; '.join(_issues.messages[:8])}",
        )
        if GENERATED_CONFIG_PATH.exists():
            GENERATED_CONFIG_PATH.unlink()
        sys.exit(1)

    _notify(
        config, "EVS destroy SUCCEEDED",
        f"Environment {env_id} and orchestrator-created AWS resources have "
        f"been deleted. If this was a bootstrap-mode deployment, delete the "
        f"bootstrap CloudFormation stack next to remove the VPC/runner/IAM role.",
    )

    if GENERATED_CONFIG_PATH.exists():
        GENERATED_CONFIG_PATH.unlink()
        logger.info("Removed %s", GENERATED_CONFIG_PATH)


def main():
    args = parse_args()
    config = yaml.safe_load(Path(args.config).read_text())
    vpc_create = {"true": True, "false": False, None: None}[args.vpc_create]
    _normalize_config(config, vpc_create=vpc_create)

    if args.destroy and args.dry_run:
        # run_destroy() performs REAL, irreversible deletions with no preview mode
        # and no confirmation prompt. Silently ignoring --dry-run would let an
        # operator asking to PREVIEW a teardown actually perform one, so reject
        # the combination outright rather than guessing which flag wins.
        logger.error(
            "--destroy and --dry-run cannot be combined: --destroy always "
            "performs a real teardown immediately, with no preview mode. "
            "Run without --dry-run if you intend to actually tear down, or "
            "without --destroy if you only wanted to preview the stage plan."
        )
        sys.exit(1)

    if not args.destroy:
        try:
            _validate_config(config)
            # Skip vCPU preflight when resuming from a stage after phase2_deploy
            # (hosts already exist and are billing — the check would double-count them).
            phase2_idx = STAGE_IDS.index("phase2_deploy")
            start_idx = STAGE_IDS.index(args.start_from) if args.start_from else 0
            if start_idx <= phase2_idx and not args.resume:
                _vcpu_quota_preflight(config)
            elif args.resume:
                # Only run preflight if phase2 hasn't completed yet
                checkpoint_path = Path(config.get("checkpoint_path", "./orchestrator_state.json"))
                if checkpoint_path.exists():
                    config_hash = hashlib.sha256(
                        json.dumps(config, sort_keys=True).encode()
                    ).hexdigest()
                    cp = Checkpoint(checkpoint_path, config_hash)
                    if not cp.is_completed("phase2_deploy"):
                        _vcpu_quota_preflight(config)
                else:
                    _vcpu_quota_preflight(config)
            else:
                logger.info("vCPU preflight skipped (resuming from after phase2_deploy)")
        except (ValueError, RuntimeError) as e:
            logger.error("Blueprint validation / preflight failed: %s", e)
            _notify(config, "EVS deployment FAILED",
                    f"Blueprint validation / preflight failed:\n\n{e}")
            sys.exit(1)
    # else: --destroy skips the vCPU preflight. That check counts every blueprint
    # host as NEW demand, but for a teardown those are the SAME hosts about to be
    # deleted -- running it would refuse to start a teardown when quota is tight
    # (normal right after a deploy), stranding the billing hosts being removed.

    # Prevent concurrent orchestrator runs (e.g. manual --resume/--destroy
    # while nohup still running). Applies to --destroy too — a teardown
    # racing a still-live deployment is exactly the scenario this guards.
    import fcntl
    lock_path = Path(args.config).parent / ".orchestrator.lock"
    lock_file = open(lock_path, "w")
    try:
        fcntl.flock(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        logger.error("Another orchestrator instance is already running (lock: %s). "
                     "If you're sure no other instance is running, delete the lock file.",
                     lock_path)
        sys.exit(1)

    if args.destroy:
        run_destroy(config)
        return

    config_hash = hashlib.sha256(
        json.dumps(config, sort_keys=True).encode()
    ).hexdigest()

    checkpoint_path = Path(config.get("checkpoint_path", "./orchestrator_state.json"))
    checkpoint = Checkpoint(checkpoint_path, config_hash)

    if args.dry_run:
        logger.info("DRY RUN — stage plan:")
        for stage_id, _ in STAGES:
            skip = (
                (args.skip_prework and stage_id in ESXI_PREWORK_STAGES)
                or (args.start_from and STAGE_IDS.index(stage_id) < STAGE_IDS.index(args.start_from))
                or (args.resume and checkpoint.is_completed(stage_id))
            )
            status = "SKIP" if skip else "RUN"
            logger.info("  [%s] %s", status, stage_id)
        return

    start_index = STAGE_IDS.index(args.start_from) if args.start_from else 0

    # If aws_config already completed in a previous run (resume / --start-from),
    # its outputs (security group, route server endpoint IPs, key pair) must be
    # re-merged into the in-memory config for the later stages.
    prior_aws_config = checkpoint.get_result("aws_config")
    if prior_aws_config:
        _apply_aws_config_outputs(config, prior_aws_config)

    if start_index == 0 and not args.resume:
        _vcf_version = config.get("evs", {}).get("vcf_version", "9.x")
        _notify(
            config,
            "Deployment started - about 4-7 hours",
            f"Your Amazon EVS + VCF {_vcf_version} deployment has started and "
            f"runs fully unattended (about 4-7 hours total).\n\n"
            "You'll get an email at each major point: when preparation is done "
            "(environment + host creation begins), when all pre-bringup steps "
            "finish (VCF bringup begins), roughly every 2 hours during the long "
            "bringup phase, and at the final result.\n\n"
            "No action needed.",
        )

    for i, (stage_id, stage_fn) in enumerate(STAGES):
        if i < start_index:
            logger.info("SKIP  [%s] (before --start-from)", stage_id)
            continue
        if args.skip_prework and stage_id in ESXI_PREWORK_STAGES:
            logger.info("SKIP  [%s] (--skip-prework)", stage_id)
            continue
        if args.resume and checkpoint.is_completed(stage_id):
            logger.info("SKIP  [%s] (already completed)", stage_id)
            continue

        logger.info("START [%s]", stage_id)
        try:
            result = _run_stage_with_heartbeat(
                config, stage_id, stage_fn, checkpoint,
                position=i + 1, total=len(STAGES),
            )
            checkpoint.mark_completed(stage_id, result)
            logger.info("DONE  [%s] %s", stage_id, json.dumps(result, default=str))
            # Curated customer notifications: stay quiet through the fast prep
            # and ESXi-prep stages, then send exactly two milestones -- one when
            # preparation is done (about to create the environment + hosts) and
            # one when all pre-bringup work is done (about to start the
            # multi-hour bringup). Long stages still emit a ~2h heartbeat from
            # _run_stage_with_heartbeat; SUCCEEDED/FAILED are emitted outside the
            # loop. Fewer, well-spaced emails also avoid the out-of-order arrival
            # you get when many notifications are published seconds apart.
            if stage_id == "validate_dns":
                _notify(
                    config,
                    "Preparation complete - creating environment + hosts",
                    "Networking, deployment config, and DNS validation are "
                    "complete.\n\n"
                    "Next: creating the EVS environment and provisioning the "
                    "bare-metal ESXi hosts -- this typically takes about 45-100 "
                    "minutes.\n\n"
                    "No action needed.",
                )
            elif stage_id == "esxi_ova_verify":
                _notify(
                    config,
                    "Infrastructure ready - starting VCF bringup (~3-5.5h)",
                    "All pre-bringup steps are complete: the EVS environment is "
                    "created, the bare-metal hosts are up, and the SDDC Manager "
                    "OVA has been deployed and verified.\n\n"
                    "Now starting the longest phase -- VCF bringup and the NSX "
                    "edge cluster (about 3-5.5 hours). You'll get a progress "
                    "update roughly every 2 hours until it completes.\n\n"
                    "No action needed.",
                )
        except Exception as e:
            checkpoint.mark_failed(stage_id, str(e))
            logger.error("FAIL  [%s] %s", stage_id, e, exc_info=True)
            logger.error(
                "To resume from this stage, run with --resume or --start-from %s",
                stage_id,
            )
            # Gather recent ERROR lines for a more useful notification
            recent_errors = ""
            try:
                log_path = Path("/opt/evs/orchestrator.log")
                if log_path.exists():
                    lines = log_path.read_text().splitlines()
                    error_lines = [ln for ln in lines[-50:] if "[ERROR]" in ln]
                    if error_lines:
                        recent_errors = "\n\nRecent errors:\n" + "\n".join(error_lines[-5:])
            except Exception:
                pass

            _notify(
                config, "EVS deployment FAILED",
                f"Stage '{stage_id}' failed: {e}{recent_errors}\n\n"
                f"To resume from this stage: --resume or --start-from {stage_id}",
            )
            sys.exit(1)

    fqdn = config["dns"]["fqdn"]
    env_id = ""
    try:
        env_id = json.loads(GENERATED_CONFIG_PATH.read_text()).get("environmentId", "")
    except (OSError, ValueError):
        pass
    region = config.get("aws", {}).get("region", "<region>")
    jumpbox_enabled = (config.get("jumpbox", {}) or {}).get("enabled") is True

    urls = (
        f"  vCenter:      https://{config['hostnames']['vcenter']}.{fqdn}\n"
        f"  NSX Manager:  https://{config['hostnames']['nsx']}.{fqdn}\n"
        f"  SDDC Manager: https://{config['hostnames']['sddc_manager']}.{fqdn}"
    )
    jumpbox_detail = ""
    if jumpbox_enabled:
        try:
            cfn = boto3.Session(
                profile_name=config.get("aws", {}).get("profile"),
                region_name=region,
            ).client("cloudformation")
            outs = cfn.describe_stacks(
                StackName=_aws_config_stack_name(config)
            )["Stacks"][0].get("Outputs", [])
            jb = {o["OutputKey"]: o["OutputValue"] for o in outs
                  if o["OutputKey"].startswith("Jumpbox")}
            if jb:
                jumpbox_detail = (
                    f"\n  Jumpbox public IP:      {jb.get('JumpboxPublicIp', '?')}"
                    f"\n  Jumpbox instance ID:    {jb.get('JumpboxInstanceId', '?')}"
                    f"\n  Jumpbox security group: {jb.get('JumpboxSecurityGroupId', '?')}"
                    f"\n  To connect: add an RDP (3389) rule for your IP to that "
                    f"security group, then RDP as Administrator (password via EC2 "
                    f"'Get Windows password' with the deployment's key pair — see "
                    f"the README's jumpbox section)."
                )
        except Exception as e:  # pragma: no cover — cosmetic enrichment only
            logger.debug("Could not fetch jumpbox outputs: %s", e)

    access_note = (
        "These URLs are on a PRIVATE network inside your VPC — they will not "
        "load from your laptop directly. Access them from a machine inside "
        "the VPC"
        + (", such as the Windows jumpbox this deployment created:"
           + jumpbox_detail
           if jumpbox_enabled else
           " (e.g. a jumpbox or VPN — see the README's 'After deployment' "
           "section for options).")
    )
    creds_note = (
        f"Passwords are in AWS Secrets Manager (region {region}), named "
        f"evs-{env_id or '<environment-id>'}_<role>.\n"
        f"  vCenter SSO admin (administrator@vsphere.local):\n"
        f"    aws secretsmanager get-secret-value --region {region} "
        f"--secret-id evs-{env_id or '<environment-id>'}_vcenterSso "
        f"--query SecretString --output text\n"
        f"  Other roles: vcenterRoot, nsxAdmin, nsxRoot, sddcManagerRoot, "
        f"operationsAdmin, edgeAppliance."
    )

    logger.info("=" * 60)
    logger.info("ALL STAGES COMPLETE — your VCF environment is ready")
    for line in urls.splitlines():
        logger.info("%s", line)
    logger.info("-" * 60)
    logger.info("%s", access_note)
    for line in creds_note.splitlines():
        logger.info("%s", line)
    logger.info("=" * 60)
    _notify(
        config, "EVS deployment SUCCEEDED",
        f"Your VCF environment is ready.\n\n"
        f"Log in to:\n{urls}\n\n"
        f"How to access:\n{access_note}\n\n"
        f"Credentials:\n{creds_note}\n",
    )


if __name__ == "__main__":
    main()
