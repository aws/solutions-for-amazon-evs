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
        verify_tls: False skips TLS cert verification entirely. True uses
            the system default CA trust store. Or a filesystem path (str)
            to a PEM cert to trust specifically - real chain validation
            against that exact pinned cert, recommended for a self-signed
            appliance instead of skipping validation altogether.
        timeout_seconds: Per-request HTTP timeout.
    """

    def __init__(
        self,
        host: str,
        username: str,
        password: str,
        verify_tls: bool | str = False,
        timeout_seconds: int = 60,
    ) -> None:
        self._base_url = f"https://{host}"
        self._timeout = timeout_seconds

        if verify_tls is False:
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

        # Shared session carries Basic auth on every request.
        self._session = requests.Session()
        self._session.auth = (username, password)
        self._session.verify = verify_tls

        # Optional wire-level debug: VCF_NSX_DEBUG=1 logs every request/
        # response body. The only way to see NSX's real error body when the
        # SDK returns an ApiError with all fields None (non-JSON-shaped reply).
        import os as _os
        if _os.environ.get("VCF_NSX_DEBUG") == "1":
            self._attach_wire_debug(self._session)

        # Build the VAPI connector + stub config once; every stub reuses it.
        # msg_protocol='rest' is critical: NSX stubs are generated is_vapi_rest=
        # False, and the default 'json' connector speaks JSON-RPC (which NSX
        # rejects with ``CoreException: JSON-RPC connector not supported...``).
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

        Triggered by VCF_NSX_DEBUG. When NSX returns an ApiError with all
        fields None the SDK has swallowed the real error body; wire logging is
        the only way to see it. Sensitive fields are redacted before logging.
        """
        from wire_redact import redact_body

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
            "node_version": getattr(props, "node_version", None),
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

        Backs the ``wait_for_*`` variants that block until a cross-stage NSX
        resource realizes. ``None`` or a ``RuntimeError`` (find_* "not found"
        signal) means keep polling; a non-None value returns immediately; any
        other exception propagates (real errors should fail fast, not retry).
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
                # policy slug. The mgmt API accepts only the realization UUID
                # (the policy id surfaces as error_code 5008 "Pool identifier
                # is null"), so do NOT fall back to id -- fail loud instead.
                uuid = d.get("unique_id") or d.get("realization_id")
                if not uuid:
                    raise RuntimeError(
                        f"NSX transport zone '{display_name}' found but its "
                        f"realization UUID is not populated yet; retry once "
                        f"NSX realization completes."
                    )
                return uuid
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
                uuid = d.get("unique_id") or d.get("realization_id")
                if not uuid:
                    raise RuntimeError(
                        f"NSX host switch profile '{display_name}' found but "
                        f"its realization UUID is not populated yet; retry "
                        f"once NSX realization completes."
                    )
                return uuid
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
                uuid = d.get("unique_id") or d.get("realization_id")
                if not uuid:
                    raise RuntimeError(
                        f"NSX IP pool '{display_name}' found but its "
                        f"realization UUID is not populated yet; retry once "
                        f"NSX realization completes."
                    )
                return uuid
        raise RuntimeError(f"NSX IP pool '{display_name}' not found")

    def find_transport_node_by_display_name(self, display_name: str) -> Any | None:
        """Find an edge transport node by display name. None if not present."""
        page = self.mgmt_transport_nodes.list_transport_nodes_with_deployment_info()
        for tn in page.results or []:
            d = tn.to_dict()
            if d.get("display_name") == display_name:
                return tn
        return None

    def delete_transport_node(self, transport_node_id: str, force: bool = True) -> None:
        """Delete an edge transport node (and its underlying vCenter VM).

        NSX rejects a second POST with the same ``display_name`` (uniqueness),
        so a failed edge deploy must be deleted before recreating, not retried
        in place. ``force=True`` (UI "force delete") is needed because a node
        stuck mid-realization often isn't in a state plain delete accepts.
        """
        self.mgmt_transport_nodes.delete_transport_node_with_deployment_info(
            transport_node_id, force=force,
        )

    def wait_for_transport_node_by_display_name(
        self, display_name: str,
        timeout_seconds: int = 120, poll_interval_seconds: int = 5,
    ) -> Any:
        """Like :meth:`find_transport_node_by_display_name` but blocks until
        the node appears in the mgmt-API listing (Stage 3 reading what Stage 2
        created). Returns the node, or raises ``TimeoutError`` if it never shows.
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
        edge cluster's policy-side projection shows up. Needed for cross-stage
        chaining (``deploy-edge-cluster``), where Stage 4's T0 creation can
        race ahead of the projection of the cluster Stage 3 just created.
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

        Used for ``Tier0Interface.edge_path`` to pin an uplink to a specific
        edge: without it NSX 9.x defaults both interfaces to the same node and
        rejects the second with ``error_code 503101 "Segment ... is already
        attached"``.

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
        consecutive_errors = 0
        max_consecutive_errors = 5

        while time.time() < deadline:
            try:
                last_state = (
                    self.mgmt_transport_node_state
                    .get_transport_node_state_with_deployment_info(
                        transport_node_id
                    )
                )
            except Exception as e:
                # Tolerate transient errors (502/reset/blip) during the
                # 10-20 min edge deploy. Only give up after several
                # consecutive failures.
                consecutive_errors += 1
                logger.warning(
                    "Error polling transport node %s state (attempt %d/%d): %s",
                    transport_node_id, consecutive_errors,
                    max_consecutive_errors, e,
                )
                if consecutive_errors >= max_consecutive_errors:
                    raise RuntimeError(
                        f"Could not poll transport node {transport_node_id} "
                        f"state after {consecutive_errors} consecutive "
                        f"errors: {e}"
                    ) from e
                time.sleep(poll_interval_seconds)
                continue
            consecutive_errors = 0

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
