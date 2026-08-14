#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Comprehensive pre-launch quota check for the EVS Deployment Orchestrator.

Reads your blueprint, computes exactly what the deployment will consume,
compares it against your account's service quotas AND current usage, and
tells you what to do about any shortfall — before you launch anything.

Covers every quota-bound resource actually created by the bootstrap
CloudFormation stack, the orchestrator's landing-zone (aws_config) stage,
and Amazon EVS itself:

  EC2/VPC   - bare-metal vCPUs, VPC count, Elastic IPs, security groups,
              subnets per VPC, route tables per VPC, NAT gateways per VPC
  Route53Resolver - resolver endpoints per region (the inbound endpoint
              the landing zone creates for VLAN DNS)
  Route53   - hosted zones per account (forward + reverse zone; uses the
              legacy get-account-limit API, since Route 53 zone limits are
              not exposed through Service Quotas)
  EVS       - environment count per account, host count per environment
              (the two EVS-specific limits that generic EC2/VPC checks
              cannot see, and the ones most likely to bind for anyone
              running more than a couple of environments at once)
  Secrets Manager - total secrets (shown for completeness; the default of
              500,000 is essentially never binding, but a custom
              account-level restriction would still be caught)

Not checked, on purpose:
  - EC2 key pairs: confirmed no adjustable Service Quotas entry exists for
    this resource (unlimited).
  - VPC Route Server / endpoints / peers: no distinct AWS-adjustable quota
    code exists for these as of this writing.
  - Transit Gateway (only relevant if your blueprint sets
    aws_config.create_tgw): no quota code could be confirmed live for this
    account; add one yourself if you rely on this optional feature and hit
    a real limit.

Usage:
    python3 check-quotas.py --blueprint blueprint.yaml --region us-east-2 \
        --availability-zone us-east-2a

Exit codes:
    0 - every check ran and passed, nothing skipped
    1 - every check that ran passed, but at least one was skipped
        (e.g. --availability-zone omitted) — verify those manually
    2 - a setup/usage error (bad blueprint, bad --availability-zone,
        credentials unreachable) meant no checks could run at all
    3 - at least one check ran AND failed — you don't have the quota
        headroom for this deployment as configured

Requires only boto3 + AWS credentials (both already present in AWS
CloudShell — no installs needed there). PyYAML is used if available,
with a built-in fallback parser otherwise.
Read-only: makes no changes to your account.
"""

import argparse
import sys

try:
    import boto3
except ImportError:
    print("boto3 is required. It's preinstalled in AWS CloudShell — or: pip3 install boto3")
    sys.exit(2)

try:
    import yaml  # optional — falls back to a minimal parser below
except ImportError:
    yaml = None


def _mini_parse(path):
    """Extract just the fields this check needs from the blueprint without
    PyYAML: evs.instance_type, hostnames.esxi length, jumpbox/hcx enabled,
    hcx.public_cidr. Good enough for the well-formed blueprint format."""
    import re
    section = None
    last_key = None
    out = {"evs": {}, "hostnames": {}, "jumpbox": {}, "hcx": {}}
    for raw in open(path):
        line = raw.split("#", 1)[0].rstrip()
        if not line.strip():
            continue
        if not line.startswith(" "):
            section = line.rstrip(":").strip()
            last_key = None
            continue
        # block-style list items ("  - esxi01") under the last seen key
        mi = re.match(r"\s+-\s*(.+)$", line)
        if mi and section in out and last_key == "esxi":
            out[section].setdefault("esxi", []).append(
                mi.group(1).strip().strip('"').strip("'"))
            continue
        m = re.match(r"\s+([A-Za-z_]+):\s*(.*)$", line)
        if not m or section not in out:
            continue
        last_key = m.group(1)
        key, val = m.group(1), m.group(2).strip().strip('"').strip("'")
        if key == "esxi":
            out[section][key] = re.findall(r"[\w-]+", val)
        elif val.lower() in ("true", "false"):
            out[section][key] = val.lower() == "true"
        elif val:
            out[section][key] = val
    return out


# Each tuple: (service_code, quota_code, display_name)
VCPU_QUOTA = ("ec2", "L-1216C47A", "Running On-Demand Standard instances (vCPUs)")
VPC_QUOTA = ("vpc", "L-F678F1CE", "VPCs per region")
EIP_QUOTA = ("ec2", "L-0263D0A3", "Elastic IPs per region")
SG_QUOTA = ("vpc", "L-E79EC296", "VPC security groups per region")
SUBNET_QUOTA = ("vpc", "L-407747CB", "Subnets per VPC")
RTB_QUOTA = ("vpc", "L-589F43AA", "Route tables per VPC")
NATGW_QUOTA = ("vpc", "L-FE5A380F", "NAT gateways per Availability Zone")
RESOLVER_EP_QUOTA = ("route53resolver", "L-4A669CC0", "Resolver endpoints per region")
EVS_ENV_QUOTA = ("evs", "L-27E780D9", "EVS environments per account")
EVS_HOST_QUOTA = ("evs", "L-96A49955", "Hosts per EVS environment")
SECRETS_QUOTA = ("secretsmanager", "L-2F66C23C", "Secrets Manager secrets per account")

GREEN, RED, YELLOW, RESET = "\033[92m", "\033[91m", "\033[93m", "\033[0m"

# Per-environment secret count: 1 ESXi root secret per host, plus these
# fixed appliance-role secrets (vcenterSso, vcenterRoot, nsxAdmin, nsxRoot,
# sddcManagerRoot, operationsAdmin, edgeAppliance) created during bringup.
APPLIANCE_SECRETS_PER_ENV = 7


def get_quota(sq, service, code):
    try:
        return sq.get_service_quota(ServiceCode=service, QuotaCode=code)["Quota"]["Value"]
    except sq.exceptions.NoSuchResourceException:
        return sq.get_aws_default_service_quota(
            ServiceCode=service, QuotaCode=code)["Quota"]["Value"]


def running_standard_vcpus(ec2):
    """Sum vCPUs of running/pending instances in Standard quota families."""
    total = 0
    paginator = ec2.get_paginator("describe_instances")
    for page in paginator.paginate(
        Filters=[{"Name": "instance-state-name", "Values": ["running", "pending"]}]
    ):
        for res in page["Reservations"]:
            for inst in res["Instances"]:
                family = ""
                for ch in inst["InstanceType"].split(".")[0]:
                    if not ch.isalpha():
                        break
                    family += ch
                if family.lower() in {"a", "c", "d", "h", "i", "m", "r", "t", "z"}:
                    cpu = inst.get("CpuOptions", {})
                    total += (cpu.get("CoreCount") or 0) * (cpu.get("ThreadsPerCore") or 1)
    return total


def count_secrets(sm):
    total = 0
    paginator = sm.get_paginator("list_secrets")
    for page in paginator.paginate():
        total += len(page.get("SecretList", []))
    return total


def count_subnets(ec2, vpc_id=None):
    """Subnets per VPC is a per-VPC quota. When vpc_id is None (a fresh VPC
    this deployment is about to create), usage is always 0."""
    if vpc_id is None:
        return 0
    total = 0
    paginator = ec2.get_paginator("describe_subnets")
    for page in paginator.paginate(Filters=[{"Name": "vpc-id", "Values": [vpc_id]}]):
        total += len(page.get("Subnets", []))
    return total


def count_route_tables(ec2, vpc_id=None):
    """Route tables per VPC is a per-VPC quota. When vpc_id is None (a
    fresh VPC this deployment is about to create), usage is always 0."""
    if vpc_id is None:
        return 0
    total = 0
    paginator = ec2.get_paginator("describe_route_tables")
    for page in paginator.paginate(Filters=[{"Name": "vpc-id", "Values": [vpc_id]}]):
        total += len(page.get("RouteTables", []))
    return total


# EC2 caps most describe_* Filters at 200 values per filter — chunk any
# subnet-id list built for the NAT-gateway-per-AZ join so an AZ with more
# than 200 subnets doesn't error the whole check.
_EC2_FILTER_VALUE_CHUNK = 200


def _chunked(seq, size):
    for i in range(0, len(seq), size):
        yield seq[i:i + size]


def count_nat_gateways(ec2, availability_zone=None):
    """NAT gateways per Availability Zone is an AZ-scoped quota (not per-VPC or
    per-region) — usage must be counted across every VPC in the target AZ. With
    no availability_zone the caller must skip this check; it can't be scoped."""
    if not availability_zone:
        raise ValueError("count_nat_gateways requires an availability_zone to scope correctly")

    # NAT gateways don't carry AZ directly on the API response; join
    # through the subnet's AZ instead. describe_subnets is paginated —
    # an account can have far more than one page of subnets in one AZ.
    subnet_ids = []
    paginator = ec2.get_paginator("describe_subnets")
    for page in paginator.paginate(
        Filters=[{"Name": "availability-zone", "Values": [availability_zone]}]
    ):
        subnet_ids.extend(s["SubnetId"] for s in page.get("Subnets", []))
    if not subnet_ids:
        return 0

    total = 0
    nat_paginator = ec2.get_paginator("describe_nat_gateways")
    base_filter = {"Name": "state", "Values": ["pending", "available"]}
    for chunk in _chunked(subnet_ids, _EC2_FILTER_VALUE_CHUNK):
        filters = [base_filter, {"Name": "subnet-id", "Values": chunk}]
        for page in nat_paginator.paginate(Filter=filters):
            total += len(page.get("NatGateways", []))
    return total


def count_resolver_endpoints(r53r):
    total = 0
    paginator = r53r.get_paginator("list_resolver_endpoints")
    for page in paginator.paginate():
        total += len(page.get("ResolverEndpoints", []))
    return total


def count_evs_environments(evs):
    total = 0
    next_token = None
    while True:
        # Request param is lowercase 'nextToken' (matches the response key);
        # 'NextToken' raises ParamValidationError on the 2nd page.
        kwargs = {"nextToken": next_token} if next_token else {}
        resp = evs.list_environments(**kwargs)
        # Exclude DELETED environments — they don't count against the
        # per-account quota. Other states are counted (conservative: the
        # preflight must never under-report and let a doomed deploy start).
        total += sum(
            1 for e in resp.get("environmentSummaries", [])
            if e.get("environmentState") != "DELETED"
        )
        next_token = resp.get("nextToken")
        if not next_token:
            break
    return total


def get_hosted_zone_limit_and_usage(r53):
    """Route 53 hosted zone limits aren't exposed through Service Quotas (the
    route53 code returns NoSuchResourceException — zones are a global, account-
    level resource), so this uses the legacy get-account-limit API instead."""
    limit = r53.get_account_limit(Type="MAX_HOSTED_ZONES_BY_OWNER")["Limit"]["Value"]
    used = 0
    paginator = r53.get_paginator("list_hosted_zones")
    for page in paginator.paginate():
        used += len(page.get("HostedZones", []))
    return limit, used


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--blueprint", required=True, help="Path to your customized blueprint.yaml")
    ap.add_argument("--region", required=True, help="AWS region (e.g. us-east-2)")
    ap.add_argument("--profile", help="AWS profile name (optional)")
    ap.add_argument("--byo-vpc", action="store_true",
                    help="You will deploy into an existing VPC (skips the VPC-count check). "
                         "Pass --existing-vpc-id together with this to also get accurate "
                         "subnets/route-tables-per-VPC usage for that specific VPC; without "
                         "it, those two checks are skipped entirely rather than silently "
                         "assuming 0 usage for a VPC that may already have other resources "
                         "in it.")
    ap.add_argument("--existing-vpc-id", help="VPC ID to scope the subnets/route-tables-per-VPC "
                    "checks against when using --byo-vpc. Required together with --byo-vpc to "
                    "run those two checks at all.")
    ap.add_argument("--availability-zone", help="Availability Zone this deployment will "
                    "launch into (e.g. us-east-2a) — same value as the CFN AvailabilityZone "
                    "stack parameter. Required to scope the NAT-gateways-per-AZ check "
                    "correctly; if omitted, that check is skipped with a warning rather than "
                    "silently under- or over-counting.")
    args = ap.parse_args()

    if args.availability_zone:
        az = args.availability_zone
        # Must be exactly <region> + one lowercase letter (e.g. 'us-east-2a').
        # A plain [:-1] == region check would accept 'us-east-2A' or 'us-east-21' —
        # invalid AZ names that silently report 0 NAT usage instead of erroring.
        valid_az = (
            az[:-1] == args.region
            and len(az) > len(args.region)
            and az[-1].isalpha()
            and az[-1].islower()
        )
        if not valid_az:
            print(f"--availability-zone '{az}' is not a valid AZ name for --region "
                  f"'{args.region}' (expected a name like '{args.region}a' — the region "
                  f"followed by exactly one lowercase letter). Double-check this value — "
                  f"the NAT-gateway check silently reports 0 usage for a wrong/unavailable "
                  f"AZ rather than erroring.")
            sys.exit(2)

    if yaml is not None:
        with open(args.blueprint) as f:
            bp = yaml.safe_load(f)
    else:
        bp = _mini_parse(args.blueprint)
    bp = bp or {}  # an empty/comment-only blueprint file parses to None

    evs_cfg = bp.get("evs") or {}
    instance_type = evs_cfg.get("instance_type", "")
    host_count = len((bp.get("hostnames") or {}).get("esxi", []) or [])
    jumpbox = ((bp.get("jumpbox") or {}).get("enabled")) is True
    hcx = ((bp.get("hcx") or {}).get("enabled")) is True
    hcx_byo_cidr = bool((bp.get("hcx") or {}).get("public_cidr"))

    if not instance_type or not host_count:
        print("Blueprint is missing evs.instance_type or hostnames.esxi — fix that first.")
        sys.exit(2)

    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    ec2 = session.client("ec2")
    sq = session.client("service-quotas")
    sm = session.client("secretsmanager")
    r53 = session.client("route53")
    r53r = session.client("route53resolver")
    evs = session.client("evs")

    try:
        vcpus_per_host = ec2.describe_instance_types(InstanceTypes=[instance_type])[
            "InstanceTypes"][0]["VCpuInfo"]["DefaultVCpus"]

        jumpbox_vcpus = 0
        if jumpbox:
            jb_type = (bp.get("jumpbox", {}) or {}).get("instance_type") or "t3.xlarge"
            jumpbox_vcpus = ec2.describe_instance_types(InstanceTypes=[jb_type])[
                "InstanceTypes"][0]["VCpuInfo"]["DefaultVCpus"]
    except Exception as e:
        print(f"{RED}Could not reach AWS to look up instance type vCPU counts "
              f"({type(e).__name__}: {e}).{RESET}\n"
              f"Check your credentials, region, and permissions before retrying — "
              f"no quota checks could be run.")
        return 2

    # --- What this deployment needs -----------------------------------------
    # Runner default is t3.large (2 vCPUs).
    need_vcpus = host_count * vcpus_per_host + 2 + jumpbox_vcpus
    need_vpcs = 0 if args.byo_vpc else 1
    # EIPs: 1 for the NAT gateway, plus HCX: 3 auto-allocated public IPs when
    # IPAM provisions the /28, or 1 CFN-created EIP when you bring your own CIDR.
    # HCX's public IPs (whether IPAM-auto-provisioned or BYO-CIDR) don't draw
    # from the standard EIP quota at all -- see _check_eip below (confirmed
    # live: ec2:AllocateAddress succeeded for a new standard EIP in an
    # account already showing more EIPs than this quota's limit, because the
    # HCX/IPAM-pool addresses don't count against it). Only the NAT gateway
    # allocates a standard-pool EIP.
    need_eips = 1
    # Security groups: bootstrap runner SG + landing-zone EVS-service-access
    # SG, plus one more if the jumpbox is enabled.
    need_sgs = 2 + (1 if jumpbox else 0)
    # Subnets: the bootstrap template creates exactly 2 (public + runner); the
    # landing zone reuses the passed-in service-access subnet, so BYO-VPC needs 0.
    need_subnets = 0 if args.byo_vpc else 2
    # Route tables per VPC: 2 from the bootstrap template (public +
    # service-access), unless BYO-VPC supplies its own.
    need_route_tables = 0 if args.byo_vpc else 2
    # NAT gateways per Availability Zone: this deployment always adds
    # exactly 1 (the landing zone's NAT gateway).
    need_nat_gateways = 1
    # Resolver endpoints per region: always exactly 1 (inbound endpoint for
    # VLAN DNS), regardless of BYO-VPC.
    need_resolver_endpoints = 1
    # Route 53 hosted zones per account: forward + reverse zone, always 2.
    need_hosted_zones = 2
    # EVS: this deployment is exactly 1 new environment with `host_count` hosts.
    need_evs_environments = 1
    need_evs_hosts_per_env = host_count
    # Secrets Manager: 1 ESXi root secret per host + fixed appliance-role
    # secrets created during bringup.
    need_secrets = host_count + APPLIANCE_SECRETS_PER_ENV

    checks = []
    skipped_checks = []

    def _safe_check(name, fn, *fn_args):
        """Run one quota check in isolation. A failure here (throttling, access
        denied, service unavailable in-region, a wrong quota code) is recorded
        and reported at the end instead of aborting checks that already ran."""
        try:
            fn(*fn_args)
        except Exception as e:
            skipped_checks.append((name, f"{type(e).__name__}: {e}"))

    # --- EC2 / VPC -----------------------------------------------------------
    def _check_vcpu():
        quota = get_quota(sq, *VCPU_QUOTA[:2])
        used = running_standard_vcpus(ec2)
        checks.append((VCPU_QUOTA[2], need_vcpus, used, quota,
                       f"{host_count} x {instance_type} ({vcpus_per_host} vCPUs) + runner"
                       + (f" + jumpbox ({jumpbox_vcpus})" if jumpbox else "")))
    _safe_check(VCPU_QUOTA[2], _check_vcpu)

    if need_vpcs:
        def _check_vpc():
            quota = get_quota(sq, *VPC_QUOTA[:2])
            used = 0
            for page in ec2.get_paginator("describe_vpcs").paginate():
                used += len(page.get("Vpcs", []))
            checks.append((VPC_QUOTA[2], need_vpcs, used, quota, "one new VPC for the deployment"))
        _safe_check(VPC_QUOTA[2], _check_vpc)

    def _check_eip():
        quota = get_quota(sq, *EIP_QUOTA[:2])
        # Only count EIPs from the standard "amazon" pool against this quota.
        # EIPs allocated from a customer/Amazon-provided IPAM pool (PublicIpv4Pool
        # is a pool id, not the literal "amazon") draw from a separate IPAM
        # allocation limit, NOT this account/region EIP quota -- confirmed live:
        # ec2:AllocateAddress succeeded for a new standard EIP in an account
        # already showing 7 EIPs against a quota of 5, because only 4 of those
        # 7 were standard-pool (the other 3 were HCX/IPAM-pool addresses).
        # Counting them here previously produced a false "already N OVER quota".
        all_addrs = ec2.describe_addresses()["Addresses"]
        used = sum(1 for a in all_addrs if a.get("PublicIpv4Pool", "amazon") == "amazon")
        if hcx and not hcx_byo_cidr:
            eip_why = "NAT gateway (the 3 HCX public IPs are IPAM-provisioned and " \
                      "draw from a separate IPAM allocation limit, not this quota)"
        elif hcx and hcx_byo_cidr:
            eip_why = "NAT gateway (the 1 HCX public IP is a bring-your-own CIDR, " \
                      "not an EIP allocation, and doesn't draw from this quota)"
        else:
            eip_why = "NAT gateway"
        checks.append((EIP_QUOTA[2], need_eips, used, quota, eip_why))
    _safe_check(EIP_QUOTA[2], _check_eip)

    def _check_sg():
        quota = get_quota(sq, *SG_QUOTA[:2])
        used = 0
        for page in ec2.get_paginator("describe_security_groups").paginate():
            used += len(page.get("SecurityGroups", []))
        checks.append((SG_QUOTA[2], need_sgs, used, quota,
                       "runner SG + EVS service-access SG"
                       + (" + jumpbox SG" if jumpbox else "")))
    _safe_check(SG_QUOTA[2], _check_sg)

    vpc_id = args.existing_vpc_id if args.byo_vpc else None

    # BYO-VPC adds 0 new subnets/route tables, but running the check when
    # --existing-vpc-id is given surfaces whether the existing VPC is ALREADY at
    # its limit. Skip only when there's no VPC ID to scope it to at all.
    if not args.byo_vpc or args.existing_vpc_id:
        def _check_subnets():
            quota = get_quota(sq, *SUBNET_QUOTA[:2])
            used = count_subnets(ec2, vpc_id)
            checks.append((SUBNET_QUOTA[2], need_subnets, used, quota,
                           "bootstrap public + runner subnets"
                           if not args.byo_vpc else
                           "existing VPC headroom check (this deployment adds none)"))
        _safe_check(SUBNET_QUOTA[2], _check_subnets)

        def _check_route_tables():
            quota = get_quota(sq, *RTB_QUOTA[:2])
            used = count_route_tables(ec2, vpc_id)
            checks.append((RTB_QUOTA[2], need_route_tables, used, quota,
                           "bootstrap public + service-access route tables"
                           if not args.byo_vpc else
                           "existing VPC headroom check (this deployment adds none)"))
        _safe_check(RTB_QUOTA[2], _check_route_tables)
    else:
        skipped_checks.append((SUBNET_QUOTA[2],
                                "this deployment adds no new subnets in BYO-VPC mode, and no "
                                "--existing-vpc-id was given to check the existing VPC's "
                                "current headroom"))
        skipped_checks.append((RTB_QUOTA[2],
                                "this deployment adds no new route tables in BYO-VPC mode, and "
                                "no --existing-vpc-id was given to check the existing VPC's "
                                "current headroom"))

    if args.availability_zone:
        def _check_natgw():
            quota = get_quota(sq, *NATGW_QUOTA[:2])
            used = count_nat_gateways(ec2, args.availability_zone)
            checks.append((NATGW_QUOTA[2], need_nat_gateways, used, quota,
                           f"landing-zone NAT gateway in {args.availability_zone}"))
        _safe_check(NATGW_QUOTA[2], _check_natgw)
    else:
        skipped_checks.append((NATGW_QUOTA[2],
                                "--availability-zone was not given (this quota is scoped "
                                "per-AZ, not per-region — it cannot be checked without it)"))

    # --- Route53 / Route53Resolver -------------------------------------------
    def _check_resolver():
        quota = get_quota(sq, *RESOLVER_EP_QUOTA[:2])
        used = count_resolver_endpoints(r53r)
        checks.append((RESOLVER_EP_QUOTA[2], need_resolver_endpoints, used, quota,
                       "inbound resolver endpoint for VLAN DNS"))
    _safe_check(RESOLVER_EP_QUOTA[2], _check_resolver)

    def _check_hosted_zones():
        hz_limit, hz_used = get_hosted_zone_limit_and_usage(r53)
        checks.append(("Route 53 hosted zones per account", need_hosted_zones, hz_used, hz_limit,
                       "forward + reverse lookup zone"))
    _safe_check("Route 53 hosted zones per account", _check_hosted_zones)

    # --- EVS -------------------------------------------------------------
    def _check_evs_env():
        quota = get_quota(sq, *EVS_ENV_QUOTA[:2])
        used = count_evs_environments(evs)
        checks.append((EVS_ENV_QUOTA[2], need_evs_environments, used, quota,
                       "one new EVS environment"))
    _safe_check(EVS_ENV_QUOTA[2], _check_evs_env)

    def _check_evs_hosts():
        quota = get_quota(sq, *EVS_HOST_QUOTA[:2])
        checks.append((EVS_HOST_QUOTA[2], need_evs_hosts_per_env, 0, quota,
                       f"{host_count} host(s) in the new environment "
                       "(per-environment limit, not cumulative with existing environments)"))
    _safe_check(EVS_HOST_QUOTA[2], _check_evs_hosts)

    # --- Secrets Manager ---------------------------------------------------
    def _check_secrets():
        quota = get_quota(sq, *SECRETS_QUOTA[:2])
        used = count_secrets(sm)
        checks.append((SECRETS_QUOTA[2], need_secrets, used, quota,
                       f"{host_count} ESXi root secret(s) + {APPLIANCE_SECRETS_PER_ENV} "
                       "appliance-role secrets"))
    _safe_check(SECRETS_QUOTA[2], _check_secrets)

    # --- Report --------------------------------------------------------------
    print(f"\nQuota preflight for {args.blueprint} in {args.region}")
    print(f"  {host_count} x {instance_type}"
          + (", jumpbox" if jumpbox else "") + (", HCX" if hcx else "") + "\n")
    print(f"{'Check':46} {'Need':>6} {'In use':>7} {'Limit':>7} {'Free':>6}  Result")
    print("-" * 90)

    failures = []
    for name, need, used, quota, why in checks:
        free = quota - used
        ok = free >= need
        mark = f"{GREEN}PASS{RESET}" if ok else f"{RED}FAIL{RESET}"
        # "Free" can't be negative -- being over the quota (used > quota) is a
        # distinct condition from merely having zero headroom, and printing a
        # raw negative number here ("Free: -2") reads as broken math. Clamp
        # the displayed value to 0 and flag over-quota explicitly instead.
        free_display = max(0, free)
        over_note = f" (already {-free:.0f} OVER quota)" if free < 0 else ""
        print(f"{name:46} {need:>6.0f} {used:>7.0f} {quota:>7.0f} {free_display:>6.0f}  {mark}{over_note}")
        if not ok:
            failures.append((name, need, used, quota, free, why))

    print("\nNot checked (no adjustable AWS quota exists for these): EC2 key pairs, "
          "VPC Route Server/endpoints/peers.")
    if (bp.get("aws_config") or {}).get("create_tgw"):
        print("Note: your blueprint sets aws_config.create_tgw, but no Transit Gateway "
              "quota code could be confirmed for this check — verify your Transit Gateway "
              "headroom separately if you rely on this feature.")

    if skipped_checks:
        print(f"\n{YELLOW}{len(skipped_checks)} check(s) could not run and were skipped "
              f"(not counted as pass or fail):{RESET}")
        for name, reason in skipped_checks:
            print(f"  {name} — {reason}")

    if not checks:
        print(f"\n{RED}Every check was skipped — nothing could be verified. This usually "
              f"means bad credentials, the wrong region, or missing permissions.{RESET}")
        return 2
    if not failures and not skipped_checks:
        print(f"\n{GREEN}All checks passed — you have the headroom to launch.{RESET}")
        return 0
    if not failures:
        print(f"\n{YELLOW}No failures among the checks that ran, but {len(skipped_checks)} "
              f"check(s) above were skipped — verify those manually before launching.{RESET}")
        return 1

    quota_codes = {
        VCPU_QUOTA[2]: VCPU_QUOTA, VPC_QUOTA[2]: VPC_QUOTA, EIP_QUOTA[2]: EIP_QUOTA,
        SG_QUOTA[2]: SG_QUOTA, SUBNET_QUOTA[2]: SUBNET_QUOTA, RTB_QUOTA[2]: RTB_QUOTA,
        NATGW_QUOTA[2]: NATGW_QUOTA, RESOLVER_EP_QUOTA[2]: RESOLVER_EP_QUOTA,
        EVS_ENV_QUOTA[2]: EVS_ENV_QUOTA, EVS_HOST_QUOTA[2]: EVS_HOST_QUOTA,
        SECRETS_QUOTA[2]: SECRETS_QUOTA,
    }
    print(f"\n{RED}{len(failures)} check(s) failed.{RESET} For each, you can either free up "
          "capacity or request a quota increase:\n")
    for name, need, used, quota, free, why in failures:
        shortfall = need - free  # unchanged: correct even when free is negative
        if free < 0:
            print(f"  {name} — needs {need:.0f} ({why}); already "
                  f"{-free:.0f} OVER the {quota:.0f} limit ({used:.0f} in use).")
        else:
            print(f"  {name} — needs {need:.0f} ({why}), only {free:.0f} free.")
        if name in quota_codes:
            service, code, _ = quota_codes[name]
            print(f"    Option A: free up at least {shortfall:.0f} by deleting unused resources")
            print("    Option B: request an increase (may take hours-days to approve):")
            print("      aws service-quotas request-service-quota-increase \\")
            print(f"        --service-code {service} --quota-code {code} \\")
            print(f"        --desired-value {used + need:.0f} --region {args.region}\n")
        else:
            print("    This limit (Route 53 hosted zones) has no Service Quotas increase "
                  "path — open an AWS Support case to raise it.\n")
    return 3


if __name__ == "__main__":
    sys.exit(main())
