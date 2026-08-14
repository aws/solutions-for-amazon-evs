"""Assemble the SDDC bringup spec dict from collected operator answers.

Produces the wire-format JSON (camelCase keys) the VCF Installer expects
at ``POST /v1/sddcs``. Version-aware: 9.0 attaches the Fleet Manager
spec, 9.1 omits it. Both attach ``deployWithoutLicenseKeys``.

The ``answers`` dict passed to :func:`build_spec` is produced by
``sddc_spec_builder.py``. Shape:

    {
        "version": "9.0",
        "product_version": "9.0.2.0",
        "sddc_id": "EVS-Management",
        "fqdn": "my.fqdn.evs",
        "name_prefix": "env-abc123",
        "dns_servers": ["10.0.0.100", "10.0.0.101"],
        "ntp_servers": ["time.aws.com"],
        "instance_type": "i4i.metal",       # or "" if none matched
        "simple_deployment": True,
        "hostnames": { "vcenter": "vc", ... },
        "vlan_cidrs": { "vmManagement": "10.0.20.0/24", ... },
        "hosts": [ {"hostname": "esxi01", "password": "...", "thumbprint": "..."} ],
        "passwords": { "vcenterRoot": "...", ... },
    }
"""

import constants as C
import cidr


def _fqdn(short, fqdn):
    return f"{short}.{fqdn}"


def _cluster_name(prefix):
    return f"{prefix}{C.CLUSTER_SUFFIX}"


def _datacenter_name(prefix):
    return f"{prefix}{C.DATACENTER_SUFFIX}"


def _dvs_name(prefix):
    return f"{prefix}{C.DVS_SUFFIX}"


def _build_dns_spec(answers):
    return {
        "subdomain": answers["fqdn"],
        "nameservers": answers["dns_servers"],
    }


def _build_network_specs(answers):
    out = []
    vlan_cidrs = answers["vlan_cidrs"]
    for pool_key, static in C.POOL_STATIC.items():
        network_type = static.get("network_type")
        if network_type is None:
            continue  # vTep handled in the NSX section
        cidr_value = vlan_cidrs.get(pool_key)
        if not cidr_value:
            continue

        if static["teaming_policy"] == "failover_explicit":
            active = ["uplink1"]
            standby = ["uplink2"]
        else:
            active = ["uplink1", "uplink2"]
            standby = None

        spec = {
            "networkType": network_type,
            "portGroupKey": static["port_group_name"],
            "vlanId": static["vlan"],
            "subnet": cidr_value,
            "gateway": cidr.first_usable(cidr_value),
            "mtu": static["mtu"],
            "teamingPolicy": static["teaming_policy"],
            "activeUplinks": active,
        }
        if standby is not None:
            spec["standbyUplinks"] = standby
        if static["uses_range"]:
            spec["includeIpAddressRanges"] = [
                {
                    "startIpAddress": cidr.tenth_host(cidr_value),
                    "endIpAddress": cidr.sixth_from_end(cidr_value),
                }
            ]
        out.append(spec)
    return out


def _build_nsxt_spec(answers):
    hostnames = answers["hostnames"]
    passwords = answers["passwords"]
    vlan_cidrs = answers["vlan_cidrs"]
    tep_cidr = vlan_cidrs.get("vTep")
    tep_static = C.POOL_STATIC["vTep"]
    simple = answers["simple_deployment"]
    prefix = answers["name_prefix"]

    if simple:
        nsx_shorts = [hostnames.get("nsx01", "nsx01")]
    else:
        nsx_shorts = [
            hostnames.get("nsx01", "nsx01"),
            hostnames.get("nsx02", "nsx02"),
            hostnames.get("nsx03", "nsx03"),
        ]

    spec = {
        "nsxtManagerSize": C.DEFAULT_NSX_MANAGER_SIZE,
        "vipFqdn": _fqdn(hostnames.get("nsx", "nsx"), answers["fqdn"]),
        "rootNsxtManagerPassword": passwords["nsxRoot"],
        "nsxtAdminPassword": passwords["nsxAdmin"],
        "nsxtAuditPassword": passwords["nsxAudit"],
        "nsxtManagers": [
            {"hostname": _fqdn(short, answers["fqdn"])} for short in nsx_shorts
        ],
        "transportVlanId": tep_static["vlan"],
    }

    if tep_cidr:
        spec["ipAddressPoolSpec"] = {
            "name": f"{_cluster_name(prefix)}_ip_pool",
            "description": (
                f"Management Domain ({_cluster_name(prefix)}) - Host TEP IP Pool"
            ),
            "ignoreUnavailableNsxtCluster": True,
            "subnets": [
                {
                    "cidr": tep_cidr,
                    "gateway": cidr.first_usable(tep_cidr),
                    "ipAddressPoolRanges": [
                        {
                            "start": cidr.tenth_host(tep_cidr),
                            "end": cidr.sixth_from_end(tep_cidr),
                        }
                    ],
                }
            ],
        }
    return spec


def _build_vcenter_spec(answers):
    hostnames = answers["hostnames"]
    passwords = answers["passwords"]
    return {
        "vcenterHostname": _fqdn(hostnames.get("vcenter", "vc"), answers["fqdn"]),
        "vmSize": C.DEFAULT_VCENTER_VM_SIZE,
        "storageSize": C.DEFAULT_VCENTER_STORAGE_SIZE,
        "rootVcenterPassword": passwords["vcenterRoot"],
        "adminUserSsoUsername": C.VCENTER_SSO_USERNAME,
        "adminUserSsoPassword": passwords["vcenterSso"],
        "ssoDomain": C.VCENTER_SSO_DOMAIN,
    }


def _build_sddc_manager_spec(answers):
    hostnames = answers["hostnames"]
    passwords = answers["passwords"]
    return {
        "hostname": _fqdn(hostnames.get("sddc_manager", "sddcm"), answers["fqdn"]),
        "rootPassword": passwords["sddcManagerRoot"],
        "sshPassword": passwords["sddcManagerSsh"],
        "localUserPassword": passwords["sddcManagerLocal"],
        "useExistingDeployment": True,
        "version": answers["product_version"],
    }


def _build_cluster_spec(answers):
    instance_type = answers.get("instance_type") or ""
    evc_mode = C.EVC_MODE_BY_INSTANCE_TYPE.get(instance_type)
    prefix = answers["name_prefix"]
    spec = {
        "datacenterName": _datacenter_name(prefix),
        "clusterName": _cluster_name(prefix),
        "resourcePoolSpecs": [
            {"name": C.RESOURCE_POOL_NAME, "type": C.RESOURCE_POOL_TYPE}
        ],
    }
    # clusterEvcMode is only emitted when we have a match — a null/empty
    # value is how the installer reads "EVC off".
    if evc_mode:
        spec["clusterEvcMode"] = evc_mode
    return spec


def _build_datastore_spec(_answers):
    return {
        "vsanSpec": {
            "datastoreName": C.VSAN_DATASTORE_NAME,
            "vsanDedup": C.VSAN_DEDUP,
            "esaConfig": {"enabled": C.VSAN_ESA_ENABLED},
            "failuresToTolerate": C.VSAN_FAILURES_TO_TOLERATE,
        }
    }


def _build_dvs_specs(answers):
    return [
        {
            "dvsName": _dvs_name(answers["name_prefix"]),
            "networks": list(C.DVS_NETWORKS),
            "mtu": C.DVS_MTU,
            "nsxtSwitchConfig": {
                "hostSwitchOperationalMode": C.DVS_HOST_SWITCH_OPERATIONAL_MODE,
                "ipAssignmentType": C.DVS_IP_ASSIGNMENT_TYPE,
                "transportZones": [
                    {"name": C.OVERLAY_TZ_NAME, "transportType": "OVERLAY"}
                ],
            },
            "vmnicsToUplinks": [dict(v) for v in C.DVS_VMNIC_UPLINKS],
            "nsxTeamings": [
                {
                    "policy": C.DVS_NSX_TEAMING_POLICY,
                    "activeUplinks": list(C.DVS_NSX_TEAMING_ACTIVE_UPLINKS),
                    "standByUplinks": list(C.DVS_NSX_TEAMING_STANDBY_UPLINKS),
                }
            ],
        }
    ]


def _build_host_specs(answers):
    out = []
    for host in answers["hosts"]:
        spec = {
            "hostname": _fqdn(host["hostname"], answers["fqdn"]),
            "credentials": {
                "username": C.ESXI_USERNAME,
                "password": host["password"],
            },
        }
        if host.get("thumbprint"):
            spec["sslThumbprint"] = host["thumbprint"]
        out.append(spec)
    return out


def _build_vcf_operations_spec(answers):
    hostnames = answers["hostnames"]
    passwords = answers["passwords"]
    simple = answers["simple_deployment"]

    if simple:
        nodes = [
            {
                "hostname": _fqdn(hostnames.get("vcf_ops_01", "vcfops01"), answers["fqdn"]),
                "rootUserPassword": passwords["operationsMaster"],
                "type": "master",
            }
        ]
    else:
        nodes = [
            {
                "hostname": _fqdn(hostnames.get("vcf_ops_01", "vcfops01"), answers["fqdn"]),
                "rootUserPassword": passwords["operationsMaster"],
                "type": "master",
            },
            {
                "hostname": _fqdn(hostnames.get("vcf_ops_02", "vcfops02"), answers["fqdn"]),
                "rootUserPassword": passwords["operationsData"],
                "type": "data",
            },
            {
                "hostname": _fqdn(hostnames.get("vcf_ops_03", "vcfops03"), answers["fqdn"]),
                "rootUserPassword": passwords["operationsReplica"],
                "type": "replica",
            },
        ]

    return {
        "adminUserPassword": passwords["operationsAdmin"],
        "loadBalancerFqdn": _fqdn(hostnames.get("vcf_ops", "vcfops"), answers["fqdn"]),
        "applianceSize": C.DEFAULT_OPERATIONS_APPLIANCE_SIZE,
        "nodes": nodes,
    }


def _build_vcf_operations_collector_spec(answers):
    hostnames = answers["hostnames"]
    passwords = answers["passwords"]
    return {
        "hostname": _fqdn(hostnames.get("vcf_ops_collector", "vcfopscol"), answers["fqdn"]),
        "rootUserPassword": passwords["operationsCollector"],
        "applianceSize": C.DEFAULT_OPERATIONS_COLLECTOR_APPLIANCE_SIZE,
    }


def _build_fleet_management_spec(answers):
    """9.0 only. Dropped on 9.1."""
    hostnames = answers["hostnames"]
    passwords = answers["passwords"]
    return {
        "hostname": _fqdn(hostnames.get("vcf_fleet", "vcffleet"), answers["fqdn"]),
        "rootUserPassword": passwords["fleetManagerRoot"],
        "adminUserPassword": passwords["fleetManagerAdmin"],
    }


def build_spec(answers):
    """Assemble the full SDDC spec dict from collected answers."""
    prefix = answers["name_prefix"]
    spec = {
        "sddcId": answers["sddc_id"],
        "workflowType": C.WORKFLOW_TYPE,
        "version": answers["product_version"],
        "ceipEnabled": C.CEIP_ENABLED,
        "skipEsxThumbprintValidation": C.SKIP_ESX_THUMBPRINT_VALIDATION,
        # skipGatewayPingValidation and skipVsanEsaCertifiedDiskValidation
        # mirror the orchestrator's SddcSpec construction exactly
        # (orchestrator/evs_environment/sddc_spec_builder.py). With
        # VSAN_ESA_ENABLED=True (below, unconditionally) on EVS's NVMe
        # disks — which aren't on VMware's vSAN ESA certified list — the
        # ESA-disk validation failure is essentially guaranteed on a
        # manual bringup without the skip flag set.
        "skipGatewayPingValidation": True,
        "skipVsanEsaCertifiedDiskValidation": True,
        "managementPoolName": f"{_cluster_name(prefix)}_pool",
        "ntpServers": answers["ntp_servers"],
        "dnsSpec": _build_dns_spec(answers),
        "networkSpecs": _build_network_specs(answers),
        "nsxtSpec": _build_nsxt_spec(answers),
        "vcenterSpec": _build_vcenter_spec(answers),
        "sddcManagerSpec": _build_sddc_manager_spec(answers),
        "clusterSpec": _build_cluster_spec(answers),
        "datastoreSpec": _build_datastore_spec(answers),
        "dvsSpecs": _build_dvs_specs(answers),
        "hostSpecs": _build_host_specs(answers),
        "vcfOperationsSpec": _build_vcf_operations_spec(answers),
        "vcfOperationsCollectorSpec": _build_vcf_operations_collector_spec(answers),
        # Not modeled as a typed SDK field but required on the wire for
        # both 9.0 and 9.1.
        "deployWithoutLicenseKeys": C.DEPLOY_WITHOUT_LICENSE_KEYS,
    }

    # 9.0 requires the Fleet Manager spec; 9.1 dropped it but requires
    # three NEW sections instead (VCF Services Platform, License Server,
    # Identity Broker) — confirmed required by the orchestrator's own
    # _build_91_typed_kwargs and its module docstring ("vspClusterSpec,
    # licenseServerSpec, vidbSpec are required on 9.1"). Without these, a
    # 9.1 spec produced by this tool is structurally incomplete and the
    # installer will reject the POST.
    if answers["version"] == "9.0":
        spec["vcfOperationsFleetManagementSpec"] = _build_fleet_management_spec(answers)
    else:
        spec["vspClusterSpec"] = _build_vsp_cluster_spec(answers)
        spec["licenseServerSpec"] = _build_license_server_spec(answers)
        spec["vidbSpec"] = _build_vidb_spec(answers)

    return spec


def _build_vsp_cluster_spec(answers):
    """Build the VCF 9.1 VSP (VCF Services Platform) cluster spec.

    Mirrors orchestrator/evs_environment/sddc_spec_builder.py's
    _build_vsp_cluster_spec: derives a small IPv4 pool from the
    vmManagement VLAN CIDR (offsets +80..+100, matching the
    orchestrator's derivation exactly), and reuses the sddcManagerRoot
    password placeholder for systemUserPassword (VSP's password
    complexity allow-list is a subset of SDDC Manager root's, confirmed
    against password_rules.py's INTERSECTION_SPECIALS).
    """
    import ipaddress

    hostnames = answers["hostnames"]
    vlan_cidrs = answers["vlan_cidrs"]
    vm_mgmt_cidr = vlan_cidrs.get("vmManagement", "10.0.60.0/24")
    net = ipaddress.ip_network(vm_mgmt_cidr, strict=False)
    pool_start = str(net.network_address + 80)
    pool_end = str(net.network_address + 100)

    return {
        "instanceFqdn": _fqdn(hostnames.get("vsp_instance", "vsp-instance"), answers["fqdn"]),
        "fleetFqdn": _fqdn(hostnames.get("vsp_fleet", "vsp-fleet"), answers["fqdn"]),
        "platformFqdn": _fqdn(hostnames.get("vsp_platform", "vsp-platform"), answers["fqdn"]),
        "systemUserPassword": "__SECRET:sddcManagerRoot__",
        "ipv4Pool": {
            "ipRange": {
                "startIpAddress": pool_start,
                "endIpAddress": pool_end,
            },
        },
        "size": "small",
    }


def _build_license_server_spec(answers):
    """Build the VCF 9.1 License Server spec."""
    hostnames = answers["hostnames"]
    return {
        "hostname": _fqdn(hostnames.get("vcf_license", "vcf-license"), answers["fqdn"]),
    }


def _build_vidb_spec(answers):
    """Build the VCF 9.1 Virtual Identity Broker spec."""
    hostnames = answers["hostnames"]
    return {
        "hostname": _fqdn(hostnames.get("vcf_vidb", "vcf-vidb"), answers["fqdn"]),
        "size": "small",
    }


def required_password_roles(answers):
    """The password roles that need collecting for this deployment shape.

    - 11 baseline (edge appliance is NOT here — edges are a separate
      Phase 3 deploy, not part of the SDDC bringup spec)
    - +2 if HA (operations data + replica)
    - +2 if 9.0 (fleet manager root + admin)
    - 9.1's VSP systemUserPassword reuses sddcManagerRoot's placeholder
      (see _build_vsp_cluster_spec) — no separate role/secret needed,
      matching the orchestrator's own design. License Server and VIDB
      have no password field on the wire at all.
    """
    roles = [
        "vcenterRoot",
        "vcenterSso",
        "nsxRoot",
        "nsxAdmin",
        "nsxAudit",
        "sddcManagerRoot",
        "sddcManagerSsh",
        "sddcManagerLocal",
        "operationsAdmin",
        "operationsMaster",
        "operationsCollector",
    ]
    if not answers["simple_deployment"]:
        roles.extend(["operationsData", "operationsReplica"])
    if answers["version"] == "9.0":
        roles.extend(["fleetManagerRoot", "fleetManagerAdmin"])
    return roles
