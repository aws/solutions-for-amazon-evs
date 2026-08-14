# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Client for the NSX Manager API, backed by the official ``vcf-nsx`` SDK.

NSX exposes two parallel flavors:

  - Policy API       (/policy/api/v1/...)     — declarative. Preferred for
    T0/T1, BGP, segments, IP pools, host switch profiles, transport zones.
  - Management API   (/api/v1/...)            — imperative. Required for
    edge transport nodes and edge clusters (they have no policy-side CRUD).

The SDK mirrors this split. Policy clients live under
``vcf.nsx.policy.api.v1.*`` and management clients live under
``vcf.nsx.api.v1.*``. Both talk to the same NSX Manager host, so we build
one ``requests.Session`` + connector, and wrap both sides with shared auth.

This module exposes:

  - Top-level SDK client accessors (``policy_tier0s``, ``policy_tier1s``,
    ``policy_segments``, etc.) that return typed ``VapiInterface`` stubs.
  - Convenience lookups (``find_transport_zone_path``, ``find_compute_manager``,
    ``find_edge_cluster_policy_path``) that translate display names to
    policy paths or UUIDs, matching the shape of the hand-rolled client so
    ``edge_cluster_manager.py`` can move over without a big diff.
  - A ``ping`` method for connectivity smoke tests.
  - A transport-node state poller for edge realization.
"""

import logging
import time
from typing import Any

import requests
import urllib3
from vcf.nsx.api import v1_client as mgmt_v1
from vcf.nsx.api.v1 import fabric_client, transport_nodes_client
from vcf.nsx.policy.api.v1 import infra_client
from vcf.nsx.policy.api.v1.infra import (
    tier_0s_client,
    tier_1s_client,
)
from vcf.nsx.policy.api.v1.infra.ip_pools_client import IpSubnets
from vcf.nsx.policy.api.v1.infra.sites.enforcement_points_client import (
    EdgeClusters as PolicyEdgeClusters,
    TransportZones as PolicyTransportZones,
)
from vcf.nsx.policy.api.v1.infra.sites.enforcement_points.edge_clusters_client import (
    EdgeNodes as PolicyEdgeNodes,
)
from vcf.nsx.policy.api.v1.infra.tier_0s.locale_services.bgp_client import (
    Neighbors as BgpNeighbors,
)
from vcf.nsx.policy.api.v1.infra.tier_0s.locale_services_client import (
    Bgp as LocaleServicesBgp,
    Interfaces as Tier0Interfaces,
)
from vmware.vapi.bindings.stub import StubConfiguration
from vmware.vapi.lib.connect import get_requests_connector

logger = logging.getLogger(__name__)


class NsxClient:
    """SDK-backed client for NSX Manager's policy + management APIs.

    Args:
        host: NSX Manager host or VIP (no scheme, no path).
        username: NSX admin user.
        password: NSX admin password.
        verify_tls: If False, skip TLS cert verification.
        timeout_seconds: Per-request HTTP timeout.
    """

    def __init__(
        self,
        host: str,
        username: str,
        password: str,
        verify_tls: bool = False,
        timeout_seconds: int = 60,
    ) -> None:
        self._base_url = f"https://{host}"
        self._timeout = timeout_seconds

        if not verify_tls:
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

        # Shared session carries Basic auth on every request.
        self._session = requests.Session()
        self._session.auth = (username, password)
        self._session.verify = verify_tls

        # Optional wire-level debug: set VCF_NSX_DEBUG=1 to log every
        # request + response body passing through the session. Mirrors the
        # installer client's _attach_wire_debug so you can inspect NSX's
        # actual error body when the SDK returns an ApiError with all
        # fields None (which happens when the response isn't JSON-shaped
        # the way the SDK expects).
        import os as _os
        if _os.environ.get("VCF_NSX_DEBUG") == "1":
            self._attach_wire_debug(self._session)

        # Build the VAPI connector + stub config once; every stub reuses it.
        # msg_protocol='rest' is critical — every NSX stub is generated with
        # is_vapi_rest=False, and the default 'json' connector speaks JSON-RPC
        # which NSX doesn't accept. Wrong protocol surfaces as
        # ``CoreException: JSON-RPC connector not supported for this invocation``.
        connector = get_requests_connector(
            session=self._session,
            url=self._base_url,
            timeout=self._timeout,
            msg_protocol="rest",
        )
        self._config = StubConfiguration(connector)

        # Lazy-built stub cache. Populated on first access via the property
        # accessors below so we never instantiate stubs we don't use.
        self._cache: dict[str, Any] = {}

    # ---- Policy clients (declarative API) ----

    @property
    def policy_tier0s(self) -> infra_client.Tier0s:
        return self._cache.setdefault(
            "policy_tier0s", infra_client.Tier0s(self._config)
        )

    @property
    def policy_tier1s(self) -> infra_client.Tier1s:
        return self._cache.setdefault(
            "policy_tier1s", infra_client.Tier1s(self._config)
        )

    @property
    def policy_segments(self) -> infra_client.Segments:
        return self._cache.setdefault(
            "policy_segments", infra_client.Segments(self._config)
        )

    @property
    def policy_ip_pools(self) -> infra_client.IpPools:
        return self._cache.setdefault(
            "policy_ip_pools", infra_client.IpPools(self._config)
        )

    @property
    def policy_ip_pool_subnets(self) -> IpSubnets:
        return self._cache.setdefault(
            "policy_ip_pool_subnets", IpSubnets(self._config)
        )

    @property
    def policy_host_switch_profiles(self) -> infra_client.HostSwitchProfiles:
        return self._cache.setdefault(
            "policy_host_switch_profiles",
            infra_client.HostSwitchProfiles(self._config),
        )

    @property
    def policy_t0_locale_services(self) -> tier_0s_client.LocaleServices:
        return self._cache.setdefault(
            "policy_t0_locale_services",
            tier_0s_client.LocaleServices(self._config),
        )

    @property
    def policy_t1_locale_services(self) -> tier_1s_client.LocaleServices:
        return self._cache.setdefault(
            "policy_t1_locale_services",
            tier_1s_client.LocaleServices(self._config),
        )

    @property
    def policy_t0_interfaces(self) -> Tier0Interfaces:
        return self._cache.setdefault(
            "policy_t0_interfaces", Tier0Interfaces(self._config)
        )

    @property
    def policy_t0_static_routes(self) -> tier_0s_client.StaticRoutes:
        return self._cache.setdefault(
            "policy_t0_static_routes",
            tier_0s_client.StaticRoutes(self._config),
        )

    @property
    def policy_t0_prefix_lists(self) -> tier_0s_client.PrefixLists:
        return self._cache.setdefault(
            "policy_t0_prefix_lists",
            tier_0s_client.PrefixLists(self._config),
        )

    @property
    def policy_t0_bgp(self) -> LocaleServicesBgp:
        return self._cache.setdefault(
            "policy_t0_bgp", LocaleServicesBgp(self._config)
        )

    @property
    def policy_t0_bgp_neighbors(self) -> BgpNeighbors:
        return self._cache.setdefault(
            "policy_t0_bgp_neighbors", BgpNeighbors(self._config)
        )

    @property
    def policy_ep_edge_clusters(self) -> PolicyEdgeClusters:
        return self._cache.setdefault(
            "policy_ep_edge_clusters", PolicyEdgeClusters(self._config)
        )

    @property
    def policy_ep_edge_nodes(self) -> PolicyEdgeNodes:
        return self._cache.setdefault(
            "policy_ep_edge_nodes", PolicyEdgeNodes(self._config)
        )

    @property
    def policy_ep_transport_zones(self) -> PolicyTransportZones:
        return self._cache.setdefault(
            "policy_ep_transport_zones", PolicyTransportZones(self._config)
        )

    # ---- Management clients (imperative API) ----

    @property
    def mgmt_transport_nodes(self) -> mgmt_v1.TransportNodes:
        return self._cache.setdefault(
            "mgmt_transport_nodes", mgmt_v1.TransportNodes(self._config)
        )

    @property
    def mgmt_edge_clusters(self) -> mgmt_v1.EdgeClusters:
        return self._cache.setdefault(
            "mgmt_edge_clusters", mgmt_v1.EdgeClusters(self._config)
        )

    @property
    def mgmt_host_switch_profiles(self) -> mgmt_v1.HostSwitchProfiles:
        return self._cache.setdefault(
            "mgmt_host_switch_profiles",
            mgmt_v1.HostSwitchProfiles(self._config),
        )

    @property
    def mgmt_transport_zones(self) -> mgmt_v1.TransportZones:
        return self._cache.setdefault(
            "mgmt_transport_zones",
            mgmt_v1.TransportZones(self._config),
        )

    @property
    def mgmt_compute_managers(self) -> fabric_client.ComputeManagers:
        return self._cache.setdefault(
            "mgmt_compute_managers",
            fabric_client.ComputeManagers(self._config),
        )

    @property
    def mgmt_node(self) -> mgmt_v1.Node:
        return self._cache.setdefault("mgmt_node", mgmt_v1.Node(self._config))

    @property
    def mgmt_transport_node_state(self) -> transport_nodes_client.State:
        return self._cache.setdefault(
            "mgmt_transport_node_state",
            transport_nodes_client.State(self._config),
        )

    # ---- Connectivity check ----

    def _attach_wire_debug(self, session: "requests.Session") -> None:
        """Hook the requests.Session to log every HTTP request/response body.

        Triggered by the VCF_NSX_DEBUG env var. Mirrors the installer
        client's _attach_wire_debug. When NSX returns an ApiError with all
        fields None, the SDK has swallowed the actual error body — wire
        logging is the only way to see what the appliance really said.
        Sensitive fields (passwords, tokens) are redacted before logging.
        """
        from src.wire_redact import redact_body

        original_send = session.send

        def debug_send(request, **kwargs):  # type: ignore[no-untyped-def]
            if request.body:
                raw = (
                    request.body.decode("utf-8", errors="replace")
                    if isinstance(request.body, (bytes, bytearray))
                    else request.body
                )
                logger.debug(
                    "[WIRE] %s %s\n%s",
                    request.method, request.url, redact_body(raw),
                )
            else:
                logger.debug("[WIRE] %s %s", request.method, request.url)
            response = original_send(request, **kwargs)
            if not response.ok:
                logger.debug(
                    "[WIRE] <- %s %s\n%s",
                    response.status_code, response.reason,
                    redact_body(response.text),
                )
            return response

        session.send = debug_send  # type: ignore[method-assign]

    def ping(self) -> dict[str, Any]:
        """Fetch NSX node properties to confirm auth + reachability."""
        props = self.mgmt_node.node_read_node_properties()
        return {
            "node_version": props.node_version,
            "product_version": getattr(props, "product_version", None),
            "hostname": getattr(props, "hostname", None),
        }

    # ---- Resource lookups ----

    def _poll_until(
        self,
        fn: "Any",  # Callable[[], T] -> T (T is non-None on success)
        description: str,
        timeout_seconds: int,
        poll_interval_seconds: int,
    ) -> Any:
        """Poll a lookup function until it returns a non-None value.

        Used by the ``wait_for_*`` lookup variants that block until a
        cross-stage NSX resource finishes realizing on whichever API
        plane the next stage will read it from. The lookup function may:

          - Return ``None`` to indicate "not found yet" — keep polling.
          - Raise ``RuntimeError`` (our own "not found" signal from
            ``find_*`` helpers) — keep polling.
          - Return a non-None value — return that immediately.
          - Raise anything else — propagate (real errors should fail
            fast, not get retried).

        Polls every ``poll_interval_seconds`` until the deadline. Logs a
        progress line each retry so the operator can see why the chain
        is paused.
        """
        deadline = time.time() + timeout_seconds
        attempt = 0
        last_err: Exception | None = None
        while True:
            attempt += 1
            try:
                value = fn()
            except RuntimeError as e:
                # find_* helpers raise RuntimeError when the resource
                # doesn't exist yet — that's our "keep polling" signal.
                value = None
                last_err = e
            if value is not None:
                if attempt > 1:
                    logger.info("Resolved %s after %d attempt(s)",
                                description, attempt)
                return value
            if time.time() >= deadline:
                hint = f" (last error: {last_err})" if last_err else ""
                raise TimeoutError(
                    f"Timed out after {timeout_seconds}s waiting for "
                    f"{description}{hint}"
                )
            logger.info(
                "Waiting for %s (attempt %d, retry in %ds)",
                description, attempt, poll_interval_seconds,
            )
            time.sleep(poll_interval_seconds)

    def find_compute_manager(self, vcenter_host: str) -> Any:
        """Find the compute manager registered for a given vCenter host.

        NSX creates a compute manager for vCenter during bringup. Returns the
        full typed record; callers typically read ``.id``.
        """
        page = self.mgmt_compute_managers.list_compute_managers()
        for cm in page.results or []:
            server = cm.server or ""
            if server == vcenter_host or server.startswith(vcenter_host + "."):
                return cm
        raise RuntimeError(
            f"No NSX compute manager found for vCenter '{vcenter_host}'. "
            f"Verify bringup registered the vCenter with NSX."
        )

    def find_transport_zone_path(self, display_name: str) -> str:
        """Return the policy path of a transport zone by display name."""
        page = (
            self.policy_ep_transport_zones
            .policy_lm_list_transport_zones_for_enforcement_point(
                site_id="default",
                enforcementpoint_id="default",
            )
        )
        for tz in page.results or []:
            # Some list endpoints return generic VapiStruct objects rather
            # than typed subclasses (the SDK's polymorphic deserializer
            # doesn't always downcast). to_dict() works on both shapes.
            d = tz.to_dict()
            if d.get("display_name") == display_name:
                return d.get("path")
        raise RuntimeError(f"NSX transport zone '{display_name}' not found")

    def find_transport_zone_uuid(self, display_name: str) -> str:
        """Return the UUID of a transport zone by display name.

        Required for the management API (transport node creation), which
        doesn't accept policy paths — it wants raw UUIDs in
        ``transport_zone_id`` fields. Policy paths work for stages 4-7.
        """
        page = (
            self.policy_ep_transport_zones
            .policy_lm_list_transport_zones_for_enforcement_point(
                site_id="default",
                enforcementpoint_id="default",
            )
        )
        for tz in page.results or []:
            d = tz.to_dict()
            if d.get("display_name") == display_name:
                # ``unique_id`` is the realization-side UUID; ``id`` is the
                # policy resource id (often slugified human-readable).
                # The management API wants the realization UUID.
                return d.get("unique_id") or d.get("realization_id") or d.get("id")
        raise RuntimeError(f"NSX transport zone '{display_name}' not found")

    def find_host_switch_profile_path(self, display_name: str) -> str:
        """Return the policy path of an uplink host switch profile by display name."""
        page = self.policy_host_switch_profiles.policy_lm_list_policy_host_switch_profiles()
        for profile in page.results or []:
            d = profile.to_dict()
            if d.get("display_name") == display_name:
                return d.get("path")
        raise RuntimeError(f"NSX host switch profile '{display_name}' not found")

    def find_host_switch_profile_uuid(self, display_name: str) -> str:
        """Return the UUID of an uplink host switch profile by display name."""
        page = self.policy_host_switch_profiles.policy_lm_list_policy_host_switch_profiles()
        for profile in page.results or []:
            d = profile.to_dict()
            if d.get("display_name") == display_name:
                return d.get("unique_id") or d.get("realization_id") or d.get("id")
        raise RuntimeError(f"NSX host switch profile '{display_name}' not found")

    def find_ip_pool_path(self, display_name: str) -> str:
        """Return the policy path of an IP pool by display name."""
        page = self.policy_ip_pools.policy_lm_list_ip_address_pools()
        for pool in page.results or []:
            d = pool.to_dict()
            if d.get("display_name") == display_name:
                return d.get("path")
        raise RuntimeError(f"NSX IP pool '{display_name}' not found")

    def find_ip_pool_uuid(self, display_name: str) -> str:
        """Return the UUID of an IP pool by display name."""
        page = self.policy_ip_pools.policy_lm_list_ip_address_pools()
        for pool in page.results or []:
            d = pool.to_dict()
            if d.get("display_name") == display_name:
                return d.get("unique_id") or d.get("realization_id") or d.get("id")
        raise RuntimeError(f"NSX IP pool '{display_name}' not found")

    def find_transport_node_by_display_name(self, display_name: str) -> Any | None:
        """Find an edge transport node by display name. None if not present."""
        page = self.mgmt_transport_nodes.list_transport_nodes_with_deployment_info()
        for tn in page.results or []:
            d = tn.to_dict()
            if d.get("display_name") == display_name:
                return tn
        return None

    def wait_for_transport_node_by_display_name(
        self, display_name: str,
        timeout_seconds: int = 120, poll_interval_seconds: int = 5,
    ) -> Any:
        """Like :meth:`find_transport_node_by_display_name` but blocks until
        the transport node appears in the management-API listing. Used for
        Stage 3 reads what Stage 2 created.

        Returns the transport node (never None) — raises ``TimeoutError``
        if it never shows up.
        """
        return self._poll_until(
            lambda: self.find_transport_node_by_display_name(display_name),
            description=f"transport node '{display_name}'",
            timeout_seconds=timeout_seconds,
            poll_interval_seconds=poll_interval_seconds,
        )

    def find_edge_cluster_by_display_name(self, display_name: str) -> Any | None:
        """Find an edge cluster (management API) by display name."""
        page = self.mgmt_edge_clusters.list_edge_clusters()
        for ec in page.results or []:
            d = ec.to_dict()
            if d.get("display_name") == display_name:
                return ec
        return None

    def find_edge_cluster_policy_path(self, display_name: str) -> str:
        """Return the policy path of an edge cluster.

        Edge clusters are created via the management API; T0/T1 gateways
        reference them via a policy-side path. We find that path by listing
        edge clusters under the default enforcement point.
        """
        page = (
            self.policy_ep_edge_clusters
            .policy_lm_list_edge_clusters_for_enforcement_point(
                site_id="default",
                enforcementpoint_id="default",
            )
        )
        for ec in page.results or []:
            d = ec.to_dict()
            if d.get("display_name") == display_name:
                return d.get("path")
        raise RuntimeError(
            f"NSX edge cluster '{display_name}' not found on the policy side. "
            f"(Its management-API entry may still be realizing.)"
        )

    def wait_for_edge_cluster_policy_path(
        self, display_name: str,
        timeout_seconds: int = 300, poll_interval_seconds: int = 5,
    ) -> str:
        """Like :meth:`find_edge_cluster_policy_path` but retries until the
        policy-side projection of the edge cluster shows up.

        Used for cross-stage chaining (``deploy-edge-cluster``) where Stage
        4's T0 creation can race ahead of NSX's policy-side projection of
        the management-API edge cluster Stage 3 just created. Single-stage
        invocations don't need this — by the time the operator types the
        next command, NSX has caught up.
        """
        return self._poll_until(
            lambda: self.find_edge_cluster_policy_path(display_name),
            description=f"edge cluster '{display_name}' policy path",
            timeout_seconds=timeout_seconds,
            poll_interval_seconds=poll_interval_seconds,
        )

    def find_edge_node_policy_path(
        self, edge_cluster_display_name: str, edge_display_name: str,
    ) -> str:
        """Return the policy path of an edge transport node.

        Used for ``Tier0Interface.edge_path`` to pin an uplink interface to
        a specific edge — without that, NSX 9.x rejects a second interface
        on the same segment with
        ``error_code 503101 "Segment ... is already attached"`` because it
        defaults both interfaces to the same edge node.

        Steps:
          1. Find the policy edge cluster by display name to get its
             ``id`` (the policy resource id used in nested URLs).
          2. List edge nodes under that cluster.
          3. Match by display name (which is the edge transport node FQDN).
        """
        # Step 1: edge cluster -> id
        page = (
            self.policy_ep_edge_clusters
            .policy_lm_list_edge_clusters_for_enforcement_point(
                site_id="default",
                enforcementpoint_id="default",
            )
        )
        ec_id = None
        for ec in page.results or []:
            d = ec.to_dict()
            if d.get("display_name") == edge_cluster_display_name:
                ec_id = d.get("id") or d.get("unique_id")
                break
        if not ec_id:
            raise RuntimeError(
                f"NSX edge cluster '{edge_cluster_display_name}' not found on "
                f"the policy side."
            )

        # Step 2 + 3: edge nodes under that cluster -> path
        page = (
            self.policy_ep_edge_nodes
            .policy_lm_list_edge_nodes_under_edge_cluster_for_enforcement_point(
                site_id="default",
                enforcementpoint_id="default",
                edge_cluster_id=ec_id,
            )
        )
        for en in page.results or []:
            d = en.to_dict()
            if d.get("display_name") == edge_display_name:
                return d.get("path")
        raise RuntimeError(
            f"NSX edge node '{edge_display_name}' not found under edge cluster "
            f"'{edge_cluster_display_name}'."
        )

    def wait_for_edge_node_policy_path(
        self, edge_cluster_display_name: str, edge_display_name: str,
        timeout_seconds: int = 300, poll_interval_seconds: int = 5,
    ) -> str:
        """Like :meth:`find_edge_node_policy_path` but retries until the
        edge node's policy projection is visible. Used for cross-stage
        chaining where Stage 6 reads what Stages 2-3 created.
        """
        return self._poll_until(
            lambda: self.find_edge_node_policy_path(
                edge_cluster_display_name, edge_display_name,
            ),
            description=(
                f"edge node '{edge_display_name}' under edge cluster "
                f"'{edge_cluster_display_name}'"
            ),
            timeout_seconds=timeout_seconds,
            poll_interval_seconds=poll_interval_seconds,
        )

    # ---- Realization polling ----

    def wait_for_transport_node_state(
        self,
        transport_node_id: str,
        desired_state: str = "success",
        timeout_seconds: int = 1800,
        poll_interval_seconds: int = 30,
    ) -> Any:
        """Poll until a transport node reaches the desired state.

        Edge deployment (OVA onto vCenter, boot, fabric join) can take 10-20
        minutes. NSX's management API exposes a "state" sub-resource that
        tells us how far along the realization is.
        """
        deadline = time.time() + timeout_seconds
        last_state: Any = None

        while time.time() < deadline:
            last_state = (
                self.mgmt_transport_node_state
                .get_transport_node_state_with_deployment_info(
                    transport_node_id
                )
            )
            state_value = last_state.state or "unknown"
            deployment_state = (
                last_state.node_deployment_state.state
                if getattr(last_state, "node_deployment_state", None)
                else "unknown"
            )
            logger.info(
                "Transport node %s state=%s (deployment=%s)",
                transport_node_id, state_value, deployment_state,
            )
            if state_value == desired_state:
                return last_state
            if state_value == "failed":
                raise RuntimeError(
                    f"Transport node {transport_node_id} failed: "
                    f"state={state_value} deployment={deployment_state}"
                )
            time.sleep(poll_interval_seconds)

        raise TimeoutError(
            f"Transport node {transport_node_id} did not reach state "
            f"'{desired_state}' within {timeout_seconds}s."
        )
