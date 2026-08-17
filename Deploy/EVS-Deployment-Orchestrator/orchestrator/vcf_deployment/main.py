# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Entry point for the Phase 3 VCF Bringup + Edge Cluster CLI."""

import argparse
import getpass
import json
import logging
import os
import sys
import time as _time
from pathlib import Path

import boto3

from bringup_manager import BringupManager, is_bringup_failure
from depot_manager import DepotManager
from ebs_volume_destroyer import EbsVolumeDestroyer
from edge_cluster_manager import EdgeClusterManager
from installer_client import InstallerClient
from nsx_client import NsxClient
from secret_resolver import MissingSecretsError, SecretResolver, secret_name
from vcenter_client import VcenterClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
)
logger = logging.getLogger(__name__)


def _resolve_verify_tls(args: argparse.Namespace, host: str) -> "bool | str":
    """Resolve the verify_tls value to use for a given appliance host.

    Prefers ``args.verify_tls_by_host`` (optional {host: bool|str}), which
    callers set when they need a different pinned cert per appliance --
    installer, NSX, and vCenter are three hosts with three self-signed certs,
    so a single ``args.verify_tls`` can't carry a pinned path for all of them.
    Falls back to ``args.verify_tls`` (what standalone single-host actions use).
    """
    overrides = getattr(args, "verify_tls_by_host", None)
    if overrides and host in overrides:
        return overrides[host]
    return args.verify_tls

# Action groupings — actions are routed to different handlers/clients.
_BRINGUP_ACTIONS = {"start-bringup", "check-bringup"}
_DEPOT_ACTIONS = {
    "configure-depot",
    "get-depot-settings",
    "sync-depot",
    "list-bundles",
    "get-bundle",
    "download-bundle",
    "list-releases",
    "list-product-binaries",
    "download-product-binary",
    "download-all-product-binaries",
    "probe-product-binaries",
    "prepare-depot",
}
_NSX_ACTIONS = {"ping-nsx"}
_VCENTER_ACTIONS = {"ping-vcenter", "remove-installer-datastore"}

# AWS-only actions (no installer / NSX / vCenter — pure boto3).
_AWS_ACTIONS = {"destroy-ebs-volume"}

# Edge cluster actions touch both NSX and vCenter.
_EDGE_ACTIONS = {
    "prep-edge-cluster",
    "deploy-edge-nodes",
    "create-edge-cluster",
    "create-tier0",
    "create-tier1",
    "configure-routing",
    "create-anti-affinity",
    "deploy-edge-cluster",
}

# EVS connector actions (pure boto3, post-deployment).
_CONNECTOR_ACTIONS = {"create-connector"}

# Cross-handler one-shot — drives prepare-depot → start-bringup --wait →
# remove-installer-datastore → destroy-ebs-volume → deploy-edge-cluster
# → create-connector in sequence.
_PIPELINE_ACTIONS = {"deploy-vcf-and-edge"}

# Default spec paths, resolved relative to this source file. Operators can
# override via the BRINGUP_SPEC_PATH / EDGE_CLUSTER_SPEC_PATH env vars.
_DEFAULT_BRINGUP_SPEC_PATH = (
    os.environ.get("BRINGUP_SPEC_PATH")
    or str(Path(__file__).resolve().parent / "bringup_spec.json")
)
_DEFAULT_EDGE_CLUSTER_SPEC_PATH = (
    os.environ.get("EDGE_CLUSTER_SPEC_PATH")
    or str(Path(__file__).resolve().parent / "edge_cluster_spec.json")
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="VCF Phase 3 CLI: bringup (installer) and edge cluster (NSX direct)"
    )
    parser.add_argument(
        "action",
        choices=sorted(
            _BRINGUP_ACTIONS
            | _DEPOT_ACTIONS
            | _NSX_ACTIONS
            | _VCENTER_ACTIONS
            | _AWS_ACTIONS
            | _EDGE_ACTIONS
            | _CONNECTOR_ACTIONS
            | _PIPELINE_ACTIONS
        ),
        help="Action to perform",
    )

    # VCF Installer credentials (bringup + depot actions)
    parser.add_argument("--installer-host", default=None)
    parser.add_argument("--installer-username", default="admin@local")
    parser.add_argument(
        "--installer-password", default=None,
        help="DEPRECATED — visible via ps/proc and shell history. Prefer the "
             "VCF_INSTALLER_PASSWORD env var / Secrets Manager.",
    )

    # Depot / bundle / release flags (depot actions)
    parser.add_argument(
        "--depot-token",
        default=None,
        help="DEPRECATED (visible via ps/shell history) — prefer the "
             "VCF_DEPOT_TOKEN env var. Broadcom download token for "
             "configure-depot.",
    )
    parser.add_argument(
        "--depot-username",
        default=None,
        help="Optional Broadcom account username for configure-depot.",
    )
    parser.add_argument(
        "--depot-password",
        default=None,
        help="DEPRECATED (visible via ps/shell history) — prefer the "
             "VCF_DEPOT_PASSWORD env var. Optional Broadcom account password "
             "for configure-depot.",
    )
    parser.add_argument(
        "--wait",
        action="store_true",
        default=False,
        help="For sync-depot, download-bundle, download-all-product-binaries, "
             "and start-bringup: block until the operation reaches a "
             "terminal state. start-bringup polls every 10 minutes (a "
             "bringup typically runs 2-4.5 hours).",
    )
    parser.add_argument(
        "--bundle-id",
        default=None,
        help="Bundle id for get-bundle and download-bundle.",
    )
    parser.add_argument(
        "--bundle-product-type",
        default=None,
        help="Filter for list-bundles (e.g. VCF, NSX, VCENTER).",
    )
    parser.add_argument(
        "--bundle-type",
        default=None,
        help="Filter for list-bundles (e.g. INSTALL, PATCH, UPGRADE).",
    )
    parser.add_argument(
        "--applicable-for-version",
        default=None,
        help="Filter for list-releases.",
    )
    parser.add_argument(
        "--target-version",
        default=None,
        help="For download-all-product-binaries: only download bundles whose "
             "version starts with this (e.g. '9.0.2'). Omit to download every "
             "INSTALL-type bundle in the catalog across all versions.",
    )

    # NSX Manager credentials (edge cluster actions)
    parser.add_argument("--nsx-manager-host", default=None)
    parser.add_argument("--nsx-manager-username", default="admin")
    parser.add_argument("--nsx-manager-password", default=None)

    # vCenter credentials (edge cluster actions, for DVS port groups + DRS rule)
    parser.add_argument("--vcenter-host", default=None)
    parser.add_argument("--vcenter-username", default="administrator@vsphere.local")
    parser.add_argument("--vcenter-password", default=None)
    parser.add_argument(
        "--cluster-name",
        default=None,
        help="Cluster name for cluster-scoped vCenter actions (e.g. "
             "remove-installer-datastore). Defaults to "
             "clusterSpec.clusterName from bringup_spec.json when "
             "omitted.",
    )

    parser.add_argument("--spec-path", default=None)
    parser.add_argument("--workflow-id", default=None)

    # AWS Secrets Manager — used for runtime password resolution. The VCF
    # appliance passwords (vCenter, NSX, SDDC Manager, Operations, edge,
    # plus 9.0 fleet manager) are stored as
    # ``evs-<env_id>_<role>`` secrets by Phase 2. Phase 3 fetches
    # them at POST time so the on-disk spec JSON files stay inert.
    parser.add_argument(
        "--aws-profile",
        default=None,
        help="AWS profile for Secrets Manager. Falls back to "
             "$AWS_PROFILE / default credential chain.",
    )
    parser.add_argument(
        "--aws-region",
        default=None,
        help="AWS region for Secrets Manager. Falls back to "
             "$AWS_REGION / $AWS_DEFAULT_REGION (default: us-east-1).",
    )
    parser.add_argument(
        "--no-secrets-manager",
        action="store_true",
        default=False,
        help="Skip Secrets Manager wiring. Only useful for --dry-run; "
             "real bringup / edge deploys will fail if the spec still "
             "carries __SECRET placeholders.",
    )

    parser.add_argument(
        "--connector-secret-arn",
        default=None,
        help="Override the secret ARN for create-connector. If omitted, "
             "auto-resolves from evs-<env>_operationsAdmin in Secrets Manager.",
    )

    parser.add_argument(
        "--verify-tls",
        action="store_true",
        default=False,
        help="Verify the appliance's TLS certificate (default: skip verification)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="For start actions: show the spec without posting",
    )
    return parser.parse_args(argv)



def _read_spec_json(spec_path: str) -> dict:
    """Load a spec JSON file as a dict, or return an empty dict on miss.

    Used by the SDDC + NSX deployment precheck to gather
    placeholders without instantiating the typed deserializer.
    """
    path = Path(spec_path)
    if not path.exists():
        return {}
    return json.loads(path.read_text())


def _env_id_from_specs(args: argparse.Namespace) -> str | None:
    """Pull the EVS environment id from whichever spec file is on disk.

    Bringup spec carries it as ``__env__``, edge cluster spec as
    ``environmentId`` (either works). Returns ``None`` if neither is present or
    populated, in which case the SM fallback is skipped and we prompt.
    """
    bringup_path = args.spec_path or _DEFAULT_BRINGUP_SPEC_PATH
    edge_path = _DEFAULT_EDGE_CLUSTER_SPEC_PATH
    bringup_dict = _read_spec_json(bringup_path)
    edge_dict = _read_spec_json(edge_path)
    env_id = bringup_dict.get("__env__") or edge_dict.get("environmentId")
    if not env_id or str(env_id).startswith("PENDING"):
        return None
    return env_id


def _region_from_specs(args: argparse.Namespace) -> str | None:
    """Pull the AWS region from whichever spec file is on disk.

    Bringup spec carries it as ``__region__``, edge cluster spec as
    ``region``. Returns ``None`` if neither is populated, leaving
    ``_build_secrets_manager_client`` to fall back to ``--aws-region`` / env
    vars / the boto3 default chain.
    """
    bringup_path = args.spec_path or _DEFAULT_BRINGUP_SPEC_PATH
    edge_path = _DEFAULT_EDGE_CLUSTER_SPEC_PATH
    bringup_dict = _read_spec_json(bringup_path)
    edge_dict = _read_spec_json(edge_path)
    region = bringup_dict.get("__region__") or edge_dict.get("region")
    return region or None


def _cluster_name_from_bringup_spec(args: argparse.Namespace) -> str | None:
    """Pull ``clusterSpec.clusterName`` from the bringup spec on disk.

    Lets cluster-scoped actions (e.g. remove-installer-datastore) skip the
    ``--cluster-name`` flag when targeting the mgmt cluster Phase 2 provisioned.
    Returns ``None`` if absent -- caller falls back to the explicit flag.
    """
    bringup_path = args.spec_path or _DEFAULT_BRINGUP_SPEC_PATH
    bringup_dict = _read_spec_json(bringup_path)
    cluster_spec = bringup_dict.get("clusterSpec") or {}
    name = cluster_spec.get("clusterName")
    if not name or str(name).startswith("PENDING"):
        return None
    return name


def _build_secrets_manager_client(args: argparse.Namespace):
    """Build a boto3 ``secretsmanager`` client honoring CLI / env config.

    Returns ``None`` when ``--no-secrets-manager`` is set. Region resolution
    order: ``--aws-region``, ``$AWS_REGION``, ``$AWS_DEFAULT_REGION``, the spec
    files (``__region__``/``region``), then ``us-east-1``; the spec-derived
    region is the path exercised in practice. Auth is the default credential
    chain; we only add profile + region override hooks.
    """
    if args.no_secrets_manager:
        return None
    region = (
        args.aws_region
        or os.environ.get("AWS_REGION")
        or os.environ.get("AWS_DEFAULT_REGION")
        or _region_from_specs(args)
        or "us-east-1"
    )
    session_kwargs: dict[str, str] = {"region_name": region}
    if args.aws_profile:
        session_kwargs["profile_name"] = args.aws_profile
    session = boto3.Session(**session_kwargs)
    logger.info("Using AWS region for Secrets Manager: %s", region)
    return session.client("secretsmanager")


def _resolve_password_with_sm(
    cli_value: str | None,
    env_var: str,
    prompt: str,
    sm_client,
    env_id: str | None,
    role: str,
    cache_to_sm: bool = False,
) -> str:
    """Resolve a runtime auth password using CLI > env > Secrets Manager > prompt.

    Used by NSX + vCenter handlers, where the password is the same one bringup
    deployed (``nsxAdmin`` / ``vcenterSso``): with SM wired and the env id
    known, fetch directly and skip the prompt (falling back only if SM lookup
    fails).

    When ``cache_to_sm`` is True and the value came from CLI/env/prompt, it's
    written to ``evs-<env>_<role>`` -- used for the VCF Installer
    ``admin@local`` password, which Phase 2 doesn't auto-generate (the operator
    types it once, then future runs read it back).
    """
    from botocore.exceptions import ClientError

    if cli_value:
        if cache_to_sm:
            _stash_secret(sm_client, env_id, role, cli_value)
        return cli_value
    env_value = os.environ.get(env_var)
    if env_value:
        if cache_to_sm:
            _stash_secret(sm_client, env_id, role, env_value)
        return env_value
    if sm_client is not None and env_id:
        name = secret_name(env_id, role)
        try:
            response = sm_client.get_secret_value(SecretId=name)
            raw = response.get("SecretString") or ""
            if not raw and response.get("SecretBinary"):
                raw = response["SecretBinary"].decode("utf-8")
            if raw:
                try:
                    parsed = json.loads(raw)
                    if isinstance(parsed, dict) and "password" in parsed:
                        value = parsed["password"]
                    else:
                        value = raw
                except (ValueError, json.JSONDecodeError):
                    value = raw
                logger.info("Resolved %s from Secrets Manager (%s)", role, name)
                return value
            logger.warning(
                "Secret %s exists but its value is empty; falling back to prompt",
                name,
            )
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            logger.warning(
                "Couldn't fetch %s from Secrets Manager (%s); "
                "falling back to prompt",
                name, code or e,
            )
    value = getpass.getpass(prompt)
    if cache_to_sm:
        _stash_secret(sm_client, env_id, role, value)
    return value


def _stash_secret(sm_client, env_id: str | None, role: str, value: str) -> None:
    """Write a value into Secrets Manager so future runs can skip the prompt.

    Creates the secret if absent, updates it otherwise. Soft-fails: a stash
    failure must not break the bringup that just authenticated successfully.
    """
    from botocore.exceptions import ClientError

    if sm_client is None or not env_id or not value:
        return
    name = secret_name(env_id, role)
    try:
        sm_client.put_secret_value(SecretId=name, SecretString=value)
        logger.info("Stashed %s in Secrets Manager (%s)", role, name)
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code == "ResourceNotFoundException":
            try:
                sm_client.create_secret(
                    Name=name,
                    Description=(
                        f"VCF Installer admin@local password for env "
                        f"{env_id}. Stashed by Phase 3 after operator "
                        f"prompt; future runs read it back from here."
                    ),
                    SecretString=value,
                )
                logger.info("Created %s in Secrets Manager", name)
            except ClientError as create_err:
                logger.warning(
                    "Couldn't create %s in Secrets Manager (%s); "
                    "operator will be prompted again next run.",
                    name, create_err,
                )
        else:
            logger.warning(
                "Couldn't stash %s in Secrets Manager (%s); "
                "operator will be prompted again next run.",
                name, code or e,
            )


def _handle_bringup(args: argparse.Namespace) -> int:
    if not args.installer_host:
        logger.error("--installer-host is required for bringup actions")
        return 1

    sm_client = _build_secrets_manager_client(args)
    env_id = _env_id_from_specs(args)
    password = _resolve_password_with_sm(
        args.installer_password,
        "VCF_INSTALLER_PASSWORD",
        "VCF Installer password: ",
        sm_client,
        env_id,
        role="vcfInstaller",
        cache_to_sm=True,
    )
    client = InstallerClient(
        host=args.installer_host,
        username=args.installer_username,
        password=password,
        verify_tls=_resolve_verify_tls(args, args.installer_host),
    )
    spec_path = args.spec_path or _DEFAULT_BRINGUP_SPEC_PATH
    manager = BringupManager(client=client, spec_path=spec_path, sm_client=sm_client)

    if args.action == "start-bringup":
        response = manager.start(dry_run=args.dry_run, wait=args.wait)
        if not args.dry_run:
            print(json.dumps(response, indent=2, default=str))
        # Surface a non-zero exit when the workflow actually ended in failure,
        # so the deployment doesn't treat a failed bringup as success
        # and march into step 3. Only meaningful with ``--wait`` -- a no-wait
        # start returns the initial POST response, which we don't second-guess.
        if args.wait:
            status = (response.get("status") or "").upper()
            if is_bringup_failure(status):
                logger.error(
                    "Bringup workflow ended in %s; aborting with exit 1.",
                    status,
                )
                return 1
        return 0

    if args.action == "check-bringup":
        if not args.workflow_id:
            logger.error("--workflow-id is required for check-bringup")
            return 1
        response = manager.check(args.workflow_id)
        print(json.dumps(response, indent=2, default=str))
        return 0

    return 1


def _handle_depot(args: argparse.Namespace) -> int:
    if not args.installer_host:
        logger.error("--installer-host is required for depot actions")
        return 1

    sm_client = _build_secrets_manager_client(args)
    env_id = _env_id_from_specs(args)
    password = _resolve_password_with_sm(
        args.installer_password,
        "VCF_INSTALLER_PASSWORD",
        "VCF Installer password: ",
        sm_client,
        env_id,
        role="vcfInstaller",
        cache_to_sm=True,
    )
    client = InstallerClient(
        host=args.installer_host,
        username=args.installer_username,
        password=password,
        verify_tls=_resolve_verify_tls(args, args.installer_host),
    )
    manager = DepotManager(client)

    if args.action == "configure-depot":
        token = args.depot_token or os.environ.get("VCF_DEPOT_TOKEN")
        if not token:
            logger.error(
                "configure-depot requires --depot-token or VCF_DEPOT_TOKEN env var"
            )
            return 1
        result = manager.configure_depot(
            download_token=token,
            username=args.depot_username,
            password=args.depot_password,
        )
        print(json.dumps(result, indent=2, default=str))
        return 0

    if args.action == "get-depot-settings":
        result = manager.get_depot_settings()
        print(json.dumps(result, indent=2, default=str))
        return 0

    if args.action == "sync-depot":
        result = manager.sync_depot(wait=args.wait)
        print(json.dumps(result, indent=2, default=str))
        return 0

    if args.action == "list-bundles":
        result = manager.list_bundles(
            product_type=args.bundle_product_type,
            bundle_type=args.bundle_type,
        )
        print(json.dumps(result, indent=2, default=str))
        return 0

    if args.action == "get-bundle":
        if not args.bundle_id:
            logger.error("--bundle-id is required for get-bundle")
            return 1
        result = manager.get_bundle(args.bundle_id)
        print(json.dumps(result, indent=2, default=str))
        return 0

    if args.action == "download-bundle":
        if not args.bundle_id:
            logger.error("--bundle-id is required for download-bundle")
            return 1
        result = manager.download_bundle(args.bundle_id, wait=args.wait)
        print(json.dumps(result, indent=2, default=str))
        return 0

    if args.action == "list-releases":
        result = manager.list_releases(
            applicable_for_version=args.applicable_for_version,
        )
        print(json.dumps(result, indent=2, default=str))
        return 0

    if args.action == "probe-product-binaries":
        # Diagnostic probe — hit likely candidate endpoints and show what
        # status code / body each returns. No permanent behavior.
        return _handle_probe_product_binaries(client)

    if args.action == "list-product-binaries":
        # On 9.0 installers the SDK's list_bundles chokes on a schema drift
        # in Bundle.downloadStatus (SDK expects a struct, installer returns
        # a string). We go raw so the response is raw JSON dicts we can
        # trust. Matches the old `list-bundles` in spirit but works.
        query = {}
        if args.bundle_product_type:
            query["product_type"] = args.bundle_product_type
        if args.bundle_type:
            query["bundle_type"] = args.bundle_type
        result = client.raw_get("/v1/bundles", **query)
        print(json.dumps(result, indent=2, default=str))
        return 0

    if args.action == "download-product-binary":
        if not args.bundle_id:
            logger.error("--bundle-id is required for download-product-binary")
            return 1
        # /v1/bundles/{id} accepts a PATCH with BundleUpdateSpec to kick off
        # a download on 9.0. We use PATCH via session because raw_post
        # currently only does POST.
        client._ensure_authenticated()
        url = f"{client._base_url}/v1/bundles/{args.bundle_id}"
        logger.info("PATCH %s", url)
        response = client._session.patch(
            url,
            json={"bundleDownloadSpec": {"downloadNow": True}},
            timeout=client._timeout,
        )
        client._raise_for_rest_error(response)
        body = response.json() if response.content else None
        print(json.dumps(body, indent=2, default=str))
        return 0

    if args.action == "download-all-product-binaries":
        return _handle_download_all_product_binaries(
            client, wait=args.wait, target_version=args.target_version,
        )

    if args.action == "prepare-depot":
        return _handle_prepare_depot(client, manager, args)

    return 1


def _handle_prepare_depot(
    client: "InstallerClient",
    manager: "DepotManager",
    args: argparse.Namespace,
) -> int:
    """One-shot wrapper that runs all three depot prep actions in order:

      1. ``configure-depot`` — write the Broadcom token onto the installer.
      2. ``sync-depot --wait`` — refresh the bundle + release catalog.
      3. ``download-all-product-binaries --wait`` — pick the right INSTALL
         bundle per component type and trigger downloads, blocking until
         everything reaches a terminal state.

    Takes ``--depot-token`` / ``$VCF_DEPOT_TOKEN`` (step 1) and
    ``--target-version`` (step 3); ``--wait`` is implicit since partial
    pre-bringup state isn't useful. The three individual actions remain
    available for fine-grained control.
    """
    # Step 1: configure-depot.
    token = args.depot_token or os.environ.get("VCF_DEPOT_TOKEN")
    if not token:
        logger.error(
            "prepare-depot requires --depot-token or VCF_DEPOT_TOKEN env var"
        )
        return 1
    if not args.target_version:
        logger.error(
            "prepare-depot requires --target-version (e.g. '9.0.2') so the "
            "right INSTALL bundles get picked. Without it every component "
            "type would download its newest INSTALL across all VCF tracks."
        )
        return 1

    logger.info("prepare-depot 1/3: configure-depot")
    configure_result = manager.configure_depot(
        download_token=token,
        username=args.depot_username,
        password=args.depot_password,
    )
    print(json.dumps(configure_result, indent=2, default=str))

    # Step 2: sync-depot --wait.
    logger.info("prepare-depot 2/3: sync-depot --wait")
    sync_result = manager.sync_depot(wait=True)
    print(json.dumps(sync_result, indent=2, default=str))
    sync_status = (sync_result or {}).get("syncStatus", "").upper()
    # The installer reports a successful sync as SYNCED. Other green
    # states (SUCCESS / SUCCEEDED / COMPLETED) are accepted defensively
    # in case the API ever changes.
    if sync_status and sync_status not in {"SYNCED", "SUCCESS", "SUCCEEDED", "COMPLETED"}:
        logger.error(
            "Depot sync ended in non-success state '%s'; refusing to "
            "proceed to bundle downloads. Inspect the installer's "
            "errorMessage and rerun.",
            sync_status,
        )
        return 1

    # Step 3: download-all-product-binaries --wait.
    logger.info(
        "prepare-depot 3/3: download-all-product-binaries "
        "--target-version %s --wait", args.target_version,
    )
    return _handle_download_all_product_binaries(
        client, wait=True, target_version=args.target_version,
    )


def _download_pinned_bundles(
    client: "InstallerClient",
    bundles: list,
    pinned: dict[str, str],
    wait: bool,
    terminal_ok: set,
    terminal_fail: set,
) -> int:
    """Download bundles by pinned toVersion. Resolves each pinned version
    to a live bundle ID from the catalog, then triggers downloads."""

    # Build a (component_type, toVersion) -> bundle mapping from the catalog.
    # Key by both type and version because different components can share the
    # same toVersion string.
    tv_to_bundle: dict[tuple, dict] = {}
    for b in bundles:
        for c in b.get("components") or []:
            if (c.get("imageType") or "").upper() != "INSTALL":
                continue
            tv = c.get("toVersion") or ""
            ctype_key = c.get("type") or ""
            if tv and ctype_key:
                tv_to_bundle[(ctype_key, tv)] = b

    to_wait: list[str] = []
    missing: list[str] = []
    failed: list[str] = []
    for ctype, pinned_version in pinned.items():
        bundle = tv_to_bundle.get((ctype, pinned_version))
        if not bundle:
            missing.append(f"{ctype} ({pinned_version})")
            continue

        bundle_id = bundle.get("id")
        status = (bundle.get("downloadStatus") or "").upper()
        version = bundle.get("version", "?")

        if status in terminal_ok:
            logger.info("pin   %s (%s %s) already %s", bundle_id, ctype, version, status)
            continue
        if status in terminal_fail:
            logger.warning("pin   %s (%s %s) was %s — re-downloading", bundle_id, ctype, version, status)
        else:
            logger.info("pin   %s (%s %s) status=%s — downloading", bundle_id, ctype, version, status or "PENDING")

        client._ensure_authenticated()
        url = f"{client._base_url}/v1/bundles/{bundle_id}"
        response = client._session.patch(
            url,
            json={"bundleDownloadSpec": {"downloadNow": True}},
            timeout=client._timeout,
        )
        if not response.ok:
            logger.error("PATCH /v1/bundles/%s -> %s: %s", bundle_id, response.status_code, response.text[:300])
            failed.append(f"{ctype} ({bundle_id}): PATCH failed {response.status_code}")
            continue
        to_wait.append(bundle_id)

    if missing:
        logger.warning(
            "Pinned bundles not found in catalog for %d component(s): %s",
            len(missing), ", ".join(missing),
        )

    if failed:
        logger.error(
            "%d pinned bundle(s) failed to trigger download: %s",
            len(failed), ", ".join(failed),
        )
        return 1

    logger.info(
        "Pinned bundle download: triggered %d, already done %d, missing %d. "
        "Total pinned: %d.",
        len(to_wait), len(pinned) - len(to_wait) - len(missing), len(missing), len(pinned),
    )

    if not wait or not to_wait:
        return 0

    # Poll until all reach terminal state.
    logger.info("Polling until all %d pinned bundle(s) finish...", len(to_wait))
    deadline = _time.time() + 4 * 60 * 60
    pending = set(to_wait)
    poll_failed: list[str] = []
    while pending and _time.time() < deadline:
        _time.sleep(60)
        try:
            catalog = client.raw_get("/v1/bundles")
        except Exception:
            logger.warning("Poll fetch failed; retrying on next interval")
            continue
        current = {b.get("id"): b for b in (catalog or {}).get("elements") or []}
        for bundle_id in list(pending):
            bundle = current.get(bundle_id)
            if not bundle:
                continue
            status = (bundle.get("downloadStatus") or "").upper()
            if status in terminal_ok:
                logger.info("  %s -> %s", bundle_id, status)
                pending.discard(bundle_id)
            elif status in terminal_fail:
                logger.error("  %s -> %s", bundle_id, status)
                pending.discard(bundle_id)
                poll_failed.append(bundle_id)
        logger.info("  %d bundle(s) still pending", len(pending))

    if pending:
        logger.error("%d pinned bundle(s) did not finish: %s", len(pending), sorted(pending))
        return 1

    if poll_failed:
        logger.error("%d pinned bundle(s) failed during download: %s", len(poll_failed), poll_failed)
        return 1

    logger.info("All pinned bundles downloaded successfully.")
    return 0


def _handle_download_all_product_binaries(
    client: "InstallerClient", *, wait: bool, target_version: str | None,
) -> int:
    """List all bundles on /v1/bundles, pick exactly one INSTALL bundle per
    component type, kick off a download for each one still PENDING, and
    optionally poll until done.

    The installer puts real product identity on ``components[0].type`` (VROPS,
    VCENTER, NSX_T_MANAGER, ...); the top-level ``bundle.type`` is the useless
    umbrella ``VMWARE_SOFTWARE``, so we group by component type instead.

    Selection logic, per component type:
      1. Keep only bundles whose component carries
         ``imageType == "INSTALL"`` (skip PATCH / UPGRADE bundles).
      2. Keep only bundles whose ``version`` starts with
         ``target_version`` (e.g. ``9.0.2-<build>``). If nothing matches,
         log a WARN and skip the component. We deliberately do NOT fall
         back to newest-of-any-version — that risks pulling an
         off-track bundle (e.g. an NSX_ALB 32.x when targeting VCF
         9.0.2).
      3. Pick the newest remaining bundle — highest ``releasedDate``,
         tiebreaker = highest build suffix on ``version`` (the digits
         after the trailing ``-``).

    If ``target_version`` isn't passed, every component's newest INSTALL
    bundle is picked regardless of version track. Picks and drops are
    logged at INFO.
    """
    TERMINAL_OK = {"SUCCESSFUL", "COMPLETED"}
    TERMINAL_FAIL = {"FAILED", "CANCELLED"}

    # Pinned bundle versions per VCF release.
    _PINNED_BUNDLE_VERSIONS: dict[str, dict[str, str]] = {
        "9.0.2": {
            "VCENTER":             "9.0.2.0.25148086",
            "NSX_T_MANAGER":       "9.0.2.0.25150386",
            "SDDC_MANAGER":        "9.0.2.0.25151285",
            "VROPS":               "9.0.2.0.25137838",
            "VCF_OPS_CLOUD_PROXY": "9.0.2.0.25137840",
            "VRSLCM":              "9.0.2.0.25137839",
            "VRA":                 "9.0.2.0.25145732",
        },
        "9.1": {
            "VCENTER":                           "9.1.0.0100.25417926",
            "NSX_T_MANAGER":                     "9.1.0.0100.25470810",
            "SDDC_MANAGER":                      "9.1.0.0100.25428926",
            "VROPS":                             "9.1.0.0100.25435105",
            "VCF_OPS_CLOUD_PROXY":               "9.1.0.0100.25434833",
            "VCF_FLEET_LCM":                     "9.1.0.0100.25423358",
            "HCX":                               "9.1.0.0100.25426672",
            "VCF_SERVICE_VCD_MIGRATION_BACKEND": "9.1.0.0.25370929",
            "TELEMETRY_ACCEPTOR":                "9.1.0.0.25181946",
            "VCF_SALT":                          "9.1.0.0100.25434834",
            "VCF_SDDC_LCM":                      "9.1.0.0100.25423352",
            "VSP":                               "9.1.0.0.25370367",
            "VCF_LICENSE_SERVER":                "9.1.0.0100.25434835",
            "DEPOT_SERVICE":                     "9.1.0.0.25371105",
            "VCF_SALT_RAAS":                     "9.1.0.0100.25434834",
            "VIDB":                              "9.1.0.0.25368698",
            "VRA":                               "9.1.0.0100.25429499",
        },
    }

    # 1. Fetch the full bundle list.
    catalog = client.raw_get("/v1/bundles")
    bundles = (catalog or {}).get("elements") or []
    if not bundles:
        logger.error("/v1/bundles returned an empty catalog; nothing to download")
        return 1

    # 1b. Check if we have pinned versions for this target. Match by
    # prefix: "9.1" matches key "9.1", "9.0.2" matches key "9.0.2".
    version_prefix = (target_version or "").strip()
    pinned = None
    for pin_key in sorted(_PINNED_BUNDLE_VERSIONS.keys(), key=len, reverse=True):
        if version_prefix and version_prefix.startswith(pin_key):
            pinned = _PINNED_BUNDLE_VERSIONS[pin_key]
            break

    if pinned:
        return _download_pinned_bundles(client, bundles, pinned, wait, TERMINAL_OK, TERMINAL_FAIL)

    if version_prefix and not version_prefix.endswith("."):
        # Bundles on the wire look like "9.0.2-25151285" or "9.1.0-25371088".
        # When the user passes "9.0.2" we match with "9.0.2-" so "9.0.2"
        # doesn't also match "9.0.20". When the user passes "9.1" (no patch
        # segment) we match with "9.1." so it catches "9.1.0-*", "9.1.1-*".
        dot_count = version_prefix.count(".")
        version_prefix_dash = version_prefix + ("-" if dot_count >= 2 else ".")
    else:
        version_prefix_dash = version_prefix

    def _install_component_types(bundle: dict) -> list[str]:
        """Component ``type`` values for any INSTALL components this bundle
        carries. In practice each bundle has exactly one component, but the
        schema allows multiple so we handle the general case.
        """
        types: list[str] = []
        for comp in bundle.get("components") or []:
            if (comp.get("imageType") or "").upper() == "INSTALL":
                ctype = comp.get("type")
                if ctype and ctype not in types:
                    types.append(ctype)
        return types

    def _version_matches(bundle: dict) -> bool:
        if not version_prefix:
            return True
        bv = bundle.get("version") or ""
        return bv.startswith(version_prefix_dash) or bv == version_prefix

    def _is_ga_bundle(bundle: dict) -> bool:
        """True if the bundle's INSTALL component is a GA (base) release.

        VCF 9.x toVersion format: X.Y.Z.A.BUILD where A is the patch
        level (EEHH). GA has A == "0" or "0000"; express/hot patches
        have non-zero (e.g. "0100", "0200"). Patch-level INSTALL images
        are rejected by the installer for fresh deployments.
        """
        for comp in bundle.get("components") or []:
            if (comp.get("imageType") or "").upper() != "INSTALL":
                continue
            to_version = comp.get("toVersion") or ""
            parts = to_version.split(".")
            if len(parts) < 5:
                return True
            patch_field = parts[3]
            return patch_field in ("0", "0000")
        return True

    def _build_suffix(bundle: dict) -> int:
        """Extract the trailing build number from e.g. '9.0.2-25151285'.

        Returns 0 if we can't parse; the tiebreaker just degrades gracefully
        back to releasedDate order.
        """
        version = bundle.get("version") or ""
        _, _, suffix = version.rpartition("-")
        try:
            return int(suffix)
        except ValueError:
            return 0

    # 2. Bucket bundles by component type. Skip bundles with no INSTALL
    #    components (PATCH/UPGRADE-only) and patch-level INSTALL images
    #    (e.g. 9.0.2.0200), which the installer rejects for fresh deploys.
    by_component_type: dict[str, list[dict]] = {}
    skipped_patch = 0
    skipped_patch_install = 0
    for bundle in bundles:
        ctypes = _install_component_types(bundle)
        if not ctypes:
            skipped_patch += 1
            continue
        if not _is_ga_bundle(bundle):
            skipped_patch_install += 1
            continue
        for ctype in ctypes:
            by_component_type.setdefault(ctype, []).append(bundle)

    # 3. Per component type, pick exactly one bundle.
    picked: list[tuple[dict, str]] = []
    skipped_off_version = 0
    skipped_superseded = 0
    skipped_no_match: list[str] = []
    for ctype in sorted(by_component_type):
        group = sorted(
            by_component_type[ctype],
            key=lambda b: (b.get("releasedDate") or "", _build_suffix(b)),
            reverse=True,
        )
        if version_prefix:
            matching = [b for b in group if _version_matches(b)]
            if not matching:
                available = sorted({b.get("version") or "?" for b in group})
                logger.warning(
                    "skip  %s — no INSTALL bundle matches %s "
                    "(available: %s)",
                    ctype, version_prefix, ", ".join(available),
                )
                skipped_no_match.append(ctype)
                continue
            winner = matching[0]
            logger.info(
                "pick  %s (%s %s, released %s) — newest of %d INSTALL "
                "bundle(s) matching %s for %s",
                winner.get("id"), ctype, winner.get("version", "?"),
                winner.get("releasedDate", "?"), len(matching),
                version_prefix, ctype,
            )
        else:
            winner = group[0]
            logger.info(
                "pick  %s (%s %s, released %s) — newest of %d INSTALL "
                "bundle(s) for %s",
                winner.get("id"), ctype, winner.get("version", "?"),
                winner.get("releasedDate", "?"), len(group), ctype,
            )
        picked.append((winner, ctype))

        # Log every non-winner bundle for this component type.
        for loser in group:
            if loser is winner:
                continue
            if version_prefix and not _version_matches(loser):
                skipped_off_version += 1
                logger.info(
                    "drop  %s (%s %s, released %s) — doesn't match %s",
                    loser.get("id"), ctype, loser.get("version", "?"),
                    loser.get("releasedDate", "?"), version_prefix,
                )
            else:
                skipped_superseded += 1
                logger.info(
                    "drop  %s (%s %s, released %s) — superseded by %s",
                    loser.get("id"), ctype, loser.get("version", "?"),
                    loser.get("releasedDate", "?"), winner.get("id"),
                )

    # 4. Kick off a download for every picked bundle that isn't done yet.
    to_wait: list[str] = []
    for bundle, ctype in picked:
        bundle_id = bundle.get("id")
        status = (bundle.get("downloadStatus") or "").upper()
        version = bundle.get("version", "?")

        if status in TERMINAL_OK:
            logger.info(
                "skip  %s (%s %s) already %s", bundle_id, ctype, version, status
            )
            continue
        if status in TERMINAL_FAIL:
            logger.warning(
                "retry %s (%s %s) was %s — kicking off new download",
                bundle_id, ctype, version, status,
            )
        else:
            logger.info(
                "start %s (%s %s, %sMB) status=%s",
                bundle_id, ctype, version,
                bundle.get("sizeMB", "?"), status or "unknown",
            )

        url = f"{client._base_url}/v1/bundles/{bundle_id}"
        response = client._session.patch(
            url,
            json={"bundleDownloadSpec": {"downloadNow": True}},
            timeout=client._timeout,
        )
        if not response.ok:
            logger.warning(
                "PATCH /v1/bundles/%s -> %s: %s",
                bundle_id, response.status_code, response.text[:300],
            )
            continue
        to_wait.append(bundle_id)

    logger.info(
        "Triggered downloads for %d bundle(s). "
        "Skipped %d PATCH-only, %d patch-level INSTALL, %d off-version, "
        "%d superseded, %d component type(s) with no matching version (%s). "
        "Catalog total: %d.",
        len(to_wait), skipped_patch, skipped_patch_install,
        skipped_off_version, skipped_superseded,
        len(skipped_no_match), ", ".join(skipped_no_match) or "none",
        len(bundles),
    )

    if not wait:
        return 0

    # 5. Poll until every bundle lands in a terminal state.
    logger.info("Polling until all %d bundle(s) reach a terminal state...", len(to_wait))
    deadline = _time.time() + 4 * 60 * 60  # 4h default
    pending = set(to_wait)
    poll_failed = set()  # bundles that reached a TERMINAL_FAIL download state
    while pending and _time.time() < deadline:
        _time.sleep(60)
        try:
            catalog = client.raw_get("/v1/bundles")
        except Exception as e:  # noqa: BLE001
            # Transient network/server hiccup — log and retry on the next
            # loop. Session-level retries should catch most of these, but
            # belt-and-suspenders beats crashing a 4-hour wait.
            logger.warning(
                "Poll fetch failed (%s); retrying on next interval", e
            )
            continue
        current = {b.get("id"): b for b in (catalog or {}).get("elements") or []}
        for bundle_id in list(pending):
            bundle = current.get(bundle_id)
            if not bundle:
                continue
            status = (bundle.get("downloadStatus") or "").upper()
            if status in TERMINAL_OK:
                logger.info("  %s -> %s", bundle_id, status)
                pending.discard(bundle_id)
            elif status in TERMINAL_FAIL:
                logger.error(
                    "  %s -> %s (%s)",
                    bundle_id, status,
                    bundle.get("downloadStatus", "") or "no message",
                )
                poll_failed.add(bundle_id)
                pending.discard(bundle_id)
            else:
                logger.info(
                    "  %s downloading... (%s)", bundle_id, status or "unknown"
                )
        logger.info("  %d bundle(s) still pending", len(pending))

    if pending:
        logger.error(
            "%d bundle(s) did not finish within the 4h budget: %s",
            len(pending), sorted(pending),
        )
        return 1

    if poll_failed:
        logger.error(
            "%d bundle(s) reached a terminal FAILURE state and did NOT download: "
            "%s. The subsequent VCF bringup would fail on the missing bundle(s) — "
            "aborting rather than reporting false success.",
            len(poll_failed), sorted(poll_failed),
        )
        return 1

    logger.info("All bundles downloaded successfully.")
    return 0


def _handle_probe_product_binaries(client: "InstallerClient") -> int:
    """Hit a handful of candidate endpoints and print status+body for each.

    Used to reverse-engineer the 9.0 installer's replacement for
    /v1/bundles. Prints a terse table so we can pick the winners and wire
    them into permanent actions.
    """
    import json as _json
    candidates = [
        # Catalog endpoints that return bundle lists.
        ("GET", "/v1/bundles?product_type=VCF"),
        ("GET", "/v1/bundles"),
        # Candidates for /v1/product-binaries family. Earlier output showed
        # /v1/product-binaries refuses GET, so we try a handful of POST and
        # sub-path variants.
        ("OPTIONS", "/v1/product-binaries"),
        ("GET", "/v1/product-binaries/"),
        ("GET", "/v1/product-binaries?all=true"),
    ]
    for method, path in candidates:
        try:
            if method == "GET":
                result = client.raw_get(path)
            elif method == "POST":
                result = client.raw_post(path)
            elif method == "OPTIONS":
                # Rare but useful — tells us which methods the path accepts.
                client._ensure_authenticated()
                response = client._session.options(
                    f"{client._base_url}{path}", timeout=client._timeout
                )
                allowed = response.headers.get("Allow") or response.headers.get("allow")
                print(
                    f"{response.status_code} OPTIONS {path} allow={allowed!r}"
                )
                continue
            else:
                result = None
            body_str = _json.dumps(result)[:400] if result is not None else "<empty>"
            print(f"OK  {method} {path} -> {body_str}")
        except Exception as e:  # noqa: BLE001
            print(f"ERR {method} {path} -> {e}")
    return 0


def _handle_nsx(args: argparse.Namespace) -> int:
    if not args.nsx_manager_host:
        logger.error("--nsx-manager-host is required for NSX actions")
        return 1

    sm_client = _build_secrets_manager_client(args)
    env_id = _env_id_from_specs(args)
    password = _resolve_password_with_sm(
        args.nsx_manager_password,
        "VCF_NSX_MANAGER_PASSWORD",
        "NSX Manager password: ",
        sm_client,
        env_id,
        role="nsxAdmin",
    )
    client = NsxClient(
        host=args.nsx_manager_host,
        username=args.nsx_manager_username,
        password=password,
        verify_tls=_resolve_verify_tls(args, args.nsx_manager_host),
    )

    if args.action == "ping-nsx":
        # Connectivity + auth smoke test. Fetches NSX node version.
        response = client.ping()
        logger.info("NSX Manager reachable and authenticated")
        print(json.dumps(response, indent=2, default=str))
        return 0

    return 1


def _handle_vcenter(args: argparse.Namespace) -> int:
    if not args.vcenter_host:
        logger.error("--vcenter-host is required for vCenter actions")
        return 1

    sm_client = _build_secrets_manager_client(args)
    env_id = _env_id_from_specs(args)
    password = _resolve_password_with_sm(
        args.vcenter_password,
        "VCF_VCENTER_PASSWORD",
        "vCenter password: ",
        sm_client,
        env_id,
        role="vcenterSso",
    )
    client = VcenterClient(
        host=args.vcenter_host,
        username=args.vcenter_username,
        password=password,
        verify_tls=_resolve_verify_tls(args, args.vcenter_host),
    )

    if args.action == "ping-vcenter":
        response = client.ping()
        logger.info("vCenter reachable and authenticated")
        print(json.dumps(response, indent=2, default=str))
        return 0

    if args.action == "remove-installer-datastore":
        cluster_name = args.cluster_name or _cluster_name_from_bringup_spec(args)
        if not cluster_name:
            logger.error(
                "remove-installer-datastore couldn't resolve a cluster name. "
                "Pass --cluster-name explicitly, or run after Phase 2's "
                "post-evs-sync-config so bringup_spec.json carries "
                "clusterSpec.clusterName."
            )
            return 1
        logger.info("Using cluster name: %s", cluster_name)
        result = client.remove_local_installer_datastore(cluster_name)
        print(json.dumps(result, indent=2, default=str))
        return 0

    return 1


def _handle_aws(args: argparse.Namespace) -> int:
    """Handle pure-AWS actions that don't talk to any VCF appliance.

    Currently just ``destroy-ebs-volume`` -- detach + delete the EBS volume
    Phase 2 attached for the installer VMFS, identified by tag
    (``ManagedBy=phase2-automation`` + ``EnvironmentId=<env>``) and nothing
    else. Chained in the pipeline's step 4; also callable standalone.
    """
    if args.action != "destroy-ebs-volume":
        return 1

    env_id = _env_id_from_specs(args)
    if not env_id:
        logger.error(
            "destroy-ebs-volume couldn't resolve an environment id from "
            "bringup_spec.json:__env__ or edge_cluster_spec.json:"
            "environmentId. Run Phase 2's post-evs-sync-config so one "
            "of those files carries it."
        )
        return 1

    region = (
        args.aws_region
        or os.environ.get("AWS_REGION")
        or os.environ.get("AWS_DEFAULT_REGION")
        or _region_from_specs(args)
    )
    if not region:
        # Silently defaulting the region here is actively dangerous (unlike
        # _build_secrets_manager_client's us-east-1 fallback, which only READs
        # a secret): a wrong-region tag lookup finds nothing and returns
        # {"deleted": False, "reason": "not present"} -- indistinguishable from
        # "no volume", while the real volume keeps billing in its actual region
        # with no error surfaced. Fail loudly instead of guessing.
        logger.error(
            "destroy-ebs-volume could not resolve an AWS region from "
            "--aws-region, $AWS_REGION, $AWS_DEFAULT_REGION, or the spec "
            "files (bringup_spec.json:__region__ / "
            "edge_cluster_spec.json:region). Refusing to guess a region "
            "for a destructive/cleanup operation — pass --aws-region "
            "explicitly, set $AWS_REGION, or ensure Phase 2's "
            "post-evs-sync-config has run so the spec files carry the "
            "region."
        )
        return 1
    session_kwargs: dict[str, str] = {"region_name": region}
    if args.aws_profile:
        session_kwargs["profile_name"] = args.aws_profile
    session = boto3.Session(**session_kwargs)
    ec2 = session.client("ec2")
    logger.info(
        "Using AWS region for EBS destroy: %s (env %s)",
        region, env_id,
    )

    destroyer = EbsVolumeDestroyer(ec2, environment_id=env_id)
    result = destroyer.detach_and_delete(dry_run=args.dry_run)
    print(json.dumps(result, indent=2, default=str))
    return 0


def create_ops_manager_connector(args: argparse.Namespace) -> int:
    """Register the VCF Operations Manager connector with EVS.

    Calls CreateEnvironmentConnector with type OPERATIONS_MANAGER.
    Resolves applianceFqdn from the bringup spec and secretIdentifier
    from Secrets Manager (or --connector-secret-arn override).
    """
    from botocore.exceptions import ClientError

    env_id = _env_id_from_specs(args)
    if not env_id:
        logger.error(
            "create-connector couldn't resolve an environment id from "
            "bringup_spec.json:__env__ or edge_cluster_spec.json:"
            "environmentId. Run Phase 2's post-evs-sync-config first."
        )
        return 1

    region = (
        args.aws_region
        or os.environ.get("AWS_REGION")
        or os.environ.get("AWS_DEFAULT_REGION")
        or _region_from_specs(args)
    )
    if not region:
        logger.error(
            "create-connector: could not resolve an AWS region (no --aws-region, "
            "no AWS_REGION/AWS_DEFAULT_REGION, and none found in the specs). "
            "Refusing to guess a region for a write operation — pass --aws-region."
        )
        return 1
    session_kwargs: dict[str, str] = {"region_name": region}
    if args.aws_profile:
        session_kwargs["profile_name"] = args.aws_profile
    session = boto3.Session(**session_kwargs)
    logger.info("Using AWS region for create-connector: %s (env %s)", region, env_id)

    # Resolve applianceFqdn from bringup spec
    bringup_path = args.spec_path or _DEFAULT_BRINGUP_SPEC_PATH
    bringup_dict = _read_spec_json(bringup_path)
    ops_spec = bringup_dict.get("vcfOperationsSpec") or {}
    nodes = ops_spec.get("nodes") or []
    appliance_fqdn = nodes[0].get("hostname") if nodes else None
    if not appliance_fqdn:
        logger.error(
            "Cannot resolve applianceFqdn: bringup_spec.json doesn't have "
            "vcfOperationsSpec.nodes[0].hostname. Run post-evs-sync-config."
        )
        return 1

    # Resolve secretIdentifier (ARN)
    secret_arn = args.connector_secret_arn
    if not secret_arn:
        sm = session.client("secretsmanager")
        secret_name_str = f"evs-{env_id}_operationsAdmin"
        try:
            desc = sm.describe_secret(SecretId=secret_name_str)
            secret_arn = desc.get("ARN")
        except ClientError as e:
            logger.error(
                "Cannot resolve secret ARN for %s: %s. "
                "Pass --connector-secret-arn explicitly.",
                secret_name_str, e,
            )
            return 1

    if args.dry_run:
        logger.info("DRY RUN — would call create_environment_connector with:")
        print(json.dumps({
            "environmentId": env_id,
            "type": "OPERATIONS_MANAGER",
            "applianceFqdn": appliance_fqdn,
            "secretIdentifier": secret_arn,
        }, indent=2))
        return 0

    evs = session.client("evs")

    # Idempotency: check if an OPERATIONS_MANAGER connector already exists
    connector_id = None
    skip_create = False
    try:
        existing = evs.list_environment_connectors(environmentId=env_id)
        for c in existing.get("connectors", []):
            if c.get("type") == "OPERATIONS_MANAGER":
                state = c.get("state", "")
                connector_id = c.get("connectorId")
                logger.info(
                    "OPERATIONS_MANAGER connector already exists: id=%s state=%s",
                    connector_id, state,
                )
                if state == "ACTIVE":
                    print(json.dumps(c, indent=2, default=str))
                    return 0
                if state == "CREATING" and args.wait:
                    logger.info("Connector %s is CREATING; polling to completion...", connector_id)
                    skip_create = True
                elif state == "CREATING":
                    logger.info(
                        "Connector %s is CREATING; pass --wait to poll to completion.",
                        connector_id,
                    )
                    print(json.dumps(c, indent=2, default=str))
                    return 0
                elif state == "CREATE_FAILED":
                    logger.error("Connector %s is CREATE_FAILED; cannot retry without deletion", connector_id)
                    return 1
                else:
                    print(json.dumps(c, indent=2, default=str))
                    return 1
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        logger.warning(
            "Could not list existing connectors (%s: %s); proceeding to create",
            code, e,
        )
        if code in ("AccessDeniedException", "ValidationException"):
            raise

    if not skip_create:
        logger.info(
            "Creating OPERATIONS_MANAGER connector: env=%s fqdn=%s",
            env_id, appliance_fqdn,
        )
        response = evs.create_environment_connector(
            environmentId=env_id,
            type="OPERATIONS_MANAGER",
            applianceFqdn=appliance_fqdn,
            secretIdentifier=secret_arn,
        )
        print(json.dumps(response, indent=2, default=str))
        connector_id = response.get("connector", {}).get("connectorId")

        if not args.wait:
            return 0

    if not connector_id:
        logger.warning("No connectorId resolved; skipping poll")
        return 0

    # Poll until ACTIVE or failure
    logger.info("Polling connector %s until ACTIVE (timeout: 30 min)...", connector_id)
    deadline = _time.time() + 30 * 60
    while _time.time() < deadline:
        _time.sleep(30)
        try:
            connectors = evs.list_environment_connectors(environmentId=env_id)
            for c in connectors.get("connectors", []):
                if c.get("connectorId") == connector_id:
                    state = c.get("state", "")
                    logger.info("  connector %s state=%s", connector_id, state)
                    if state == "ACTIVE":
                        logger.info("Connector %s is ACTIVE", connector_id)
                        return 0
                    if state == "CREATE_FAILED":
                        logger.error("Connector %s reached CREATE_FAILED", connector_id)
                        return 1
                    break
        except ClientError as e:
            logger.warning("Poll failed: %s; retrying", e)

    logger.error("Connector %s did not reach ACTIVE within 30 minutes", connector_id)
    return 1


def _handle_pipeline(args: argparse.Namespace) -> int:
    """One-shot wrapper: prepare-depot → start-bringup → remove-installer-datastore → destroy-ebs-volume → deploy-edge-cluster → create-connector.

    Sequences the six Phase 3 stages end-to-end (populated config -> deployed
    VCF + edge cluster). Each substep delegates to its existing handler, so
    behavior matches running the steps individually.

    Pipeline:
      1. ``prepare-depot`` — configure the Broadcom token, sync the depot,
         download all INSTALL bundles for ``--target-version``. Total: 30-60 min.
      2. ``start-bringup --wait`` — POST the bringup spec, then poll the
         workflow every 10 minutes until it terminates. Total: 2-4.5 hours.
      3. ``remove-installer-datastore`` — storage-vMotion any VMs off
         the local installer VMFS to vSAN, then unmount the datastore.
         Total: 5-10 min depending on VM size.
      4. ``destroy-ebs-volume`` — detach + delete the EBS volume that
         hosted the now-unmounted VMFS. Tag-gated to the env so we
         can't accidentally remove the wrong volume. Total: ~30 sec.
      5. ``deploy-edge-cluster`` — run all 7 NSX edge stages in order.
         Cross-stage NSX lookups use poll-with-timeout helpers
         so the chain does not race NSX's policy-side
         realization. Total: 30-50 min.
      6. ``create-connector`` — register the VCF Operations Manager with
         EVS via CreateEnvironmentConnector. Polls until ACTIVE.
         Total: ~2-5 min.

    Total runtime: ~3-5.5 hours, dominated by step 2 (bringup).

    Required flags / env vars:
      - ``--installer-host``, ``$VCF_INSTALLER_PASSWORD``,
        ``--depot-token`` / ``$VCF_DEPOT_TOKEN``, ``--target-version``
      - ``--nsx-manager-host``, ``$VCF_NSX_MANAGER_PASSWORD``
      - ``--vcenter-host``, ``$VCF_VCENTER_PASSWORD``

    Step 2 always forces ``--wait`` regardless of the user flag -- no point
    starting bringup then failing into edge-cluster work that needs vCenter /
    NSX up.
    """
    # Validate every required input upfront so we don't burn 30 minutes
    # on prepare-depot just to fail at deploy-edge-cluster for a missing
    # arg.
    missing: list[str] = []
    if not args.installer_host:
        missing.append("--installer-host")
    if not args.target_version:
        missing.append("--target-version")
    if not args.nsx_manager_host:
        missing.append("--nsx-manager-host")
    if not args.vcenter_host:
        missing.append("--vcenter-host")
    if not (args.depot_token or os.environ.get("VCF_DEPOT_TOKEN")):
        missing.append("--depot-token (or $VCF_DEPOT_TOKEN)")
    if missing:
        logger.error(
            "deploy-vcf-and-edge requires: %s", ", ".join(missing),
        )
        return 1

    # Step 0: precheck -- validate every required Secrets Manager secret is
    # present BEFORE burning 30 min on prepare-depot. Finds every
    # ``__SECRET:<role>__`` placeholder in both specs and DescribeSecrets each,
    # failing fast with one error. Doesn't fetch values (plaintext stays out).
    if not args.no_secrets_manager:
        bringup_spec_path = args.spec_path or _DEFAULT_BRINGUP_SPEC_PATH
        edge_spec_path = _DEFAULT_EDGE_CLUSTER_SPEC_PATH
        bringup_dict = _read_spec_json(bringup_spec_path)
        edge_dict = _read_spec_json(edge_spec_path)

        env_id = bringup_dict.get("__env__") or edge_dict.get("environmentId")
        if not env_id or str(env_id).startswith("PENDING"):
            logger.error(
                "deploy-vcf-and-edge precheck: couldn't find a populated "
                "environmentId in either spec (bringup __env__: %r, "
                "edge environmentId: %r). Run Phase 2's "
                "post-evs-sync-config first.",
                bringup_dict.get("__env__"),
                edge_dict.get("environmentId"),
            )
            return 1

        logger.info(
            "deploy-vcf-and-edge 0/3: precheck — verifying VCF appliance "
            "secrets are present in AWS Secrets Manager for env %s",
            env_id,
        )
        sm_client = _build_secrets_manager_client(args)
        try:
            SecretResolver(sm_client, env_id).preflight(bringup_dict, edge_dict)
        except MissingSecretsError as e:
            logger.error("deploy-vcf-and-edge precheck failed: %s", e)
            return 1

    # Resolve the VCF Installer password once (after precheck) so steps 1-2,
    # which both auth to the installer, don't each prompt independently. Push
    # it into ``args`` so downstream handlers see it via the normal CLI path.
    # ``cache_to_sm=True`` stashes the operator-typed password under
    # ``evs-<env>_vcfInstaller`` so later runs read it back and skip the prompt.
    sm_client_for_pw = _build_secrets_manager_client(args)
    env_id_for_pw = _env_id_from_specs(args)
    args.installer_password = _resolve_password_with_sm(
        args.installer_password,
        "VCF_INSTALLER_PASSWORD",
        "VCF Installer password: ",
        sm_client_for_pw,
        env_id_for_pw,
        role="vcfInstaller",
        cache_to_sm=True,
    )

    original_action = args.action

    # Post-bringup resume detection: once bringup COMPLETED_WITH_SUCCESS the
    # appliance transitions VCF Installer -> SDDC Manager and (on 9.1) retires
    # the depot API (GET/PATCH /v1/system/settings/depot returns 410
    # API_NO_LONGER_SUPPORTED), so a resume must skip straight to step 3.
    bringup_done = False
    try:
        _probe = InstallerClient(
            host=args.installer_host,
            username=args.installer_username,
            password=args.installer_password,
            verify_tls=_resolve_verify_tls(args, args.installer_host),
        )
        for _task in _probe.list_bringup_tasks() or []:
            _status = str(getattr(_task, "status", "")).upper()
            if "COMPLETED_WITH_SUCCESS" in _status:
                bringup_done = True
                break
    except Exception as e:  # pragma: no cover - probe is best-effort
        logger.debug("Bringup-completion probe failed (%s); running full pipeline", e)

    if bringup_done:
        logger.info(
            "Bringup already COMPLETED_WITH_SUCCESS — skipping steps 1-2 "
            "(prepare-depot, start-bringup). The installer depot API is "
            "retired post-bringup on 9.1."
        )
    else:
        # Step 1: prepare-depot. Mutate the action to route into _handle_depot,
        # which will dispatch to _handle_prepare_depot internally. Restore the
        # original action afterward in case the caller wants to inspect it.
        logger.info("deploy-vcf-and-edge 1/6: prepare-depot")
        args.action = "prepare-depot"
        rc = _handle_depot(args)
        if rc != 0:
            args.action = original_action
            return rc

        # Step 2: start-bringup --wait. Same trick: route into _handle_bringup
        # with --wait forced on so we don't run ahead of bringup completion.
        logger.info("deploy-vcf-and-edge 2/6: start-bringup --wait")
        args.action = "start-bringup"
        original_wait = args.wait
        args.wait = True
        rc = _handle_bringup(args)
        args.wait = original_wait
        if rc != 0:
            args.action = original_action
            return rc

    # Step 3: drop the local installer VMFS now that vSAN is up (storage-
    # vMotions remaining VMs off first); the EBS detach+delete is step 4.
    # Bringup is complete here -- installer transitioned to SDDC Manager,
    # vCenter/NSX up, VMCA certs in place. If the caller gave a
    # post_bringup_hook (deploy_orchestrator uses it to upgrade TLS from
    # unverified to VMCA CA-trust), invoke it before any post-bringup clients.
    post_bringup_hook = getattr(args, "post_bringup_hook", None)
    if post_bringup_hook:
        post_bringup_hook()

    logger.info("deploy-vcf-and-edge 3/6: remove-installer-datastore")
    args.action = "remove-installer-datastore"
    rc = _handle_vcenter(args)
    if rc != 0:
        args.action = original_action
        return rc

    # Step 4: destroy-ebs-volume. The datastore is unmounted, the SDDC
    # Manager VM is on vSAN, so the EBS volume is now safe to detach
    # and delete on the AWS side. Tag-gated; idempotent if already gone.
    logger.info("deploy-vcf-and-edge 4/6: destroy-ebs-volume")
    args.action = "destroy-ebs-volume"
    rc = _handle_aws(args)
    if rc != 0:
        args.action = original_action
        return rc

    # Step 5: deploy-edge-cluster (chains all 7 edge stages internally).
    logger.info("deploy-vcf-and-edge 5/6: deploy-edge-cluster")
    args.action = "deploy-edge-cluster"
    rc = _handle_edge(args)
    if rc != 0:
        args.action = original_action
        return rc

    # Step 6: create-connector. Register Ops Manager with EVS.
    logger.info("deploy-vcf-and-edge 6/6: create-connector")
    args.action = "create-connector"
    rc = create_ops_manager_connector(args)
    args.action = original_action
    return rc


def _handle_edge(args: argparse.Namespace) -> int:
    missing = [
        name for name, val in [
            ("--nsx-manager-host", args.nsx_manager_host),
            ("--vcenter-host", args.vcenter_host),
        ]
        if not val
    ]
    if missing:
        logger.error("%s required for edge cluster actions", " and ".join(missing))
        return 1

    sm_client = _build_secrets_manager_client(args)
    env_id = _env_id_from_specs(args)
    nsx_password = _resolve_password_with_sm(
        args.nsx_manager_password,
        "VCF_NSX_MANAGER_PASSWORD",
        "NSX Manager password: ",
        sm_client,
        env_id,
        role="nsxAdmin",
    )
    vcenter_password = _resolve_password_with_sm(
        args.vcenter_password,
        "VCF_VCENTER_PASSWORD",
        "vCenter password: ",
        sm_client,
        env_id,
        role="vcenterSso",
    )

    nsx = NsxClient(
        host=args.nsx_manager_host,
        username=args.nsx_manager_username,
        password=nsx_password,
        verify_tls=_resolve_verify_tls(args, args.nsx_manager_host),
    )
    vcenter = VcenterClient(
        host=args.vcenter_host,
        username=args.vcenter_username,
        password=vcenter_password,
        verify_tls=_resolve_verify_tls(args, args.vcenter_host),
    )
    spec_path = args.spec_path or _DEFAULT_EDGE_CLUSTER_SPEC_PATH
    manager = EdgeClusterManager(
        nsx=nsx,
        vcenter=vcenter,
        vcenter_host=args.vcenter_host,
        spec_path=spec_path,
        sm_client=sm_client,
    )

    if args.action == "prep-edge-cluster":
        manager.prep_resources(dry_run=args.dry_run)
        return 0

    if args.action == "deploy-edge-nodes":
        manager.deploy_edge_nodes(dry_run=args.dry_run)
        return 0

    if args.action == "create-edge-cluster":
        manager.create_edge_cluster(dry_run=args.dry_run)
        return 0

    if args.action == "create-tier0":
        manager.create_tier0(dry_run=args.dry_run)
        return 0

    if args.action == "create-tier1":
        manager.create_tier1(dry_run=args.dry_run)
        return 0

    if args.action == "configure-routing":
        manager.configure_routing(dry_run=args.dry_run)
        return 0

    if args.action == "create-anti-affinity":
        manager.create_anti_affinity_rule(dry_run=args.dry_run)
        return 0

    if args.action == "deploy-edge-cluster":
        manager.deploy_end_to_end(dry_run=args.dry_run)
        return 0

    return 1


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)

    # Pipeline action goes first so it can mutate args.action when
    # dispatching to the underlying handlers without re-entering this
    # dispatcher.
    if args.action in _PIPELINE_ACTIONS:
        return _handle_pipeline(args)

    if args.action in _BRINGUP_ACTIONS:
        return _handle_bringup(args)
    if args.action in _DEPOT_ACTIONS:
        return _handle_depot(args)
    if args.action in _NSX_ACTIONS:
        return _handle_nsx(args)
    if args.action in _VCENTER_ACTIONS:
        return _handle_vcenter(args)
    if args.action in _AWS_ACTIONS:
        return _handle_aws(args)
    if args.action in _EDGE_ACTIONS:
        return _handle_edge(args)
    if args.action in _CONNECTOR_ACTIONS:
        return create_ops_manager_connector(args)

    return 1


if __name__ == "__main__":
    sys.exit(main())
