"""Standalone interactive SDDC spec builder.

Generates a VCF Installer bringup spec (SDDC spec) JSON that an operator
pastes into the installer UI by hand. Pre-populates every value our
automation hardcodes and prompts for the rest, item by item.

The very first question is the target VCF version, because the spec
shape differs between 9.0 and 9.1 (9.0 carries a Fleet Manager spec that
9.1 dropped).

Self-contained: standard library only. Run it directly —

    python sddc_spec_builder.py

or with an explicit output path —

    python sddc_spec_builder.py --out /tmp/my_sddc_spec.json
"""

import argparse
import json
import os
import sys

import builder
import constants as C
import password_rules
import prompts


# Human-friendly labels for each password role prompt.
_ROLE_LABELS = {
    "vcenterRoot": "vCenter root",
    "vcenterSso": "vCenter SSO (administrator@vsphere.local)",
    "nsxRoot": "NSX Manager root",
    "nsxAdmin": "NSX Manager admin",
    "nsxAudit": "NSX Manager audit",
    "sddcManagerRoot": "SDDC Manager root",
    "sddcManagerSsh": "SDDC Manager ssh (vcf user)",
    "sddcManagerLocal": "SDDC Manager local (admin@local)",
    "operationsAdmin": "VCF Operations admin",
    "operationsMaster": "VCF Operations master node root",
    "operationsData": "VCF Operations data node root",
    "operationsReplica": "VCF Operations replica node root",
    "operationsCollector": "VCF Operations Cloud Proxy / Collector root",
    "fleetManagerRoot": "VCF Fleet Manager root",
    "fleetManagerAdmin": "VCF Fleet Manager admin",
}


def _gather_answers():
    answers = {}

    # 1. Version — must be first; gates the rest of the spec shape.
    prompts.section("VCF version")
    print(
        "The bringup spec differs between VCF 9.0 and 9.1 (9.0 includes a\n"
        "Fleet Manager spec that 9.1 dropped)."
    )
    answers["version"] = prompts.prompt_choice(
        "Which version of VCF are you building for?",
        list(C.SUPPORTED_VERSIONS),
        default="9.0",
    )

    # 2. Product / installer identity.
    prompts.section("Installer + environment identity")
    answers["product_version"] = prompts.prompt_str(
        "Exact VCF installer product version (installer UI Settings -> About, "
        "or the OVA file name, e.g. 9.0.2.0)"
    )
    answers["sddc_id"] = prompts.prompt_str(
        "SDDC id", default=C.DEFAULT_SDDC_ID
    )
    answers["fqdn"] = prompts.prompt_str(
        "Management domain / DNS suffix (e.g. my.fqdn.evs)"
    )
    answers["name_prefix"] = prompts.prompt_str(
        "Naming prefix for cluster/datacenter/DVS (e.g. env-abc123 -> "
        "env-abc123-cl01 / -dc01 / -dvs01)"
    )

    # 3. Deployment shape.
    prompts.section("Deployment shape")
    answers["simple_deployment"] = prompts.prompt_bool(
        "Simple deployment? (yes = single NSX Mgr + single VCF Ops node; "
        "no = 3 NSX Mgrs + 3-node VCF Ops HA)",
        default=True,
    )
    instance_type = prompts.prompt_choice(
        "EVS host EC2 instance type (drives the cluster EVC mode)",
        list(C.EVC_MODE_BY_INSTANCE_TYPE.keys()) + ["i4i.metal", "none"],
        default="i4i.metal",
    )
    answers["instance_type"] = "" if instance_type == "none" else instance_type
    # i4i.metal is a valid EVS instance type but takes NO EVC mode on 9.1;
    # only i7i.metal-24xl maps to one. Resolve via .get so a no-EVC type
    # prints "none" instead of raising KeyError.
    evc_mode = (
        C.EVC_MODE_BY_INSTANCE_TYPE.get(answers["instance_type"])
        if answers["instance_type"] else None
    )
    if evc_mode:
        print(f"  -> EVC mode: {evc_mode}")
    else:
        print("  -> EVC mode: none (clusterEvcMode omitted)")

    # 4. DNS + NTP.
    prompts.section("DNS + NTP")
    import ipaddress
    dns_default = None
    while True:
        dns_raw = prompts.prompt_str(
            "DNS server IPs (comma-separated, e.g. 10.0.0.100,10.0.0.101)",
            default=dns_default,
        )
        candidates = [s.strip() for s in dns_raw.split(",") if s.strip()]
        if not candidates:
            print("  Enter at least one DNS server IP.")
            continue
        bad = []
        for c in candidates:
            try:
                ipaddress.ip_address(c)
            except ValueError:
                bad.append(c)
        if bad:
            print(f"  Not valid IP address(es): {', '.join(bad)}")
            continue
        answers["dns_servers"] = candidates
        break
    ntp_raw = prompts.prompt_str(
        "NTP server(s) (comma-separated)",
        default=",".join(C.DEFAULT_NTP_SERVERS),
    )
    answers["ntp_servers"] = [s.strip() for s in ntp_raw.split(",") if s.strip()]

    # 5. Hostnames.
    prompts.section("Appliance short hostnames (defaults shown)")
    print("These get the DNS suffix appended automatically.")
    hostnames = {}
    hostname_prompt_keys = [
        ("vcenter", "vCenter"),
        ("sddc_manager", "SDDC Manager"),
        ("nsx", "NSX VIP"),
        ("nsx01", "NSX Manager 1"),
    ]
    if not answers["simple_deployment"]:
        hostname_prompt_keys += [
            ("nsx02", "NSX Manager 2"),
            ("nsx03", "NSX Manager 3"),
        ]
    hostname_prompt_keys += [
        ("vcf_ops", "VCF Operations VIP / load balancer"),
        ("vcf_ops_01", "VCF Operations node 1 (master)"),
    ]
    if not answers["simple_deployment"]:
        hostname_prompt_keys += [
            ("vcf_ops_02", "VCF Operations node 2 (data)"),
            ("vcf_ops_03", "VCF Operations node 3 (replica)"),
        ]
    hostname_prompt_keys += [("vcf_ops_collector", "VCF Operations Cloud Proxy")]
    if answers["version"] == "9.0":
        hostname_prompt_keys += [("vcf_fleet", "VCF Fleet Manager")]
    else:
        # 9.1 dropped Fleet Manager but requires three new appliances
        # (VSP, License Server, Identity Broker) that the orchestrator
        # also builds for 9.1 - see builder.py's _build_vsp_cluster_spec/
        # _build_license_server_spec/_build_vidb_spec.
        hostname_prompt_keys += [
            ("vsp_instance", "VCF Services Platform (VSP) instance"),
            ("vsp_fleet", "VSP fleet"),
            ("vsp_platform", "VSP platform"),
            ("vcf_license", "VCF License Server"),
            ("vcf_vidb", "VCF Identity Broker (VIDB)"),
        ]

    for key, label in hostname_prompt_keys:
        hostnames[key] = prompts.prompt_str(
            f"  {label} short hostname",
            default=C.DEFAULT_HOSTNAMES.get(key, key),
        )
    answers["hostnames"] = hostnames

    # 6. VLAN CIDRs.
    prompts.section("VLAN CIDRs")
    print(
        "Enter the CIDR for each VLAN. Gateway (.1) and IP-pool ranges\n"
        "(.10 -> broadcast-5) are derived automatically."
    )
    vlan_cidrs = {}
    for pool_key, label in C.VLAN_PROMPT_ORDER:
        vlan_cidrs[pool_key] = prompts.prompt_cidr(f"  {label}")
    answers["vlan_cidrs"] = vlan_cidrs

    # 7. ESXi hosts.
    prompts.section("ESXi hosts")
    host_count = prompts.prompt_int(
        "How many ESXi hosts?", default=len(C.DEFAULT_ESXI_HOSTNAMES), minimum=1
    )
    print(
        "For each host: short hostname, root password, and (optional) SSL\n"
        "thumbprint. skipEsxThumbprintValidation is set true in the spec, so\n"
        "the thumbprint can be left blank if you don't have it handy."
    )
    hosts = []
    for i in range(host_count):
        default_short = (
            C.DEFAULT_ESXI_HOSTNAMES[i]
            if i < len(C.DEFAULT_ESXI_HOSTNAMES)
            else f"esxi{i + 1:02d}"
        )
        short = prompts.prompt_str(f"  Host {i + 1} short hostname", default=default_short)
        password = prompts.prompt_password(f"  {short} root password")
        thumbprint = prompts.prompt_str(
            f"  {short} SSL thumbprint (SHA-256, blank to skip)",
            default="",
            required=False,
        )
        hosts.append(
            {"hostname": short, "password": password, "thumbprint": thumbprint}
        )
    answers["hosts"] = hosts

    # 8. Appliance passwords.
    prompts.section("Appliance passwords")
    roles = builder.required_password_roles(answers)
    print(
        f"{len(roles)} appliance password(s) needed for this deployment.\n"
        "Each is validated against that appliance's complexity rules; the\n"
        "allowed special characters differ by appliance. These are written\n"
        "to the spec in PLAINTEXT (see the warning at the end)."
    )
    use_shared = prompts.prompt_bool(
        "Use one shared password for every appliance role?", default=False
    )

    password_map = {}
    if use_shared:
        # A shared password must satisfy every appliance, so validate it
        # against the cross-appliance intersection special set.
        specials = password_rules.INTERSECTION_SPECIALS
        print(f"  rule (must satisfy all appliances): {password_rules.rule_hint(specials)}")
        shared = prompts.prompt_password(
            "  Shared appliance password",
            validator=lambda pw: password_rules.validate(pw, specials),
        )
        for role in roles:
            password_map[role] = shared
    else:
        for role in roles:
            label = _ROLE_LABELS.get(role, role)
            specials = password_rules.allowed_specials_for_role(role)
            print(f"  [{label}] rule: {password_rules.rule_hint(specials)}")
            password_map[role] = prompts.prompt_password(
                f"  {label} password",
                validator=lambda pw, s=specials: password_rules.validate(pw, s),
            )
    answers["passwords"] = password_map

    return answers


def _print_summary(spec, answers, out_path):
    print()
    print("=" * 60)
    print("SDDC spec generated.")
    print(f"  VCF version:      {answers['version']}")
    print(f"  Product version:  {answers['product_version']}")
    print(f"  Cluster:          {spec['clusterSpec']['clusterName']}")
    print(f"  Datacenter:       {spec['clusterSpec']['datacenterName']}")
    print(f"  EVC mode:         {spec['clusterSpec'].get('clusterEvcMode', '(none)')}")
    print(f"  NSX managers:     {len(spec['nsxtSpec']['nsxtManagers'])}")
    print(f"  VCF Ops nodes:    {len(spec['vcfOperationsSpec']['nodes'])}")
    print(f"  ESXi hosts:       {len(spec['hostSpecs'])}")
    print(f"  Network specs:    {len(spec['networkSpecs'])}")
    fleet = "yes" if "vcfOperationsFleetManagementSpec" in spec else "no"
    print(f"  Fleet Manager:    {fleet}")
    print(f"  Output:           {out_path}")
    print("=" * 60)
    print()
    print("!! WARNING: this file contains appliance passwords in PLAINTEXT.")
    print("   Keep it out of version control, delete it after you've pasted")
    print("   the spec into the installer, and don't share it.")


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Interactive VCF SDDC bringup spec builder (standalone)."
    )
    parser.add_argument(
        "--out",
        default=None,
        help="Output JSON path. Defaults to sddc_spec_<version>.json in the "
             "current directory.",
    )
    args = parser.parse_args(argv)

    print("VCF SDDC spec builder")
    print("---------------------")
    print(
        "Answer each prompt. Values in [brackets] are defaults — press Enter\n"
        "to accept. Ctrl-C to bail out at any time."
    )

    try:
        answers = _gather_answers()
    except (KeyboardInterrupt, EOFError):
        print("\nAborted; nothing written.")
        return 1

    spec = builder.build_spec(answers)

    out_path = args.out or f"sddc_spec_{answers['version']}.json"
    # The spec embeds appliance/ESXi passwords, so create it 0600 (owner-only)
    # rather than relying on the process umask (often world-readable).
    fd = os.open(out_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(spec, f, indent=2)
        f.write("\n")

    _print_summary(spec, answers, out_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
