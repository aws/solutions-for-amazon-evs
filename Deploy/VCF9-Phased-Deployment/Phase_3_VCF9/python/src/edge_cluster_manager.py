# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Orchestrates the NSX-direct Edge Cluster deployment workflow.

The deployment is broken into ordered stages. Each stage is idempotent
where possible — running it twice does not duplicate resources.

  prep-edge-cluster     Stage 1 - DVS port groups, NSX IP pool, uplink
                        profile, VLAN transport zone. Everything needed
                        before edge VMs can be deployed.

  deploy-edge-nodes     Stage 2 - Create NSX edge transport nodes. Each
                        call triggers NSX to deploy the edge OVA onto
                        vCenter and join the fabric. Polls for realization.

  create-edge-cluster   Stage 3 - Group the transport nodes into an edge
                        cluster (management API).

  create-tier0          Stage 4 - Create the Tier-0 gateway. HA mode,
                        failover mode, local ASN, edge cluster reference.

  create-tier1          Stage 5 - Create the Tier-1 gateway, linked to T0
                        and the edge cluster.

  configure-routing     Stage 6 - BGP neighbors, static routes, route
                        redistribution, and T0 uplink interfaces. Also
                        creates the VLAN uplink segments and prefix list
                        used by BGP outbound filters.

  create-anti-affinity  Stage 7 - vCenter DRS VM-VM anti-affinity rule
                        keeping the two edge VMs on separate hosts.

  deploy-edge-cluster   End-to-end - runs every stage in order.

Backing library: ``vcf-nsx`` SDK (``vcf.nsx.api.v1_client`` for the
imperative management-API resources, ``vcf.nsx.policy.api.v1.*`` for
the declarative policy resources). Requests/responses are typed objects
from ``vcf.nsx.model_client``; the dict-based ``NsxClient.patch`` /
``post`` wrappers are gone.
"""

import ipaddress
import json
import logging
from pathlib import Path
from typing import Any

from vcf.nsx import model_client as nsx_m

from src.nsx_client import NsxClient
from src.vcenter_client import VcenterClient

logger = logging.getLogger(__name__)


def _slugify(s: str) -> str:
    """Build a safe NSX resource id from a human-readable name."""
    return "".join(c if c.isalnum() else "-" for c in s.lower()).strip("-")


def _netmask_to_prefix(netmask: str) -> int:
    """Convert a dotted netmask (e.g. '255.255.255.0') to a CIDR prefix length."""
    return ipaddress.IPv4Network(f"0.0.0.0/{netmask}", strict=False).prefixlen


def _route_advertisement_types(flags: dict[str, bool]) -> list[str]:
    """Translate our route-advertisement flag dict into NSX enum values.

    NSX uses a fixed set of enum strings:
      TIER1_CONNECTED, TIER1_NAT, TIER1_LB_VIP, TIER1_LB_SNAT,
      TIER1_DNS_FORWARDER_IP, TIER1_IPSEC_LOCAL_ENDPOINT, TIER1_STATIC_ROUTES
    """
    mapping = {
        "connectedSegments":    "TIER1_CONNECTED",
        "natRules":             "TIER1_NAT",
        "lbVip":                "TIER1_LB_VIP",
        "lbSnat":               "TIER1_LB_SNAT",
        "dnsForwarder":         "TIER1_DNS_FORWARDER_IP",
        "ipsecLocalEndpoints":  "TIER1_IPSEC_LOCAL_ENDPOINT",
        "staticRoutes":         "TIER1_STATIC_ROUTES",
    }
    return [
        nsx_enum for spec_key, nsx_enum in mapping.items() if flags.get(spec_key)
    ]


class EdgeClusterManager:
    """Drives the NSX-direct edge cluster deployment via the vcf-nsx SDK.

    Args:
        nsx: Initialized :class:`NsxClient`.
        vcenter: Initialized :class:`VcenterClient` (pyvmomi-backed).
        vcenter_host: vCenter host/IP; used to find the NSX compute manager.
        spec_path: Path to the edge cluster spec JSON file produced by Phase 2.
    """

    def __init__(
        self,
        nsx: NsxClient,
        vcenter: VcenterClient,
        vcenter_host: str,
        spec_path: str | Path,
        sm_client: Any | None = None,
    ) -> None:
        self._nsx = nsx
        self._vcenter = vcenter
        self._vcenter_host = vcenter_host
        self._spec_path = Path(spec_path)
        self._spec: dict[str, Any] | None = None
        self._sm_client = sm_client

    # ---- Spec loading ----

    def load_spec(self) -> dict[str, Any]:
        if self._spec is not None:
            return self._spec
        if not self._spec_path.exists():
            raise FileNotFoundError(
                f"Edge cluster spec not found: {self._spec_path}. "
                f"Run pre-evs-sync-config and post-evs-sync-config first."
            )
        with open(self._spec_path) as f:
            spec = json.load(f)

        # Resolve any ``__SECRET:<role>__`` placeholders against AWS
        # Secrets Manager so the in-memory spec carries real passwords
        # while the on-disk JSON remains inert. Phase 2 emits the edge
        # appliance password as a placeholder; the env id lives at the
        # top level so the resolver knows where to look.
        env_id = spec.get("environmentId")
        if self._sm_client is not None and env_id and not env_id.startswith("PENDING"):
            from src.secret_resolver import SecretResolver
            resolver = SecretResolver(self._sm_client, env_id)
            resolver.resolve_in_place(spec)
        elif "__SECRET:" in json.dumps(spec):
            logger.warning(
                "Edge cluster spec contains __SECRET placeholders but no "
                "Secrets Manager client is wired up. Real deploys will "
                "fail with the placeholders on the wire."
            )

        self._spec = spec
        return self._spec

    # ---- Stage 1: prep resources ----

    def prep_resources(self, dry_run: bool = False) -> None:
        """Create the pre-requisite resources for edge VM deployment.

        Order:
          1. vCenter: DVS uplink TRUNK port group.
          2. NSX:     IP pool + subnet for edge TEPs.
          3. NSX:     uplink host-switch profile.
          4. NSX:     VLAN transport zone.

        Safe to re-run — every SDK call is a policy-API patch which is
        idempotent by resource id.
        """
        spec = self.load_spec()

        if dry_run:
            logger.info("DRY RUN — would create the following resources:")
            self._log_prep_plan(spec)
            return

        self._create_dvs_portgroups(spec)
        self._create_edge_tep_ip_pool(spec)
        self._create_uplink_profile(spec)
        self._create_vlan_transport_zone(spec)
        logger.info("Stage 1 (prep-edge-cluster) complete.")

    def _log_prep_plan(self, spec: dict[str, Any]) -> None:
        pg = spec["portGroups"]["uplink1"]
        ip_pool = spec["ipPool"]
        uplink_profile = spec["uplinkProfile"]
        vlan_tz = spec["transportZones"]["vlan"]
        dvs = spec["computeCluster"]["dvsName"]

        logger.info("  [vCenter] DVS port group:   %s on DVS %s (TRUNK 0-4094)", pg["name"], dvs)
        logger.info(
            "  [NSX]     IP pool:          %s  CIDR=%s gw=%s range=%s-%s",
            ip_pool["name"], ip_pool["cidr"], ip_pool["gateway"],
            ip_pool["rangeStart"], ip_pool["rangeEnd"],
        )
        logger.info(
            "  [NSX]     Uplink profile:   %s  MTU=%d transportVlan=%d",
            uplink_profile["name"], uplink_profile["mtu"],
            uplink_profile["transportVlan"],
        )
        logger.info("  [NSX]     VLAN TZ:          %s", vlan_tz["name"])

    # ---- Stage 1 sub-steps ----

    def _create_dvs_portgroups(self, spec: dict[str, Any]) -> None:
        pg = spec["portGroups"]["uplink1"]
        dvs_name = spec["computeCluster"]["dvsName"]
        self._vcenter.create_trunk_portgroup(dvs_name=dvs_name, portgroup_name=pg["name"])

    def _create_edge_tep_ip_pool(self, spec: dict[str, Any]) -> None:
        pool = spec["ipPool"]
        pool_id = _slugify(pool["name"])

        logger.info("Creating NSX IP pool '%s' (id=%s)", pool["name"], pool_id)
        self._nsx.policy_ip_pools.policy_lm_create_or_patch_ip_address_pool(
            ip_pool_id=pool_id,
            ip_address_pool=nsx_m.IpAddressPool(
                display_name=pool["name"],
                description=pool.get("description", ""),
            ),
        )

        # Add the subnet with its allocation range. IpAddressPoolStaticSubnet
        # carries the actual CIDR/gateway/range configuration; IpSubnets is
        # the polymorphic parent type so the SDK's type discriminator is
        # driven by resource_type set on the subnet object.
        subnet_id = "default-subnet"
        logger.info(
            "Adding subnet %s (range %s-%s, gw %s) to pool %s",
            pool["cidr"], pool["rangeStart"], pool["rangeEnd"], pool["gateway"],
            pool["name"],
        )
        self._nsx.policy_ip_pool_subnets.policy_lm_create_or_patch_ip_address_pool_subnet(
            ip_pool_id=pool_id,
            ip_subnet_id=subnet_id,
            ip_address_pool_subnet=nsx_m.IpAddressPoolStaticSubnet(
                resource_type="IpAddressPoolStaticSubnet",
                cidr=pool["cidr"],
                gateway_ip=pool["gateway"],
                allocation_ranges=[
                    nsx_m.IpPoolRange(
                        start=pool["rangeStart"],
                        end=pool["rangeEnd"],
                    ),
                ],
            ),
        )

    def _create_uplink_profile(self, spec: dict[str, Any]) -> None:
        """Create the policy uplink host switch profile.

        FAILOVER_ORDER with a single active uplink (no standby), matching
        our one-uplink-per-edge design.

        Idempotent via GET-first: NSX 9.x rejects PATCH on existing
        host-switch-profiles with ``error_code 500127`` ("Cannot create an
        object ... as it already exists"), unlike the IP pool + TZ
        resources which upsert fine. Short-circuit when the profile is
        already there.
        """
        profile = spec["uplinkProfile"]
        profile_id = _slugify(profile["name"])

        try:
            existing = self._nsx.policy_host_switch_profiles.policy_lm_get_policy_host_switch_profile(
                host_switch_profile_id=profile_id,
            )
        except Exception:
            existing = None
        if existing is not None:
            logger.info(
                "NSX uplink profile '%s' already exists (id=%s); skipping create",
                profile["name"], profile_id,
            )
            return

        teaming = nsx_m.TeamingPolicy(
            policy=profile["teamingPolicy"],
            active_list=[
                nsx_m.Uplink(uplink_name=name, uplink_type="PNIC")
                for name in profile["activeUplinks"]
            ],
        )
        body = nsx_m.PolicyUplinkHostSwitchProfile(
            resource_type="PolicyUplinkHostSwitchProfile",
            display_name=profile["name"],
            mtu=profile["mtu"],
            transport_vlan=profile["transportVlan"],
            teaming=teaming,
        )

        logger.info(
            "Creating NSX uplink profile '%s' (id=%s)",
            profile["name"], profile_id,
        )
        self._nsx.policy_host_switch_profiles.policy_lm_patch_policy_host_switch_profile(
            host_switch_profile_id=profile_id,
            policy_base_host_switch_profile=body,
        )

    def _create_vlan_transport_zone(self, spec: dict[str, Any]) -> None:
        vlan_tz = spec["transportZones"]["vlan"]
        tz_id = _slugify(vlan_tz["name"])

        # Idempotent via GET-first, same as _create_uplink_profile. NSX 9.x
        # returns error_code 500127 on a PATCH against an existing policy
        # resource in some subtrees.
        try:
            existing = self._nsx.policy_ep_transport_zones.policy_lm_read_transport_zone_for_enforcement_point(
                site_id="default",
                enforcementpoint_id="default",
                transport_zone_id=tz_id,
            )
        except Exception:
            existing = None
        if existing is not None:
            logger.info(
                "NSX VLAN transport zone '%s' already exists (id=%s); skipping create",
                vlan_tz["name"], tz_id,
            )
            return

        body = nsx_m.PolicyTransportZone(
            resource_type="PolicyTransportZone",
            display_name=vlan_tz["name"],
            tz_type="VLAN_BACKED",
            uplink_teaming_policy_names=vlan_tz.get(
                "uplinkTeamingPolicyNames", ["default"]
            ),
        )
        logger.info(
            "Creating NSX VLAN transport zone '%s' (id=%s)",
            vlan_tz["name"], tz_id,
        )
        self._nsx.policy_ep_transport_zones.policy_lm_patch_transport_zone_for_enforcement_point(
            site_id="default",
            enforcementpoint_id="default",
            transport_zone_id=tz_id,
            policy_transport_zone=body,
        )

    # ---- Stage 2: edge transport nodes ----

    def deploy_edge_nodes(self, dry_run: bool = False) -> None:
        """Deploy each edge VM as an NSX transport node.

        Per-edge flow:
          1. Build a typed ``TransportNode`` with embedded ``EdgeNode``
             deployment info + ``StandardHostSwitchSpec`` that references
             the uplink profile, IP pool, and transport zones from Stage 1.
          2. POST via the mgmt API (``create_transport_node_with_deployment_info``).
          3. Poll until the transport node reaches ``success`` state.

        Idempotent: if a transport node with the same display name already
        exists, it's skipped.
        """
        spec = self.load_spec()

        if dry_run:
            logger.info("DRY RUN — would deploy the following edge transport nodes:")
            for node in spec["edgeNodes"]:
                logger.info(
                    "  [NSX] Edge TN: %s  formFactor=%s mgmt=%s tep=%s/%s uplink=%s",
                    node["fqdn"], node["formFactor"], node["managementIP"],
                    node["tep"]["ip1"], node["tep"]["ip2"],
                    node["uplinks"][0]["ip"],
                )
            return

        # Cross-cutting NSX resource paths (from Stage 1) and vCenter MOIDs
        # that every edge body needs.
        # NOTE: stage 2 hits the management API, which references resources
        # by UUID, not by policy path. Stages 4-7 use the policy API and
        # need ``find_*_path`` versions of these helpers instead. Sending
        # a policy path to the mgmt API surfaces as
        # ``error_code 5008 "Pool identifier is null."`` from id-allocation.
        tz_overlay_uuid = self._nsx.find_transport_zone_uuid(
            spec["transportZones"]["overlay"]
        )
        tz_vlan_uuid = self._nsx.find_transport_zone_uuid(
            spec["transportZones"]["vlan"]["name"]
        )
        uplink_profile_uuid = self._nsx.find_host_switch_profile_uuid(
            spec["uplinkProfile"]["name"]
        )
        ip_pool_uuid = self._nsx.find_ip_pool_uuid(spec["ipPool"]["name"])

        cc = spec["computeCluster"]
        compute_manager = self._nsx.find_compute_manager(self._vcenter_host)
        compute_manager_id = compute_manager.id

        # vCenter MOIDs
        cluster_obj = self._vcenter.find_cluster(cc["clusterName"])
        cluster_moid = self._vcenter.moid(cluster_obj)
        datastore = self._vcenter.find_vsan_datastore_on_cluster(cc["clusterName"])
        datastore_moid = self._vcenter.moid(datastore)
        mgmt_pg_moid = self._vcenter.moid(
            self._vcenter.find_portgroup(
                cc["dvsName"], spec["edgeNodes"][0]["managementPortGroup"]
            )
        )
        uplink_pg_moid = self._vcenter.moid(
            self._vcenter.find_portgroup(
                cc["dvsName"], spec["portGroups"]["uplink1"]["name"]
            )
        )

        for node in spec["edgeNodes"]:
            self._deploy_single_edge(
                node=node,
                compute_manager_id=compute_manager_id,
                cluster_moid=cluster_moid,
                datastore_moid=datastore_moid,
                mgmt_pg_moid=mgmt_pg_moid,
                uplink_pg_moid=uplink_pg_moid,
                uplink_profile_uuid=uplink_profile_uuid,
                ip_pool_uuid=ip_pool_uuid,
                tz_overlay_uuid=tz_overlay_uuid,
                tz_vlan_uuid=tz_vlan_uuid,
            )
        logger.info("Stage 2 (deploy-edge-nodes) complete.")

    def _deploy_single_edge(
        self,
        node: dict[str, Any],
        compute_manager_id: str,
        cluster_moid: str,
        datastore_moid: str,
        mgmt_pg_moid: str,
        uplink_pg_moid: str,
        uplink_profile_uuid: str,
        ip_pool_uuid: str,
        tz_overlay_uuid: str,
        tz_vlan_uuid: str,
    ) -> None:
        fqdn = node["fqdn"]

        existing = self._nsx.find_transport_node_by_display_name(fqdn)
        if existing is not None:
            logger.info("Transport node '%s' already exists; skipping create", fqdn)
            return

        passwords = node["passwords"]

        vcenter_deployment = nsx_m.VsphereDeploymentConfig(
            placement_type="VsphereDeploymentConfig",
            vc_id=compute_manager_id,
            compute_id=cluster_moid,
            storage_id=datastore_moid,
            management_network_id=mgmt_pg_moid,
            management_port_subnets=[
                nsx_m.IPSubnet(
                    ip_addresses=[node["managementIP"]],
                    prefix_length=_netmask_to_prefix(node["managementNetmask"]),
                )
            ],
            default_gateway_addresses=[node["managementGateway"]],
            data_network_ids=[uplink_pg_moid],
        )

        edge_node = nsx_m.EdgeNode(
            resource_type="EdgeNode",
            display_name=fqdn,
            deployment_type="VIRTUAL_MACHINE",
            deployment_config=nsx_m.EdgeNodeDeploymentConfig(
                form_factor=node["formFactor"],
                node_user_settings=nsx_m.NodeUserSettings(
                    cli_username="admin",
                    cli_password=passwords["cli"],
                    audit_username="audit",
                    audit_password=passwords["audit"],
                    root_password=passwords["root"],
                ),
                # vm_deployment_config is typed as ``DeploymentConfig``; we
                # build the concrete ``VsphereDeploymentConfig`` and upcast
                # so the SDK converter accepts it. See note below for
                # why ``convert_to`` is needed for polymorphic fields.
                vm_deployment_config=vcenter_deployment.convert_to(
                    nsx_m.DeploymentConfig
                ),
            ),
            node_settings=nsx_m.EdgeNodeSettings(
                hostname=fqdn,
                allow_ssh_root_login=True,
                enable_ssh=True,
                dns_servers=node["dnsServers"],
                ntp_servers=node["ntpServers"],
                search_domains=node["searchDomains"],
            ),
        )

        host_switch = nsx_m.StandardHostSwitch(
            host_switch_name="nvds1",
            host_switch_mode="STANDARD",
            host_switch_profile_ids=[
                nsx_m.HostSwitchProfileTypeIdEntry(
                    key="UplinkHostSwitchProfile",
                    value=uplink_profile_uuid,
                )
            ],
            # ip_assignment_spec is typed as the abstract ``IpAssignmentSpec``;
            # upcast the concrete ``StaticIpPoolSpec``.
            ip_assignment_spec=nsx_m.StaticIpPoolSpec(
                resource_type="StaticIpPoolSpec",
                ip_pool_id=ip_pool_uuid,
            ).convert_to(nsx_m.IpAssignmentSpec),
            pnics=[
                nsx_m.Pnic(device_name="fp-eth0", uplink_name="uplink1")
            ],
            transport_zone_endpoints=[
                nsx_m.TransportZoneEndPoint(transport_zone_id=tz_overlay_uuid),
                nsx_m.TransportZoneEndPoint(transport_zone_id=tz_vlan_uuid),
            ],
        )
        host_switch_spec = nsx_m.StandardHostSwitchSpec(
            resource_type="StandardHostSwitchSpec",
            host_switches=[host_switch],
        )
        # The TransportNode model has three fields whose declared types are
        # polymorphic abstract bases:
        #   - host_switch_spec    : HostSwitchSpec   (concrete: StandardHostSwitchSpec)
        #   - node_deployment_info: Node             (concrete: EdgeNode)
        #   - host switch's
        #     ip_assignment_spec  : IpAssignmentSpec (concrete: StaticIpPoolSpec)
        # The SDK's REST converter does class equality (not isinstance), so
        # passing the concrete subclass triggers
        # ``CoreException: Expected class of type vcf.nsx.model.<base>, but
        # received vcf.nsx.model.<concrete>``. ``convert_to(BaseType)``
        # upcasts the struct-value while keeping every field set including
        # the ``resource_type`` discriminator, so the wire JSON is
        # byte-identical to what NSX expects.
        host_switch_spec = host_switch_spec.convert_to(nsx_m.HostSwitchSpec)
        edge_node = edge_node.convert_to(nsx_m.Node)

        body = nsx_m.TransportNode(
            resource_type="TransportNode",
            display_name=fqdn,
            node_deployment_info=edge_node,
            host_switch_spec=host_switch_spec,
        )

        logger.info("Creating edge transport node '%s' (NSX will deploy the OVA)", fqdn)
        created = self._nsx.mgmt_transport_nodes.create_transport_node_with_deployment_info(
            transport_node=body
        )
        tn_id = created.node_id or created.id
        if not tn_id:
            raise RuntimeError(
                f"NSX transport-node response missing id: {created!r}"
            )

        logger.info(
            "Edge TN '%s' created (id=%s); waiting for deployment to finish...",
            fqdn, tn_id,
        )
        self._nsx.wait_for_transport_node_state(tn_id)
        logger.info("Edge TN '%s' deployed successfully", fqdn)

    # ---- Stage 3: edge cluster grouping ----

    def create_edge_cluster(self, dry_run: bool = False) -> None:
        """Group the deployed transport nodes into an NSX edge cluster."""
        spec = self.load_spec()
        cluster_name = spec["edgeCluster"]["name"]

        if dry_run:
            members = [n["fqdn"] for n in spec["edgeNodes"]]
            logger.info(
                "DRY RUN — would create edge cluster '%s' with members %s",
                cluster_name, members,
            )
            return

        existing = self._nsx.find_edge_cluster_by_display_name(cluster_name)
        if existing is not None:
            logger.info("Edge cluster '%s' already exists; skipping create", cluster_name)
            return

        members: list[nsx_m.EdgeClusterMember] = []
        for node in spec["edgeNodes"]:
            # wait_for_ variant blocks until the management-API listing
            # actually shows the transport node — covers the
            # ``deploy-edge-cluster`` chained run where Stage 3 starts
            # immediately after Stage 2's last POST and the listing might
            # lag for a few seconds. Single-stage runs return immediately.
            tn = self._nsx.wait_for_transport_node_by_display_name(node["fqdn"])
            tn_id = tn.node_id or tn.id
            members.append(nsx_m.EdgeClusterMember(transport_node_id=tn_id))

        body = nsx_m.EdgeCluster(
            resource_type="EdgeCluster",
            display_name=cluster_name,
            members=members,
        )

        logger.info(
            "Creating NSX edge cluster '%s' with %d members",
            cluster_name, len(members),
        )
        self._nsx.mgmt_edge_clusters.create_edge_cluster(edge_cluster=body)
        logger.info("Stage 3 (create-edge-cluster) complete.")

    # ---- Stage 4: Tier-0 gateway ----

    def create_tier0(self, dry_run: bool = False) -> None:
        """Create the Tier-0 gateway and attach it to the edge cluster.

        This creates the T0 object with HA mode, failover mode, and BGP
        local ASN. Static routes and BGP neighbors come in stage 6.
        """
        spec = self.load_spec()
        t0 = spec["tier0"]
        t0_id = _slugify(t0["name"])

        if dry_run:
            logger.info(
                "DRY RUN — would create Tier-0 '%s' (HA=%s failover=%s ASN=%d)",
                t0["name"], t0["haMode"], t0["failoverMode"], t0["localAsn"],
            )
            return

        edge_cluster_path = self._nsx.wait_for_edge_cluster_policy_path(
            spec["edgeCluster"]["name"]
        )

        # 1. Create the T0 gateway itself.
        t0_body = nsx_m.Tier0(
            resource_type="Tier0",
            display_name=t0["name"],
            ha_mode=t0["haMode"],
            failover_mode=t0["failoverMode"],
        )
        logger.info("Creating Tier-0 '%s' (id=%s)", t0["name"], t0_id)
        self._nsx.policy_tier0s.policy_lm_patch_tier0(tier0_id=t0_id, tier0=t0_body)

        # 2. Create the locale-services child resource — this is where the
        #    edge-cluster attachment lives on the policy API.
        ls_id = "default"
        ls_body = nsx_m.LocaleServices(
            resource_type="LocaleServices",
            display_name=ls_id,
            edge_cluster_path=edge_cluster_path,
        )
        logger.info(
            "Attaching Tier-0 '%s' to edge cluster (locale-service=%s)", t0["name"], ls_id
        )
        self._nsx.policy_t0_locale_services.policy_lm_patch_tier0_locale_services(
            tier0_id=t0_id,
            locale_services_id=ls_id,
            locale_services=ls_body,
        )

        # 3. Enable BGP on the T0 with the configured local ASN. Neighbors
        #    come in stage 6.
        bgp_body = nsx_m.BgpRoutingConfig(
            resource_type="BgpRoutingConfig",
            enabled=True,
            local_as_num=str(t0["localAsn"]),
            ecmp=False,
        )
        logger.info("Enabling BGP on Tier-0 '%s' (ASN=%d)", t0["name"], t0["localAsn"])
        self._nsx.policy_t0_bgp.policy_lm_patch_bgp_routing_config(
            tier0_id=t0_id,
            locale_service_id=ls_id,
            bgp_routing_config=bgp_body,
        )
        logger.info("Stage 4 (create-tier0) complete.")

    # ---- Stage 5: Tier-1 gateway ----

    def create_tier1(self, dry_run: bool = False) -> None:
        """Create the Tier-1 gateway linked to the Tier-0 and edge cluster."""
        spec = self.load_spec()
        t0 = spec["tier0"]
        t1 = spec["tier1"]
        t0_id = _slugify(t0["name"])
        t1_id = _slugify(t1["name"])

        if dry_run:
            logger.info(
                "DRY RUN — would create Tier-1 '%s' (failover=%s, standbyRelocation=%s)",
                t1["name"], t1["failoverMode"], t1["standbyRelocation"],
            )
            return

        edge_cluster_path = self._nsx.wait_for_edge_cluster_policy_path(
            spec["edgeCluster"]["name"]
        )
        route_adv = _route_advertisement_types(t1["routeAdvertisement"])

        t1_body = nsx_m.Tier1(
            resource_type="Tier1",
            display_name=t1["name"],
            tier0_path=f"/infra/tier-0s/{t0_id}",
            failover_mode=t1["failoverMode"],
            enable_standby_relocation=t1["standbyRelocation"],
            route_advertisement_types=route_adv,
        )
        logger.info("Creating Tier-1 '%s' linked to Tier-0 '%s'", t1["name"], t0["name"])
        self._nsx.policy_tier1s.policy_lm_patch_tier1(tier1_id=t1_id, tier1=t1_body)

        # locale-services ties the T1 to the edge cluster so it's hosted there.
        ls_body = nsx_m.LocaleServices(
            resource_type="LocaleServices",
            display_name="default",
            edge_cluster_path=edge_cluster_path,
        )
        logger.info("Attaching Tier-1 '%s' to edge cluster", t1["name"])
        self._nsx.policy_t1_locale_services.policy_lm_patch_tier1_locale_services(
            tier1_id=t1_id,
            locale_services_id="default",
            locale_services=ls_body,
        )
        logger.info("Stage 5 (create-tier1) complete.")

    # ---- Stage 6: routing ----

    def configure_routing(self, dry_run: bool = False) -> None:
        """Configure the T0's uplink interfaces, BGP neighbors, static
        routes, and route redistribution.

        Order:
          1. Create a VLAN-backed segment on the uplink VLAN so each edge's
             uplink interface has somewhere to attach.
          2. Create one T0 uplink interface per edge, holding that edge's
             uplink IP.
          3. Create the RFC-1918 prefix list. Used directly as the BGP
             outbound filter (no intermediate route map — NSX 9 accepts a
             prefix-list path in ``out_route_filters``).
          4. Create one BGP neighbor per edge. Each peers with its assigned
             Route Server endpoint IP with the configured remote ASN. The
             source address is constrained to the owning edge's uplink IP
             so BGP traffic egresses the correct edge.
          5. Create the 3 static routes: default + 2 /32 host routes to the
             Route Server endpoints.
          6. Configure route redistribution by PATCHing the locale-service
             with a ``route_redistribution_config``.
        """
        spec = self.load_spec()
        t0 = spec["tier0"]
        t0_id = _slugify(t0["name"])
        ls_id = "default"

        if dry_run:
            logger.info("DRY RUN — would configure routing on Tier-0 '%s':", t0["name"])
            for node in spec["edgeNodes"]:
                uplink = node["uplinks"][0]
                peer = uplink["bgpNeighbor"]
                logger.info(
                    "  [T0 interface]  edge=%s ip=%s/%d vlan=%d",
                    node["fqdn"], uplink["ip"], uplink["prefixLength"], uplink["vlan"],
                )
                logger.info(
                    "  [BGP neighbor]  peer=%s remoteAsn=%d source=%s",
                    peer["ip"], peer["remoteAsn"], uplink["ip"],
                )
            for route in t0["staticRoutes"]:
                logger.info(
                    "  [static route]  %s via %s",
                    route["network"], route["nextHop"],
                )
            logger.info(
                "  [prefix list]   %s (%d prefixes)",
                t0["rfc1918PrefixList"]["name"],
                len(t0["rfc1918PrefixList"]["prefixes"]),
            )
            logger.info(
                "  [redistribution] sources=%s",
                t0["routeRedistribution"].get(
                    "sources",
                    t0["routeRedistribution"].get("tier0Sources", "<legacy>"),
                ),
            )
            return

        self._create_uplink_segments(spec)
        self._create_tier0_uplink_interfaces(spec, t0_id, ls_id)
        self._create_rfc1918_prefix_list(spec, t0_id)
        self._create_bgp_neighbors(spec, t0_id, ls_id)
        self._create_static_routes(spec, t0_id)
        self._configure_route_redistribution(spec, t0_id, ls_id)
        logger.info("Stage 6 (configure-routing) complete.")

    # ---- Stage 6 sub-steps ----

    @staticmethod
    def _environment_id_from_spec(spec: dict[str, Any]) -> str:
        env = spec.get("environmentId", "")
        if not env or env.startswith("PENDING"):
            raise RuntimeError(
                "environmentId not populated in edge cluster spec. "
                "Run post-evs-sync-config before configure-routing."
            )
        return env

    def _uplink_segment_id(self, spec: dict[str, Any]) -> str:
        env = self._environment_id_from_spec(spec)
        return _slugify(f"{env}-uplink-seg")

    def _uplink_segment_path(self, spec: dict[str, Any]) -> str:
        return f"/infra/segments/{self._uplink_segment_id(spec)}"

    def _create_uplink_segments(self, spec: dict[str, Any]) -> None:
        """Create one VLAN-backed segment on the uplink VLAN for T0 interfaces."""
        seg_id = self._uplink_segment_id(spec)
        vlan_tz_path = self._nsx.find_transport_zone_path(
            spec["transportZones"]["vlan"]["name"]
        )
        vlan_ids = sorted({n["uplinks"][0]["vlan"] for n in spec["edgeNodes"]})

        body = nsx_m.Segment(
            resource_type="Segment",
            display_name=seg_id,
            transport_zone_path=vlan_tz_path,
            vlan_ids=[str(v) for v in vlan_ids],
        )
        logger.info("Creating uplink VLAN segment '%s' on VLANs %s", seg_id, vlan_ids)
        self._nsx.policy_segments.policy_lm_patch_infra_segment(
            segment_id=seg_id, segment=body,
        )

    def _create_tier0_uplink_interfaces(
        self, spec: dict[str, Any], t0_id: str, ls_id: str,
    ) -> None:
        """One T0 uplink interface per edge, each holding that edge's uplink IP.

        Each interface must be pinned to a specific edge via ``edge_path``,
        otherwise NSX 9.x rejects the second interface with
        ``error_code 503101 "Segment ... is already attached"`` since both
        interfaces default to the same edge node and a segment can only
        attach to one interface per edge.
        """
        seg_path = self._uplink_segment_path(spec)
        edge_cluster_name = spec["edgeCluster"]["name"]

        for node in spec["edgeNodes"]:
            uplink = node["uplinks"][0]
            iface_id = _slugify(f"{node['name']}-uplink1")
            edge_path = self._nsx.wait_for_edge_node_policy_path(
                edge_cluster_name, node["fqdn"],
            )

            body = nsx_m.Tier0Interface(
                resource_type="Tier0Interface",
                display_name=iface_id,
                type="EXTERNAL",
                segment_path=seg_path,
                edge_path=edge_path,
                subnets=[
                    nsx_m.InterfaceSubnet(
                        ip_addresses=[uplink["ip"]],
                        prefix_len=uplink["prefixLength"],
                    )
                ],
            )
            logger.info(
                "Creating Tier-0 uplink interface '%s' (ip=%s/%d, edge=%s)",
                iface_id, uplink["ip"], uplink["prefixLength"], node["fqdn"],
            )
            self._nsx.policy_t0_interfaces.policy_lm_patch_tier0_interface(
                tier0_id=t0_id,
                locale_service_id=ls_id,
                interface_id=iface_id,
                tier0_interface=body,
            )

    def _create_rfc1918_prefix_list(
        self, spec: dict[str, Any], t0_id: str,
    ) -> None:
        pl = spec["tier0"]["rfc1918PrefixList"]
        pl_id = _slugify(pl["name"])

        # Each entry is now a dict with ``network`` plus optional ``ge`` /
        # ``le``. For backwards compatibility (in case anyone is hand-
        # editing edge_cluster_spec.json) we still accept a bare string,
        # which behaves like ``ge`` and ``le`` both unset (matches only
        # the literal supernet — see Phase 2 ``_RFC1918_PREFIXES`` for why
        # ``ge`` matters).
        prefixes: list[Any] = []
        for entry in pl["prefixes"]:
            if isinstance(entry, str):
                prefixes.append(
                    nsx_m.PrefixEntry(network=entry, action="PERMIT")
                )
            else:
                prefixes.append(
                    nsx_m.PrefixEntry(
                        network=entry["network"],
                        ge=entry.get("ge"),
                        le=entry.get("le"),
                        action="PERMIT",
                    )
                )
        # Catch-all deny — any non-listed network is not advertised. NSX 9
        # requires every PrefixEntry to carry a ``network`` field, including
        # DENY entries (validation error 255 "required property network is
        # missing"), so we make the catch-all explicit with 0.0.0.0/0.
        prefixes.append(nsx_m.PrefixEntry(network="0.0.0.0/0", action="DENY"))

        body = nsx_m.PrefixList(
            resource_type="PrefixList",
            display_name=pl["name"],
            prefixes=prefixes,
        )
        logger.info(
            "Creating prefix list '%s' on Tier-0 (%d permit + 1 deny)",
            pl["name"], len(pl["prefixes"]),
        )
        self._nsx.policy_t0_prefix_lists.policy_lm_patch_prefix_list(
            tier0_id=t0_id, prefix_list_id=pl_id, prefix_list=body,
        )

    def _create_bgp_neighbors(
        self, spec: dict[str, Any], t0_id: str, ls_id: str,
    ) -> None:
        """One BGP neighbor per edge.

        edge01 peers to routeServerEndpoint01Ip from its own uplink IP.
        edge02 peers to routeServerEndpoint02Ip from its own uplink IP.

        Outbound BGP filter is the RFC-1918 prefix list directly — we no
        longer wrap it in a route map. NSX 9 accepts a prefix-list path in
        ``out_route_filters`` and applies it as a permit-or-deny filter
        with no extra layer of indirection. Simpler config, same effect.
        """
        pl_id = _slugify(spec["tier0"]["rfc1918PrefixList"]["name"])
        pl_path = f"/infra/tier-0s/{t0_id}/prefix-lists/{pl_id}"

        for node in spec["edgeNodes"]:
            uplink = node["uplinks"][0]
            peer = uplink["bgpNeighbor"]
            neighbor_id = _slugify(f"{node['name']}-bgp")

            kwargs = dict(
                resource_type="BgpNeighborConfig",
                display_name=neighbor_id,
                neighbor_address=peer["ip"],
                remote_as_num=str(peer["remoteAsn"]),
                source_addresses=[uplink["ip"]],
                maximum_hop_limit=2,
                hold_down_time=15,
                keep_alive_time=5,
                route_filtering=[
                    nsx_m.BgpRouteFiltering(
                        address_family="IPV4",
                        enabled=True,
                        out_route_filters=[pl_path],
                    )
                ],
            )
            if peer.get("password"):
                kwargs["password"] = peer["password"]

            body = nsx_m.BgpNeighborConfig(**kwargs)
            logger.info(
                "Creating BGP neighbor '%s' peer=%s source=%s asn=%d",
                neighbor_id, peer["ip"], uplink["ip"], peer["remoteAsn"],
            )
            self._nsx.policy_t0_bgp_neighbors.policy_lm_patch_bgp_neighbor_config(
                tier0_id=t0_id,
                locale_service_id=ls_id,
                neighbor_id=neighbor_id,
                bgp_neighbor_config=body,
            )

    def _create_static_routes(
        self, spec: dict[str, Any], t0_id: str,
    ) -> None:
        for route in spec["tier0"]["staticRoutes"]:
            route_id = _slugify(route["name"])

            body = nsx_m.StaticRoutes(
                resource_type="StaticRoutes",
                display_name=route["name"],
                network=route["network"],
                next_hops=[
                    nsx_m.RouterNexthop(
                        ip_address=route["nextHop"],
                        admin_distance=1,
                    )
                ],
            )
            logger.info(
                "Creating static route '%s' (%s via %s)",
                route["name"], route["network"], route["nextHop"],
            )
            self._nsx.policy_t0_static_routes.policy_lm_patch_tier0_static_routes(
                tier0_id=t0_id, route_id=route_id, static_routes=body,
            )

    def _configure_route_redistribution(
        self, spec: dict[str, Any], t0_id: str, ls_id: str,
    ) -> None:
        """Configure route redistribution on the T0 locale-service.

        NSX 9 models this via the ``route_redistribution_config`` field on
        the ``LocaleServices`` object (there's no separate endpoint). We
        PATCH the locale service with the updated field; the edge cluster
        attachment set in Stage 4 is preserved because PATCH merges.

        Spec shape: a single ``sources`` list under
        ``tier0.routeRedistribution``, flattened across both T0 and T1
        enum types. We emit one ``Tier0RouteRedistributionRule`` named
        ``redistribution-types``.

        Backwards-compat: if the old two-list shape (``tier0Sources`` /
        ``tier1Sources``) is present we merge them into a single rule, and
        if the legacy 3-boolean shape (``tier0Connected`` /
        ``tier0StaticRoutes`` / ``tier1Connected``) is present we
        translate it. Both fallbacks emit a single rule too — the
        multi-rule path is gone.
        """
        rr = spec["tier0"]["routeRedistribution"]
        edge_cluster_path = self._nsx.wait_for_edge_cluster_policy_path(
            spec["edgeCluster"]["name"]
        )

        if "sources" in rr:
            types = list(rr.get("sources") or [])
        elif "tier0Sources" in rr or "tier1Sources" in rr:
            # Old two-list shape — merge into one.
            types = list(rr.get("tier0Sources") or []) + list(
                rr.get("tier1Sources") or []
            )
        else:
            # Legacy 3-boolean shape.
            types = []
            if rr.get("tier0Connected"):
                types.append("TIER0_CONNECTED")
            if rr.get("tier0StaticRoutes"):
                types.append("TIER0_STATIC")
            if rr.get("tier1Connected"):
                types.append("TIER1_CONNECTED")

        redistribution = nsx_m.Tier0RouteRedistributionConfig(
            bgp_enabled=True,
            redistribution_rules=[
                nsx_m.Tier0RouteRedistributionRule(
                    name="redistribution-types",
                    route_redistribution_types=types,
                )
            ],
        )

        ls_body = nsx_m.LocaleServices(
            resource_type="LocaleServices",
            display_name=ls_id,
            edge_cluster_path=edge_cluster_path,
            route_redistribution_config=redistribution,
        )
        logger.info(
            "Configuring Tier-0 route redistribution (types=%s)", types
        )
        self._nsx.policy_t0_locale_services.policy_lm_patch_tier0_locale_services(
            tier0_id=t0_id,
            locale_services_id=ls_id,
            locale_services=ls_body,
        )

    # ---- Stage 7: DRS anti-affinity rule ----

    def create_anti_affinity_rule(self, dry_run: bool = False) -> None:
        """Create a vCenter DRS VM-VM anti-affinity rule so the two edge VMs
        are placed on different hosts. Idempotent — skipped if a rule with
        the same name already exists on the cluster.
        """
        spec = self.load_spec()
        rule_name = spec["antiAffinityRule"]["name"]
        cluster_name = spec["computeCluster"]["clusterName"]
        vm_names = [n["fqdn"] for n in spec["edgeNodes"]]

        if dry_run:
            logger.info(
                "DRY RUN — would create anti-affinity rule '%s' on cluster '%s' "
                "for VMs %s",
                rule_name, cluster_name, vm_names,
            )
            return

        self._vcenter.create_vm_anti_affinity_rule(
            cluster_name=cluster_name,
            rule_name=rule_name,
            vm_names=vm_names,
        )
        logger.info("Stage 7 (create-anti-affinity) complete.")

    # ---- End-to-end ----

    def deploy_end_to_end(self, dry_run: bool = False) -> None:
        """Run every edge-cluster deployment stage in order.

        Stage order:
            1. prep_resources
            2. deploy_edge_nodes   (this stage is the longest — edge OVAs)
            3. create_edge_cluster
            4. create_tier0
            5. create_tier1
            6. configure_routing
            7. create_anti_affinity_rule
        """
        logger.info("Starting end-to-end edge cluster deployment...")
        self.prep_resources(dry_run=dry_run)
        self.deploy_edge_nodes(dry_run=dry_run)
        self.create_edge_cluster(dry_run=dry_run)
        self.create_tier0(dry_run=dry_run)
        self.create_tier1(dry_run=dry_run)
        self.configure_routing(dry_run=dry_run)
        self.create_anti_affinity_rule(dry_run=dry_run)
        logger.info("End-to-end edge cluster deployment complete.")
