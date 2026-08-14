# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Build the internal Edge Cluster JSON spec for Phase 3 NSX-direct deployment.

Phase 3's CLI reads this spec and translates it into NSX Manager REST calls
plus a few vCenter operations. It is our internal contract between Phase 2
(builds it) and Phase 3 (executes it) — NOT the shape of any single VMware
API endpoint.

Built in two passes (same as the bringup spec): ``pre-evs-sync-config`` writes
every static/hostname/network field and stubs the env-id-derived names with a
placeholder; ``post-evs-sync-config`` rewrites with the env id filled in.
Per-edge uplink BGP neighbor IPs come from the Phase 1 Route Server endpoint
ENIs, captured into config.json by config_sync.py.
"""

import ipaddress
import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_NAME_PLACEHOLDER = "PENDING_POST_EVS_SYNC"


class EdgeClusterSpec:
    """Builds and writes the internal Edge Cluster JSON spec.

    Args:
        output_path: Path to write the edge cluster spec JSON to.
        config: Phase 2 config dict.
        environment_id: Optional EVS environment ID. When None, env-derived
            names get a placeholder.
    """

    # Edge appliance password placeholder; Phase 3's secret_resolver swaps it
    # for a real password from Secrets Manager (``evs-<env_id>_edgeAppliance``).
    # Same value for root/cli/audit on both edge VMs — intentional.
    _PLACEHOLDER_EDGE_APPLIANCE = "__SECRET:edgeAppliance__"

    # Static config ------------------------------------------------------

    _FORM_FACTOR_DEFAULT = "LARGE"
    _HA_MODE = "ACTIVE_STANDBY"
    # NON_PREEMPTIVE on both gateways: once a failover happens, the newly-active
    # node stays active even after the original one recovers -- avoiding an
    # unnecessary second BGP session drop when the recovered node would
    # otherwise forcibly reclaim the active role. This matches AWS's own EVS
    # getting-started guide ("Ensure both the Tier-0 and Tier-1 gateways have
    # Non-preemptive failover... Non-preemptive is the NSX default and avoids
    # an unnecessary BGP session drop") and EVS's own reference NSX config
    # (NsxGatewayConfig.java step 8A: ha_mode=ACTIVE_STANDBY,
    # failover_mode=NON_PREEMPTIVE) used by their continuously-running
    # self-deployed canary against real VCF 9.0.2/9.1.0 environments.
    _T0_FAILOVER_MODE = "NON_PREEMPTIVE"
    _T1_FAILOVER_MODE = "NON_PREEMPTIVE"
    _EDGE_CLUSTER_LOCAL_ASN = 65000
    _BGP_REMOTE_ASN = 65022
    _MTU = 1500

    # VLANs — canonical VLAN IDs AWS Elastic VMware Service hardcodes on every
    # environment. NOT chosen per deployment (the third-octet-of-CIDR
    # convention does not apply). Source: AWS Console → EVS → VLAN subnets.
    _MGMT_VLAN = 20         # vmManagement (VM management, where vCenter etc. live)
    _EDGE_TEP_VLAN = 60     # edgeVTep (VLAN tag NSX edges use to encapsulate overlay traffic)
    _UPLINK_VLAN = 70       # nsxUplink (T0 north-south egress; carries BGP to AWS Route Server)

    # Port group names on the management DVS
    _MGMT_PORTGROUP_NAME = "pg-vm-mgmt"
    _UPLINK_PORTGROUP_NAME = "pg-edge-uplink1"

    # Transport zones (overlay TZ is created during bringup; hardcoded here).
    _OVERLAY_TZ_NAME = "tz-overlay-01"
    _VLAN_TZ_NAME_SUFFIX = "-tz-vlan01"

    # Phase 1 initialVlans CIDR keys.
    _EDGE_TEP_CIDR_KEY = "edgeVTep"
    _UPLINK_CIDR_KEY = "nsxUplink"
    _MGMT_CIDR_KEY = "vmManagement"

    # Edge TEP pool allocation range offsets (from the CIDR's network address).
    _EDGE_TEP_POOL_RANGE_START_OFFSET = 6
    _EDGE_TEP_POOL_RANGE_END_OFFSET = 254  # inclusive

    # Per-edge TEP IP offsets. edge01 gets .6/.7; edge02 gets .8/.9.
    _EDGE_TEP_IP_OFFSETS = [(6, 7), (8, 9)]

    # Per-edge uplink IP offsets on the nsxUplink CIDR (.10, .11).
    _EDGE_UPLINK_IP_OFFSETS = [10, 11]

    # Per-edge management IP offset on the vmManagement CIDR (edge01=.14, edge02=.15).
    _EDGE_MGMT_IP_OFFSETS = [14, 15]

    # RFC-1918 prefix list for BGP outbound filtering — stops the NSX edges
    # advertising non-private networks to the AWS Route Server. ``ge`` is set
    # to each supernet's own prefix length so a PERMIT entry matches the
    # supernet plus every route under it; without ``ge`` it matches only the
    # literal supernet (which the T0 doesn't carry), so nothing is advertised.
    _RFC1918_PREFIXES = [
        {"network": "10.0.0.0/8", "ge": 8},
        {"network": "172.16.0.0/12", "ge": 12},
        {"network": "192.168.0.0/16", "ge": 16},
    ]

    def __init__(
        self,
        output_path: str | Path,
        config: dict[str, Any],
        environment_id: str | None = None,
    ) -> None:
        self._output_path = Path(output_path)
        self._config = config
        self._environment_id = environment_id

    # ---- CIDR helpers ------------------------------------------------

    @staticmethod
    def _first_usable(cidr: str) -> str:
        net = ipaddress.ip_network(cidr, strict=False)
        return str(net.network_address + 1)

    @staticmethod
    def _ip_at_offset(cidr: str, offset: int) -> str:
        net = ipaddress.ip_network(cidr, strict=False)
        return str(net.network_address + offset)

    @staticmethod
    def _prefix_length(cidr: str) -> int:
        return ipaddress.ip_network(cidr, strict=False).prefixlen

    @staticmethod
    def _netmask(cidr: str) -> str:
        return str(ipaddress.ip_network(cidr, strict=False).netmask)

    def _require_vlan_cidr(self, key: str) -> str:
        cidr = self._config.get("initialVlans", {}).get(key, {}).get("cidr")
        if not cidr:
            raise KeyError(
                f"initialVlans.{key} missing from config — ensure "
                f"config.json is fully populated."
            )
        return cidr

    def _fqdn(self, short: str) -> str:
        return f"{short}.{self._config['fqdn']}"

    def _route_server_endpoint_ip(self, key: str) -> str:
        """Read a Route Server endpoint IP from config.

        Pre-sync pass (no environment_id): the placeholder is expected —
        post-evs-sync-config rewrites with real values — so return as-is.
        Post-sync pass (environment_id set): the value MUST be a real IP; a
        missing/placeholder/garbage value would otherwise be written into the
        spec and break north-south BGP when Phase 3 programs it into NSX.
        """
        value = self._config.get(key, _NAME_PLACEHOLDER)
        if not self._environment_id:
            return value
        if not value or value == _NAME_PLACEHOLDER:
            raise ValueError(
                f"{key} missing from config — Route Server endpoint IPs must "
                f"be captured (config_sync.py) before the post-evs-sync pass."
            )
        try:
            ipaddress.ip_address(value)
        except ValueError as exc:
            raise ValueError(
                f"{key} is not a valid IP address: {value!r}"
            ) from exc
        return value

    # ---- Env-id-derived names ----------------------------------------

    def _name_with_env(self, suffix: str) -> str:
        env = self._environment_id
        return f"{env}{suffix}" if env else _NAME_PLACEHOLDER

    def _edge_cluster_name(self) -> str:
        return self._name_with_env("-ec01")

    def _tier0_name(self) -> str:
        return self._name_with_env("-t0")

    def _tier1_name(self) -> str:
        return self._name_with_env("-t1")

    def _edge_tep_pool_name(self) -> str:
        return self._name_with_env("-edge-tep-pool")

    def _vlan_tz_name(self) -> str:
        return self._name_with_env(self._VLAN_TZ_NAME_SUFFIX)

    def _uplink_profile_name(self) -> str:
        return self._name_with_env("-edge-uplink-profile")

    def _anti_affinity_rule_name(self) -> str:
        return self._name_with_env("-edge-anti-affinity")

    def _rfc1918_prefix_list_name(self) -> str:
        return self._name_with_env("-rfc-1918-allow")

    # ---- Section builders --------------------------------------------

    def _build_compute_cluster(self) -> dict[str, Any]:
        env = self._environment_id or _NAME_PLACEHOLDER
        return {
            "datacenterName": f"{env}-dc01",
            "clusterName": f"{env}-cl01",
            "dvsName": f"{env}-dvs01",
        }

    def _build_port_groups(self) -> dict[str, Any]:
        return {
            "uplink1": {
                "name": self._UPLINK_PORTGROUP_NAME,
                "vlanMode": "TRUNK",
            },
        }

    def _build_ip_pool(self) -> dict[str, Any]:
        cidr = self._require_vlan_cidr(self._EDGE_TEP_CIDR_KEY)
        return {
            "name": self._edge_tep_pool_name(),
            "description": "Edge TEP IP Pool for the Management Edge Cluster",
            "cidr": cidr,
            "gateway": self._first_usable(cidr),
            "rangeStart": self._ip_at_offset(cidr, self._EDGE_TEP_POOL_RANGE_START_OFFSET),
            "rangeEnd": self._ip_at_offset(cidr, self._EDGE_TEP_POOL_RANGE_END_OFFSET),
        }

    def _build_uplink_profile(self) -> dict[str, Any]:
        return {
            "name": self._uplink_profile_name(),
            "mtu": self._MTU,
            "transportVlan": self._EDGE_TEP_VLAN,
            "teamingPolicy": "FAILOVER_ORDER",
            "activeUplinks": ["uplink1"],
        }

    def _build_transport_zones(self) -> dict[str, Any]:
        return {
            "overlay": self._OVERLAY_TZ_NAME,
            "vlan": {
                "name": self._vlan_tz_name(),
                "uplinkTeamingPolicyNames": ["default"],
            },
        }

    def _build_edge_node(self, index: int) -> dict[str, Any]:
        hostnames = self._config.get("vcfHostnames", {})
        short = hostnames.get(f"edge0{index + 1}", f"edge0{index + 1}")

        tep_cidr = self._require_vlan_cidr(self._EDGE_TEP_CIDR_KEY)
        tep1_offset, tep2_offset = self._EDGE_TEP_IP_OFFSETS[index]

        mgmt_cidr = self._require_vlan_cidr(self._MGMT_CIDR_KEY)
        mgmt_offset = self._EDGE_MGMT_IP_OFFSETS[index]

        uplink_cidr = self._require_vlan_cidr(self._UPLINK_CIDR_KEY)
        uplink_offset = self._EDGE_UPLINK_IP_OFFSETS[index]

        # Each edge peers to exactly one Route Server endpoint:
        #   edge01 -> endpoint01
        #   edge02 -> endpoint02
        peer_config_key = f"routeServerEndpoint0{index + 1}Ip"
        bgp_peer_ip = self._route_server_endpoint_ip(peer_config_key)

        sizing = self._config.get("vcfSizing", {})

        return {
            "name": short,
            "fqdn": self._fqdn(short),
            "formFactor": sizing.get("edgeFormFactor", self._FORM_FACTOR_DEFAULT),
            "managementIP": self._ip_at_offset(mgmt_cidr, mgmt_offset),
            "managementNetmask": self._netmask(mgmt_cidr),
            "managementGateway": self._first_usable(mgmt_cidr),
            "managementPortGroup": self._MGMT_PORTGROUP_NAME,
            "managementVlan": self._MGMT_VLAN,
            "dnsServers": self._config.get("dnsServers", []),
            "ntpServers": self._config.get("ntp", ["time.aws.com"]),
            "searchDomains": [self._config.get("fqdn", "")],
            "passwords": {
                "root": self._PLACEHOLDER_EDGE_APPLIANCE,
                "cli": self._PLACEHOLDER_EDGE_APPLIANCE,
                "audit": self._PLACEHOLDER_EDGE_APPLIANCE,
            },
            "tep": {
                "vlan": self._EDGE_TEP_VLAN,
                "ip1": self._ip_at_offset(tep_cidr, tep1_offset),
                "ip2": self._ip_at_offset(tep_cidr, tep2_offset),
                "gateway": self._first_usable(tep_cidr),
                "prefixLength": self._prefix_length(tep_cidr),
            },
            "uplinks": [
                {
                    "uplinkName": "uplink1",
                    "vlan": self._UPLINK_VLAN,
                    "ip": self._ip_at_offset(uplink_cidr, uplink_offset),
                    "prefixLength": self._prefix_length(uplink_cidr),
                    "bgpNeighbor": {
                        "ip": bgp_peer_ip,
                        "remoteAsn": self._BGP_REMOTE_ASN,
                        "password": "",
                    },
                },
            ],
        }

    def _build_edge_cluster(self) -> dict[str, Any]:
        return {
            "name": self._edge_cluster_name(),
            "haMode": self._HA_MODE,
        }

    def _build_tier0(self) -> dict[str, Any]:
        uplink_cidr = self._require_vlan_cidr(self._UPLINK_CIDR_KEY)
        uplink_gateway = self._first_usable(uplink_cidr)

        ep01 = self._route_server_endpoint_ip("routeServerEndpoint01Ip")
        ep02 = self._route_server_endpoint_ip("routeServerEndpoint02Ip")

        return {
            "name": self._tier0_name(),
            "haMode": self._HA_MODE,
            "failoverMode": self._T0_FAILOVER_MODE,
            "localAsn": self._EDGE_CLUSTER_LOCAL_ASN,
            # Route redistribution into BGP: one combined list of NSX
            # redistribution-type enums (T0 and T1 mixed). Phase 3 turns it
            # into one Tier0RouteRedistributionRule. Old two-list shape
            # (``tier0Sources``/``tier1Sources``) is still accepted.
            "routeRedistribution": {
                "sources": [
                    # T0 sources. ``TIER0_CONNECTED`` is intentionally off so
                    # we don't advertise the uplink subnet to AWS.
                    # ``TIER0_STATIC`` is off so we don't re-advertise the AWS
                    # Route Server endpoint /32s back to AWS (the T0 still
                    # forwards via those static routes; just not via BGP).
                    "TIER0_NAT",
                    "TIER0_IPSEC_LOCAL_IP",
                    "TIER0_DNS_FORWARDER_IP",
                    "TIER0_EVPN_TEP_IP",
                    "TIER0_ROUTER_LINK",
                    "TIER0_SERVICE_INTERFACE",
                    "TIER0_LOOPBACK_INTERFACE",
                    "TIER0_SEGMENT",
                    "INTER_VRF_STATIC",
                    "TGW_STATIC",
                    # T1 sources.
                    "TIER1_DNS_FORWARDER_IP",
                    "TIER1_STATIC",
                    "TIER1_LB_VIP",
                    "TIER1_NAT",
                    "TIER1_LB_SNAT",
                    "TIER1_IPSEC_LOCAL_ENDPOINT",
                    "TIER1_CONNECTED",
                    "TIER1_SERVICE_INTERFACE",
                    "TIER1_SEGMENT",
                ],
            },
            "staticRoutes": [
                {"name": "default", "network": "0.0.0.0/0", "nextHop": uplink_gateway},
                {"name": "ep01-host-route", "network": f"{ep01}/32", "nextHop": uplink_gateway},
                {"name": "ep02-host-route", "network": f"{ep02}/32", "nextHop": uplink_gateway},
            ],
            "rfc1918PrefixList": {
                "name": self._rfc1918_prefix_list_name(),
                "prefixes": self._RFC1918_PREFIXES,
            },
        }

    def _build_tier1(self) -> dict[str, Any]:
        return {
            "name": self._tier1_name(),
            "failoverMode": self._T1_FAILOVER_MODE,
            "standbyRelocation": False,
            # Everything the T1 can advertise to the T0, flipped on so the T0
            # sees connected segments, VPC subnets, NAT/LB/DNS/IPSec, and T1
            # static routes. The T0's redistribution rule decides what enters BGP.
            "routeAdvertisement": {
                "connectedSegments": True,
                "lbVip": True,
                "lbSnat": True,
                "dnsForwarder": True,
                "ipsecLocalEndpoints": True,
                "natRules": True,
                "staticRoutes": True,
            },
        }

    def _build_anti_affinity_rule(self) -> dict[str, Any]:
        return {
            "name": self._anti_affinity_rule_name(),
        }

    # ---- Top-level assembly ------------------------------------------

    def _build_spec(self) -> dict[str, Any]:
        return {
            "edgeClusterName": self._edge_cluster_name(),
            "environmentId": self._environment_id or _NAME_PLACEHOLDER,
            "region": self._config.get("region", ""),
            "domainSuffix": self._config.get("fqdn", ""),
            "computeCluster": self._build_compute_cluster(),
            "portGroups": self._build_port_groups(),
            "ipPool": self._build_ip_pool(),
            "uplinkProfile": self._build_uplink_profile(),
            "transportZones": self._build_transport_zones(),
            "edgeNodes": [
                self._build_edge_node(0),
                self._build_edge_node(1),
            ],
            "edgeCluster": self._build_edge_cluster(),
            "tier0": self._build_tier0(),
            "tier1": self._build_tier1(),
            "antiAffinityRule": self._build_anti_affinity_rule(),
        }

    def sync(self, dry_run: bool = False) -> dict[str, Any]:
        """Build and write the edge cluster spec.

        Args:
            dry_run: If True, print the result instead of writing.

        Returns:
            The edge cluster spec dict.
        """
        spec = self._build_spec()

        if dry_run:
            logger.info(
                "DRY RUN — would write edge cluster spec to %s", self._output_path
            )
            print(json.dumps(spec, indent=2, default=str))
            return spec

        self._output_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._output_path, "w") as f:
            json.dump(spec, f, indent=2)
            f.write("\n")
        logger.info("Wrote edge cluster spec to: %s", self._output_path)
        return spec
