"""Hardcoded values for the standalone SDDC spec builder.

Everything in this module is a value our automation pins rather than
derives from the environment. The interactive CLI
(``sddc_spec_builder.py``) surfaces
each of these as a default the operator can accept with Enter, and
prompts for the site-specific pieces (FQDN, CIDRs, product version,
hostnames, passwords) that aren't knowable up front.

Source of truth: these mirror
``orchestrator/evs_environment/sddc_spec_builder.py`` so a spec produced
here matches what the full automation would POST.
"""

SUPPORTED_VERSIONS = ("9.0", "9.1")

# Top-level spec defaults.
DEFAULT_SDDC_ID = "EVS-Management"
WORKFLOW_TYPE = "VCF"
CEIP_ENABLED = False
SKIP_ESX_THUMBPRINT_VALIDATION = True
DEPLOY_WITHOUT_LICENSE_KEYS = True
DEFAULT_NTP_SERVERS = ["time.aws.com"]

# vCenter.
VCENTER_SSO_USERNAME = "administrator@vsphere.local"
VCENTER_SSO_DOMAIN = "vsphere.local"
DEFAULT_VCENTER_VM_SIZE = "medium"
DEFAULT_VCENTER_STORAGE_SIZE = "lstorage"

# NSX.
DEFAULT_NSX_MANAGER_SIZE = "medium"

# VCF Operations sizing.
DEFAULT_OPERATIONS_APPLIANCE_SIZE = "medium"
DEFAULT_OPERATIONS_COLLECTOR_APPLIANCE_SIZE = "standard"

# vSAN datastore.
VSAN_DATASTORE_NAME = "vsan-datastore"
VSAN_DEDUP = False
VSAN_ESA_ENABLED = True
VSAN_FAILURES_TO_TOLERATE = 1

# DVS.
DVS_MTU = 8500
DVS_NETWORKS = ["MANAGEMENT", "VSAN", "VMOTION", "VM_MANAGEMENT"]
# NOTE: the orchestrator's current _build_dvs_specs does NOT set
# host_switch_operational_mode/ip_assignment_type on NsxtSwitchConfig at
# Matches the orchestrator's
# sddc_spec_builder.py directly) - these two constants are unused
# leftovers from an earlier drift and are not read by builder.py's spec
# construction. Kept only as historical reference; do not wire them in
# without first confirming the orchestrator has started setting them.
DVS_HOST_SWITCH_OPERATIONAL_MODE = "STANDARD"
DVS_IP_ASSIGNMENT_TYPE = "STATIC"
# vmnic-to-uplink mapping is CROSSED (vmnic0->uplink2, vmnic1->uplink1),
# matching the orchestrator's current _build_dvs_specs exactly - this is
# EVS's expected NIC ordering, not an arbitrary choice; getting this
# backwards silently misconfigures host networking.
DVS_VMNIC_UPLINKS = [
    {"id": "vmnic0", "uplink": "uplink2"},
    {"id": "vmnic1", "uplink": "uplink1"},
]
DVS_NSX_TEAMING_POLICY = "FAILOVER_ORDER"
DVS_NSX_TEAMING_ACTIVE_UPLINKS = ["uplink1"]
DVS_NSX_TEAMING_STANDBY_UPLINKS = ["uplink2"]

# Overlay transport zone (created during bringup; referenced by the DVS).
OVERLAY_TZ_NAME = "tz-overlay-01"

# Resource pool created on the management cluster.
RESOURCE_POOL_NAME = "Management Appliances"
RESOURCE_POOL_TYPE = "management"

# EC2 instance type -> vSphere EVC mode.
# NOTE: i4i.metal is deliberately NOT in this map, mirroring the
# orchestrator's own _EVC_MODE_BY_INSTANCE_TYPE exactly (see
# orchestrator/evs_environment/sddc_spec_builder.py for the full
# explanation): i4i never needed an explicit EVC mode, and setting it
# breaks VCF 9.1 bringup with EVCAdmissionFailedVmActive (ESXi 9.1 VMs
# boot claiming CPU features, incl. AVX-512, that exceed the
# intel-icelake baseline). i4i.metal was previously the interactive
# CLI's DEFAULT instance type (sddc_spec_builder.py prompt) - a spec
# generated with defaults on a 9.1 build would reproduce this exact
# failure.
EVC_MODE_BY_INSTANCE_TYPE = {
    "i7i.metal-24xl": "INTEL_SAPPHIRERAPIDS",
}

# ESXi host credentials.
ESXI_USERNAME = "root"

# Per-pool static network config. VLAN IDs are fixed by AWS Elastic
# VMware Service; the CIDRs get prompted (operator enters the per-pool
# CIDR, we derive gateway + ranges from it).
#
#   network_type  -> wire NetworkType enum (None => not a bringup network
#                    spec; vTep is handled inside the NSX section)
#   uses_range    -> whether to emit includeIpAddressRanges
#   teaming       -> "failover_explicit" (active/standby) or
#                    "loadbalance_loadbased" (active/active)
#
# MTU/teaming values match the orchestrator's current _POOL_STATIC
# Matches the orchestrator's
# sddc_spec_builder.py directly) - vmManagement/vmkManagement are 1500
# MTU (NOT 8500 - only the DVS-level MTU and the vMotion/vSAN pools use
# 8500), and vMotion/vSan both use failover_explicit (active/standby),
# NOT loadbalance_loadbased (active/active). A prior version of this
# file had all four values backwards.
POOL_STATIC = {
    "vmManagement": {
        "network_type": "VM_MANAGEMENT",
        "port_group_name": "pg-vm-mgmt",
        "vlan": 20,
        "mtu": 1500,
        "teaming_policy": "failover_explicit",
        "uses_range": False,
    },
    "vmkManagement": {
        "network_type": "MANAGEMENT",
        "port_group_name": "pg-host-mgmt",
        "vlan": 0,
        "mtu": 1500,
        "teaming_policy": "failover_explicit",
        "uses_range": False,
    },
    "vMotion": {
        "network_type": "VMOTION",
        "port_group_name": "pg-vmotion",
        "vlan": 30,
        "mtu": 8500,
        "teaming_policy": "failover_explicit",
        "uses_range": True,
    },
    "vSan": {
        "network_type": "VSAN",
        "port_group_name": "pg-vsan",
        "vlan": 40,
        "mtu": 8500,
        "teaming_policy": "failover_explicit",
        "uses_range": True,
    },
    "vTep": {
        "port_group_name": "pg-host-tep-01",
        "vlan": 50,
        "mtu": 8500,
        "uses_range": True,
        "tz_overlay_name": OVERLAY_TZ_NAME,
    },
}

# The order VLAN CIDRs get prompted in, and a human label for each.
VLAN_PROMPT_ORDER = [
    ("vmManagement", "VM management (vCenter, NSX Mgr, SDDC Mgr live here) - VLAN 20"),
    ("vmkManagement", "ESXi host vmkernel management - untagged"),
    ("vMotion", "vMotion - VLAN 30"),
    ("vSan", "vSAN - VLAN 40"),
    ("vTep", "Host TEP / overlay - VLAN 50"),
]

# Default short hostnames (operator can override each).
DEFAULT_HOSTNAMES = {
    "vcenter": "vc",
    "sddc_manager": "sddcm",
    "nsx": "nsx",
    "nsx01": "nsx01",
    "nsx02": "nsx02",
    "nsx03": "nsx03",
    "edge01": "edge01",
    "edge02": "edge02",
    "vcf_ops": "vcfops",
    "vcf_ops_01": "vcfops01",
    "vcf_ops_02": "vcfops02",
    "vcf_ops_03": "vcfops03",
    "vcf_ops_collector": "vcfopscol",
    "vcf_fleet": "vcffleet",
    # VCF 9.1-only appliances (9.0 has no equivalents - see the version
    # branch in sddc_spec_builder.py's hostname prompts).
    "vsp_instance": "vsp-instance",
    "vsp_fleet": "vsp-fleet",
    "vsp_platform": "vsp-platform",
    "vcf_license": "vcf-license",
    "vcf_vidb": "vcf-vidb",
}

# Default ESXi short hostnames.
DEFAULT_ESXI_HOSTNAMES = ["esxi01", "esxi02", "esxi03"]

# Naming suffixes derived from the environment id / name prefix.
CLUSTER_SUFFIX = "-cl01"
DATACENTER_SUFFIX = "-dc01"
DVS_SUFFIX = "-dvs01"
