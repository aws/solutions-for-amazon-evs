# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Build a typed VCF Installer SddcSpec object from the Phase 2 config.

Constructs ``vmware.vcf_installer.model_client.SddcSpec`` (and its nested
classes) from the Phase 2 config dict. The resulting object can be:

  - passed directly to ``Sddcs.deploy_sddc(sddc_spec)``, or
  - serialized to wire JSON via the SDK's own serializer and written to
    disk so it can be inspected by hand (that's what ``phase3_sync.py``
    does).

Version handling
----------------

The 9.1 SDK typed model drops two fields we need on VCF 9.0:

  - ``deployWithoutLicenseKeys`` — required on both 9.0 and 9.1, not modeled
    as a typed field on either SDK version.
  - ``vcfOperationsFleetManagementSpec`` — required on 9.0, dropped on 9.1.

Both are set through ``VapiStruct._set_extra_fields({...})`` which adds the
field to the wire JSON verbatim. See ``_version_extras`` for the version
branch.
"""

import ipaddress
import logging
from typing import Any

from vmware.vcf_installer import model_client as m

logger = logging.getLogger(__name__)

_PLACEHOLDER = "PENDING_POST_EVS_SYNC"


class SddcSpecBuilder:
    """Builds a typed ``SddcSpec`` object from the Phase 2 config.

    Args:
        config: Phase 2 config dict (as loaded from config.json).
        environment_id: Optional EVS environment ID. When None, env-derived
            names (cluster / datacenter / DVS) fall back to a placeholder.
    """

    # Per-pool static config (VLAN, MTU, port group).
    _POOL_STATIC: dict[str, dict[str, Any]] = {
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
            "tz_overlay_name": "tz-overlay-01",
        },
    }

    # Placeholder values written into the bringup spec. Phase 3's
    # ``secret_resolver`` swaps these for real password strings fetched
    # from AWS Secrets Manager just before POSTing to the installer.
    # Format: ``__SECRET:<roleName>__`` where the role name maps to the
    # Phase 2 ``vcf_password_provisioner`` role list.
    _PLACEHOLDER_VCENTER_ROOT = "__SECRET:vcenterRoot__"
    _PLACEHOLDER_VCENTER_SSO = "__SECRET:vcenterSso__"
    _PLACEHOLDER_NSX_ROOT = "__SECRET:nsxRoot__"
    _PLACEHOLDER_NSX_ADMIN = "__SECRET:nsxAdmin__"
    _PLACEHOLDER_NSX_AUDIT = "__SECRET:nsxAudit__"
    _PLACEHOLDER_SDDC_MANAGER_ROOT = "__SECRET:sddcManagerRoot__"
    _PLACEHOLDER_SDDC_MANAGER_SSH = "__SECRET:sddcManagerSsh__"
    _PLACEHOLDER_SDDC_MANAGER_LOCAL = "__SECRET:sddcManagerLocal__"
    _PLACEHOLDER_OPERATIONS_ADMIN = "__SECRET:operationsAdmin__"
    _PLACEHOLDER_OPERATIONS_MASTER = "__SECRET:operationsMaster__"
    _PLACEHOLDER_OPERATIONS_DATA = "__SECRET:operationsData__"
    _PLACEHOLDER_OPERATIONS_REPLICA = "__SECRET:operationsReplica__"
    _PLACEHOLDER_OPERATIONS_COLLECTOR = "__SECRET:operationsCollector__"

    # EC2 instance type -> vSphere EVC mode.
    _EVC_MODE_BY_INSTANCE_TYPE = {
        "i4i.metal":       "INTEL_ICELAKE",
        "i7i.metal-24xl":  "INTEL_SAPPHIRERAPIDS",
    }

    _SUPPORTED_VERSION_PREFIXES = ("9.0", "9.1")

    def __init__(
        self,
        config: dict[str, Any],
        environment_id: str | None = None,
    ) -> None:
        self._config = config
        self._environment_id = environment_id

        installer_version = str(config.get("vcfInstallerProductVersion") or "")
        parts = installer_version.split(".")
        derived = f"{parts[0]}.{parts[1]}" if len(parts) >= 2 else installer_version

        matched = next(
            (p for p in self._SUPPORTED_VERSION_PREFIXES if derived.startswith(p)),
            None,
        )
        if not matched:
            raise ValueError(
                f"Cannot derive a supported VCF version from "
                f"vcfInstallerProductVersion {installer_version!r}. "
                f"Must start with one of: {self._SUPPORTED_VERSION_PREFIXES}"
            )
        self._target_version = matched

    # ---- CIDR helpers ----

    @staticmethod
    def _first_usable(cidr: str) -> str:
        net = ipaddress.ip_network(cidr, strict=False)
        return str(net.network_address + 1)

    @staticmethod
    def _tenth_host(cidr: str) -> str:
        net = ipaddress.ip_network(cidr, strict=False)
        return str(net.network_address + 10)

    @staticmethod
    def _sixth_from_end(cidr: str) -> str:
        net = ipaddress.ip_network(cidr, strict=False)
        return str(net.broadcast_address - 5)

    # ---- Hostname / name helpers ----

    def _fqdn(self, short: str) -> str:
        return f"{short}.{self._config['fqdn']}"

    def _cluster_name(self) -> str:
        return f"{self._environment_id}-cl01" if self._environment_id else _PLACEHOLDER

    def _datacenter_name(self) -> str:
        return f"{self._environment_id}-dc01" if self._environment_id else _PLACEHOLDER

    def _dvs_name(self) -> str:
        return f"{self._environment_id}-dvs01" if self._environment_id else _PLACEHOLDER

    # ---- Section builders ----

    def _build_network_specs(self) -> list[m.SddcNetworkSpec]:
        vlans = self._config.get("initialVlans", {})
        out: list[m.SddcNetworkSpec] = []

        for pool_key, static in self._POOL_STATIC.items():
            if "network_type" not in static:
                continue  # vTep handled in the NSX section, not here
            if pool_key not in vlans:
                logger.warning("initialVlans.%s missing; skipping network", pool_key)
                continue

            cidr = vlans[pool_key]["cidr"]
            if static["teaming_policy"] == "failover_explicit":
                active = ["uplink1"]
                standby = ["uplink2"]
            else:
                active = ["uplink1", "uplink2"]
                standby = None

            include_ranges = None
            if static["uses_range"]:
                include_ranges = [
                    m.IpRange(
                        start_ip_address=self._tenth_host(cidr),
                        end_ip_address=self._sixth_from_end(cidr),
                    )
                ]

            out.append(
                m.SddcNetworkSpec(
                    network_type=static["network_type"],
                    port_group_key=static["port_group_name"],
                    vlan_id=static["vlan"],
                    subnet=cidr,
                    gateway=self._first_usable(cidr),
                    mtu=static["mtu"],
                    teaming_policy=static["teaming_policy"],
                    active_uplinks=active,
                    standby_uplinks=standby,
                    include_ip_address_ranges=include_ranges,
                )
            )
        return out

    def _build_nsxt_spec(self) -> m.SddcNsxtSpec:
        vlans = self._config.get("initialVlans", {})
        tep_cidr = vlans.get("vTep", {}).get("cidr")
        tep_static = self._POOL_STATIC["vTep"]
        hostnames = self._config.get("vcfHostnames", {})
        sizing = self._config.get("vcfSizing", {})
        simple = self._config.get("simpleDeployment", True)

        nsxt_short_names = (
            [hostnames.get("nsx01", "nsx01")]
            if simple
            else [
                hostnames.get("nsx01", "nsx01"),
                hostnames.get("nsx02", "nsx02"),
                hostnames.get("nsx03", "nsx03"),
            ]
        )

        managers = [
            m.NsxtManagerSpec(hostname=self._fqdn(short))
            for short in nsxt_short_names
        ]

        ip_pool: m.IpAddressPoolSpec | None = None
        if tep_cidr:
            ip_pool = m.IpAddressPoolSpec(
                name=f"{self._cluster_name()}_ip_pool",
                description=(
                    f"Management Domain ({self._cluster_name()}) - Host TEP IP Pool"
                ),
                ignore_unavailable_nsxt_cluster=True,
                subnets=[
                    m.IpAddressPoolSubnetSpec(
                        cidr=tep_cidr,
                        gateway=self._first_usable(tep_cidr),
                        ip_address_pool_ranges=[
                            m.IpAddressPoolRangeSpec(
                                start=self._tenth_host(tep_cidr),
                                end=self._sixth_from_end(tep_cidr),
                            )
                        ],
                    )
                ],
            )

        return m.SddcNsxtSpec(
            nsxt_manager_size=sizing.get("nsxSize", "medium"),
            vip_fqdn=self._fqdn(hostnames.get("nsx", "nsx")),
            root_nsxt_manager_password=self._PLACEHOLDER_NSX_ROOT,
            nsxt_admin_password=self._PLACEHOLDER_NSX_ADMIN,
            nsxt_audit_password=self._PLACEHOLDER_NSX_AUDIT,
            nsxt_managers=managers,
            transport_vlan_id=tep_static["vlan"],
            ip_address_pool_spec=ip_pool,
            skip_nsx_overlay_over_management_network=True,
        )

    def _build_vcf_operations_spec(self) -> m.VcfOperationsSpec:
        hostnames = self._config.get("vcfHostnames", {})
        sizing = self._config.get("vcfSizing", {})
        simple = self._config.get("simpleDeployment", True)

        if simple:
            nodes = [
                m.VcfOperationsNode(
                    hostname=self._fqdn(hostnames.get("vcf_ops_01", "vcfops01")),
                    root_user_password=self._PLACEHOLDER_OPERATIONS_MASTER,
                    type="master",
                ),
            ]
        else:
            nodes = [
                m.VcfOperationsNode(
                    hostname=self._fqdn(hostnames.get("vcf_ops_01", "vcfops01")),
                    root_user_password=self._PLACEHOLDER_OPERATIONS_MASTER,
                    type="master",
                ),
                m.VcfOperationsNode(
                    hostname=self._fqdn(hostnames.get("vcf_ops_02", "vcfops02")),
                    root_user_password=self._PLACEHOLDER_OPERATIONS_DATA,
                    type="data",
                ),
                m.VcfOperationsNode(
                    hostname=self._fqdn(hostnames.get("vcf_ops_03", "vcfops03")),
                    root_user_password=self._PLACEHOLDER_OPERATIONS_REPLICA,
                    type="replica",
                ),
            ]

        return m.VcfOperationsSpec(
            admin_user_password=self._PLACEHOLDER_OPERATIONS_ADMIN,
            load_balancer_fqdn=self._fqdn(hostnames.get("vcf_ops", "vcfops")),
            appliance_size=sizing.get("operationsApplianceSize", "medium"),
            nodes=nodes,
        )

    def _build_vcf_operations_collector_spec(self) -> m.VcfOperationsCollectorSpec:
        hostnames = self._config.get("vcfHostnames", {})
        sizing = self._config.get("vcfSizing", {})
        return m.VcfOperationsCollectorSpec(
            hostname=self._fqdn(hostnames.get("vcf_ops_collector", "vcfopscol")),
            root_user_password=self._PLACEHOLDER_OPERATIONS_COLLECTOR,
            appliance_size=sizing.get("operationsCollectorApplianceSize", "standard"),
        )

    def _build_host_specs(self) -> list[m.SddcHostSpec]:
        esxi_hostnames = self._config.get("esxiHostnames", [])
        esxi_thumbprints = self._config.get("esxiSslThumbprints", {})

        # ESXi root passwords flow through the same placeholder path as
        # the appliance passwords. Phase 3's secret_resolver swaps each
        # ``__SECRET:esxi:<host>__`` for the live value pulled from the
        # privileged ``evs!<env>_<host>`` secret (created by the EVS
        # service when the host was provisioned). Keeps real
        # passwords out of bringup_spec.json on disk.
        hosts: list[m.SddcHostSpec] = []
        for short in esxi_hostnames:
            hosts.append(
                m.SddcHostSpec(
                    hostname=self._fqdn(short),
                    credentials=m.SddcCredentials(
                        username="root",
                        password=f"__SECRET:esxi:{short}__",
                    ),
                    # SSL thumbprint is required by the installer's spec
                    # validator even though skipEsxThumbprintValidation is
                    # set on the top-level spec. Leaves null here (ESXi hosts
                    # aren't DNS-resolvable from Phase 2's workstation);
                    # Phase 3's BringupManager fills it in at POST time by
                    # hitting each host directly from the Phase 3 runtime.
                    ssl_thumbprint=esxi_thumbprints.get(short),
                )
            )
        return hosts

    def _build_dvs_specs(self) -> list[m.DvsSpec]:
        switch_config = m.NsxtSwitchConfig(
            transport_zones=[
                m.TransportZone(
                    name=self._POOL_STATIC["vTep"]["tz_overlay_name"],
                    transport_type="OVERLAY",
                )
            ],
        )

        return [
            m.DvsSpec(
                dvs_name=self._dvs_name(),
                networks=["MANAGEMENT", "VSAN", "VMOTION", "VM_MANAGEMENT"],
                mtu=8500,
                nsxt_switch_config=switch_config,
                vmnics_to_uplinks=[
                    m.VmnicToUplink(id="vmnic0", uplink="uplink2"),
                    m.VmnicToUplink(id="vmnic1", uplink="uplink1"),
                ],
                nsx_teamings=[
                    m.TeamingSpec(
                        policy="FAILOVER_ORDER",
                        active_uplinks=["uplink1"],
                        stand_by_uplinks=["uplink2"],
                    )
                ],
            )
        ]

    def _build_cluster_spec(self) -> m.SddcClusterSpec:
        return m.SddcClusterSpec(
            datacenter_name=self._datacenter_name(),
            cluster_name=self._cluster_name(),
            resource_pool_specs=[
                m.ResourcePoolSpec(name="Management Appliances", type="management"),
            ],
        )

    def _build_datastore_spec(self) -> m.SddcDatastoreSpec:
        return m.SddcDatastoreSpec(
            vsan_spec=m.VsanSpec(
                datastore_name="vsan-datastore",
                vsan_dedup=False,
                esa_config=m.VsanEsaConfig(enabled=True),
                failures_to_tolerate=1,
            ),
        )

    def _build_vcenter_spec(self) -> m.SddcVcenterSpec:
        hostnames = self._config.get("vcfHostnames", {})
        sizing = self._config.get("vcfSizing", {})
        return m.SddcVcenterSpec(
            vcenter_hostname=self._fqdn(hostnames.get("vcenter", "vc")),
            vm_size=sizing.get("vcenterSize", "medium"),
            storage_size=sizing.get("vcenterStorageSize", "lstorage"),
            root_vcenter_password=self._PLACEHOLDER_VCENTER_ROOT,
            admin_user_sso_username="administrator@vsphere.local",
            admin_user_sso_password=self._PLACEHOLDER_VCENTER_SSO,
            sso_domain="vsphere.local",
        )

    def _build_sddc_manager_spec(self) -> m.SddcManagerSpec:
        hostnames = self._config.get("vcfHostnames", {})
        return m.SddcManagerSpec(
            hostname=self._fqdn(hostnames.get("sddc_manager", "sddcm")),
            root_password=self._PLACEHOLDER_SDDC_MANAGER_ROOT,
            ssh_password=self._PLACEHOLDER_SDDC_MANAGER_SSH,
            local_user_password=self._PLACEHOLDER_SDDC_MANAGER_LOCAL,
            use_existing_deployment=True,
            # The installer's Workflow Options resolver reads VMwareProductVersion
            # off the SDDC Manager spec, not the top-level SddcSpec.version.
            # Missing this field surfaces as a hard 500 before the spec is even
            # validated: "VMwareProductVersion can not be null or empty".
            version=self._config.get("vcfInstallerProductVersion"),
        )

    def _build_dns_spec(self) -> m.DnsSpec:
        return m.DnsSpec(
            subdomain=self._config.get("fqdn", ""),
            nameservers=self._config.get("dnsServers", []),
        )

    # ---- Version-specific extras ----

    def _version_extras(self) -> dict[str, Any]:
        """Return the fields the SDK typed class doesn't model.

        - ``deployWithoutLicenseKeys`` is not modeled by the 9.1 SDK but is
          required on the wire for both 9.0 and 9.1 bringups in our setup.
        - ``vcfOperationsFleetManagementSpec`` is required on 9.0 and dropped
          on 9.1. The 9.1 SDK does not model it.
        - ``vspClusterSpec``, ``licenseServerSpec``, ``vidbSpec`` are required
          on 9.1 (VCF Services Platform, License Server, Identity Broker).
        - ``__env__`` is a project-private extra (not part of the VCF API)
          that Phase 3 reads to know which Secrets Manager namespace to
          fetch passwords from at runtime. The leading/trailing
          underscores keep it from colliding with any real installer field
          and signal "internal use" to anyone reading the JSON.
        - ``__region__`` is the AWS region the EVS environment lives in.
          Phase 3 reads it to point the Secrets Manager client at the
          right region without the operator having to remember
          ``--aws-region`` on every invocation. Same naming convention
          as ``__env__``.
        """
        extras: dict[str, Any] = {
            "deployWithoutLicenseKeys": True,
            "skipVsanEsaCertifiedDiskValidation": True,
        }

        env_id = self._environment_id
        if env_id:
            extras["__env__"] = env_id

        region = self._config.get("region")
        if region:
            extras["__region__"] = region

        if self._target_version == "9.0":
            hostnames = self._config.get("vcfHostnames", {})
            extras["vcfOperationsFleetManagementSpec"] = {
                "hostname": self._fqdn(hostnames.get("vcf_fleet", "vcffleet")),
                "rootUserPassword": "__SECRET:fleetManagerRoot__",
                "adminUserPassword": "__SECRET:fleetManagerAdmin__",
            }

        return extras

    def _build_91_typed_kwargs(self) -> dict[str, Any]:
        """Return kwargs for VCF 9.1-only typed fields on SddcSpec."""
        return {
            "vsp_cluster_spec": self._build_vsp_cluster_spec(),
            "license_server_spec": self._build_license_server_spec(),
            "vidb_spec": self._build_vidb_spec(),
        }

    def _build_vsp_cluster_spec(self) -> "m.SddcVspClusterSpec":
        """Build the VCF 9.1 VSP cluster spec (typed)."""
        hostnames = self._config.get("vcfHostnames", {})
        vlans = self._config.get("initialVlans", {})
        vm_mgmt_cidr = vlans.get("vmManagement", {}).get("cidr", "10.0.60.0/24")
        net = ipaddress.ip_network(vm_mgmt_cidr, strict=False)
        pool_start = str(net.network_address + 80)
        pool_end = str(net.network_address + 100)

        return m.SddcVspClusterSpec(
            instance_fqdn=self._fqdn(
                hostnames.get("vsp_instance", "vsp-instance")
            ),
            fleet_fqdn=self._fqdn(
                hostnames.get("vsp_fleet", "vsp-fleet")
            ),
            platform_fqdn=self._fqdn(
                hostnames.get("vsp_platform", "vsp-platform")
            ),
            system_user_password=self._PLACEHOLDER_SDDC_MANAGER_ROOT,
            ipv4_pool=m.IPv4Pool(
                ip_range=m.IpRange(
                    start_ip_address=pool_start,
                    end_ip_address=pool_end,
                ),
            ),
            size="small",
        )

    def _build_license_server_spec(self) -> "m.LicenseServerSpec":
        """Build the VCF 9.1 License Server spec (typed)."""
        hostnames = self._config.get("vcfHostnames", {})
        return m.LicenseServerSpec(
            hostname=self._fqdn(
                hostnames.get("vcf_license", "vcf-license")
            ),
        )

    def _build_vidb_spec(self) -> "m.VidbSpec":
        """Build the VCF 9.1 Virtual Identity Broker spec (typed)."""
        hostnames = self._config.get("vcfHostnames", {})
        return m.VidbSpec(
            hostname=self._fqdn(
                hostnames.get("vcf_vidb", "vcf-vidb")
            ),
            size="small",
        )

    # ---- Top-level assembly ----

    def build(self) -> m.SddcSpec:
        """Build the typed ``SddcSpec`` object.

        Returns:
            A ``vmware.vcf_installer.model_client.SddcSpec`` ready to pass to
            ``Sddcs.deploy_sddc``. Version-specific extras (license-key flag,
            fleet management spec on 9.0) are attached via
            ``_set_extra_fields``.

        Raises:
            ValueError: If ``vcfInstallerProductVersion`` is missing from
                the config. The installer rejects bringup specs without an
                exact product version (``VMwareProductVersion can not be
                null or empty``).
        """
        cfg = self._config

        installer_version = cfg.get("vcfInstallerProductVersion")
        if not installer_version:
            raise ValueError(
                "config.vcfInstallerProductVersion must be set (e.g. "
                "'9.0.2.0' or '9.1.0.0'). The installer requires an exact "
                "product version on the bringup spec. Check the installer "
                "UI (Settings → About) or the "
                "OVA file name for the exact version."
            )

        spec = m.SddcSpec(
            sddc_id=cfg.get("vcfInstance", "EVS-Management"),
            workflow_type="VCF",
            version=installer_version,
            ceip_enabled=cfg.get("ceip", False),
            skip_esx_thumbprint_validation=True,
            skip_gateway_ping_validation=True,
            management_pool_name=f"{self._cluster_name()}_pool",
            ntp_servers=cfg.get("ntp", ["time.aws.com"]),
            dns_spec=self._build_dns_spec(),
            network_specs=self._build_network_specs(),
            nsxt_spec=self._build_nsxt_spec(),
            vcenter_spec=self._build_vcenter_spec(),
            sddc_manager_spec=self._build_sddc_manager_spec(),
            cluster_spec=self._build_cluster_spec(),
            datastore_spec=self._build_datastore_spec(),
            dvs_specs=self._build_dvs_specs(),
            host_specs=self._build_host_specs(),
            vcf_operations_spec=self._build_vcf_operations_spec(),
            vcf_operations_collector_spec=self._build_vcf_operations_collector_spec(),
            **(self._build_91_typed_kwargs() if self._target_version == "9.1" else {}),
        )
        spec._set_extra_fields(self._version_extras())
        return spec
