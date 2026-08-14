# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Client for the VCF Installer API, backed by the official VCF Python SDK.

Replaces the hand-rolled ``requests``-based client. The SDK handles:

  - Request/response serialization (typed ``SddcSpec`` in, typed ``SddcTask``
    out — no dict drift).
  - REST path templating and query parameters.
  - Error surfacing via ``vmware.vcf_installer.model_client.Error``.

We still own two pieces that don't come for free with the SDK:

  1. Auth: ``POST /v1/tokens`` for an access token, then attach it as a
     ``Bearer`` header on the ``requests.Session`` used by subsequent
     stubs. The SDK exposes ``Tokens.create_token`` to generate the token
     but doesn't wire it into later requests automatically.
  2. TLS: the installer ships with a self-signed cert, so we disable cert
     verification on the session by default (same behavior as the old
     client). Flip this off in production.

Public API is shaped around the operations we actually perform:

  - start_bringup / get_bringup     — SDDC deployment workflow
  - configure_depot / get_depot_settings / sync_depot_metadata /
    get_depot_sync_info             — depot token + metadata refresh (the
    bits that used to be a manual pre-work step in the installer UI)
  - list_bundles / get_bundle /
    download_bundle                 — binary bundle catalog + downloads
  - list_releases                   — release compatibility catalog
  - get_task                        — generic task polling

``bringup_manager.py`` calls start/get; the depot/bundle/release surface is
exposed through new CLI actions in ``main.py``.
"""

import logging
from typing import Any

import requests
import urllib3
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from vmware.vapi.bindings.stub import StubConfiguration
from vmware.vapi.lib.connect import get_requests_connector
from vmware.vcf_installer import v1_client
from vmware.vcf_installer.model_client import (
    Bundle,
    BundleDownloadSpec,
    BundleUpdateSpec,
    DepotAccount,
    DepotConfiguration,
    DepotSettings,
    DepotSyncInfo as DepotSyncInfoModel,
    PageOfBundle,
    PageOfRelease,
    SddcSpec,
    SddcTask,
    Task,
    TokenCreationSpec,
)
from vmware.vcf_installer.v1.system.settings.depot_client import (
    DepotSyncInfo as DepotSyncInfoClient,
)
from vmware.vcf_installer.v1.system.settings_client import Depot as DepotClient

logger = logging.getLogger(__name__)


class InstallerClient:
    """SDK-backed client for the VCF 9 Installer REST API.

    Args:
        host: IP or FQDN of the installer appliance (no scheme, no path).
        username: Installer user (defaults to ``admin@local``).
        password: Installer password.
        verify_tls: False disables TLS cert verification entirely (the
            installer's self-signed cert fails default validation). True
            uses the system default CA trust store. Or a filesystem path
            (str) to a PEM cert to trust specifically - requests then
            performs real certificate chain validation against that exact
            pinned cert instead of skipping validation altogether. This is
            the recommended mode against a self-signed appliance.
        timeout_seconds: Per-request HTTP timeout on the underlying session.
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
        self._username = username
        self._password = password
        self._timeout = timeout_seconds

        if verify_tls is False:
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

        # One requests.Session powers every subsequent SDK call. We mutate
        # its headers to attach the Bearer token after /v1/tokens succeeds.
        self._session = requests.Session()
        self._session.verify = verify_tls

        # Retry transient wire failures -- the VCF installer resets idle TCP
        # connections on a short interval, so without this a poll loop can crash
        # with ConnectionResetError on the next call after an idle period.
        retry = Retry(
            total=5,
            connect=5,
            read=5,
            status=3,
            backoff_factor=2,  # 2, 4, 8, 16, 32 seconds
            allowed_methods={"GET", "HEAD", "OPTIONS", "PATCH"},
            status_forcelist={502, 503, 504},
        )
        adapter = HTTPAdapter(max_retries=retry)
        self._session.mount("https://", adapter)
        self._session.mount("http://", adapter)

        # Lazy-built stubs (instantiated once auth succeeds).
        self._access_token: str | None = None
        self._sddcs: v1_client.Sddcs | None = None
        self._tasks: v1_client.Tasks | None = None
        self._bundles: v1_client.Bundles | None = None
        self._releases: v1_client.Releases | None = None
        self._depot: DepotClient | None = None
        self._depot_sync: DepotSyncInfoClient | None = None

    # ---- SDK stub wiring ----

    def _make_stub_config(self) -> StubConfiguration:
        """Build a ``StubConfiguration`` bound to our shared requests session.

        Called both anonymously (for ``Tokens.create_token``) and after auth;
        the session carries the ``Authorization`` header either way.

        ``msg_protocol='rest'`` is critical: installer stubs are Swagger-REST
        (``is_vapi_rest=False``), and the default ``'json'`` connector speaks
        JSON-RPC which the installer rejects (``CoreException: JSON-RPC
        connector not supported for this invocation``).
        """
        connector = get_requests_connector(
            session=self._session,
            url=self._base_url,
            timeout=self._timeout,
            msg_protocol="rest",
            # pool_size=0 prevents the SDK from re-mounting plain
            # HTTPAdapters (no retries) over our Retry adapter on every
            # call to _make_stub_config.
            pool_size=0,
        )
        return StubConfiguration(connector)

    def refresh_token(self) -> bool:
        """Re-acquire the access token, replacing the stale one.

        Returns True if the refresh succeeded (installer is still alive),
        False if the token endpoint is unreachable (appliance has likely
        transitioned to SDDC Manager).
        """
        logger.info("Refreshing access token from %s/v1/tokens", self._base_url)
        try:
            tokens_stub = v1_client.Tokens(self._make_stub_config())
            pair = tokens_stub.create_token(
                TokenCreationSpec(
                    username=self._username,
                    password=self._password,
                )
            )
            if pair is None or not pair.access_token:
                logger.warning("Token refresh returned no access token")
                return False
        except Exception as e:  # noqa: BLE001
            # A refresh failure is EXPECTED at the installer->SDDC-Manager
            # handoff (the token endpoint stops answering), so we still return
            # False either way. But distinguish the cause in the log so a real
            # auth/server error isn't silently misread as a clean handoff.
            if isinstance(e, (requests.exceptions.ConnectionError,
                              requests.exceptions.Timeout)):
                logger.info(
                    "Token refresh: endpoint unreachable (%s) — installer has "
                    "likely handed off to SDDC Manager", e)
            else:
                logger.warning(
                    "Token refresh failed with a non-connection error "
                    "(%s: %s) — treating as installer-gone, but this may be an "
                    "auth/server problem rather than a clean handoff",
                    type(e).__name__, e)
            return False

        self._access_token = pair.access_token
        self._session.headers["Authorization"] = f"Bearer {self._access_token}"
        logger.info("Token refreshed successfully")

        cfg = self._make_stub_config()
        self._sddcs = v1_client.Sddcs(cfg)
        self._tasks = v1_client.Tasks(cfg)
        self._bundles = v1_client.Bundles(cfg)
        self._releases = v1_client.Releases(cfg)
        self._depot = DepotClient(cfg)
        self._depot_sync = DepotSyncInfoClient(cfg)
        return True

    def _ensure_authenticated(self) -> None:
        """Acquire an access token via ``Tokens.create_token`` if we lack one.

        Writes the token into the session as a Bearer header so every later
        stub method picks it up automatically.
        """
        if self._access_token:
            return

        logger.info("Acquiring access token from %s/v1/tokens", self._base_url)
        tokens_stub = v1_client.Tokens(self._make_stub_config())
        pair = tokens_stub.create_token(
            TokenCreationSpec(
                username=self._username,
                password=self._password,
            )
        )
        if pair is None or not pair.access_token:
            raise RuntimeError(
                f"Installer /v1/tokens did not return an access token: {pair}"
            )

        self._access_token = pair.access_token
        self._session.headers["Authorization"] = f"Bearer {self._access_token}"
        logger.info("Authenticated to installer API")

        # Optional request/response debug: VCF_INSTALLER_DEBUG=1 logs the exact
        # wire JSON to /v1/sddcs and the installer's error body -- useful for
        # reverse-engineering undocumented field requirements.
        import os
        if os.environ.get("VCF_INSTALLER_DEBUG"):
            self._attach_wire_debug()

        # Build the authenticated stubs now that the header is set.
        cfg = self._make_stub_config()
        self._sddcs = v1_client.Sddcs(cfg)
        self._tasks = v1_client.Tasks(cfg)
        self._bundles = v1_client.Bundles(cfg)
        self._releases = v1_client.Releases(cfg)
        self._depot = DepotClient(cfg)
        self._depot_sync = DepotSyncInfoClient(cfg)

    # ---- Raw REST helpers (for endpoints the SDK doesn't cover) ----

    def raw_get(self, path: str, **params: Any) -> Any:
        """GET a raw installer endpoint, return parsed JSON.

        For endpoints the SDK doesn't expose (e.g. /v1/product-binaries, the
        9.0+ replacement for /v1/bundles, unmodeled by the 9.1 SDK). Reuses the
        authenticated session so Bearer auth + TLS-skip stay consistent.
        """
        self._ensure_authenticated()
        url = f"{self._base_url}{path}"
        logger.info("GET %s", url)
        response = self._session.get(
            url, params=params or None, timeout=self._timeout
        )
        self._raise_for_rest_error(response)
        return response.json() if response.content else None

    def raw_post(
        self, path: str, body: Any = None, **params: Any,
    ) -> Any:
        """POST a raw installer endpoint with an optional JSON body."""
        self._ensure_authenticated()
        url = f"{self._base_url}{path}"
        logger.info("POST %s", url)
        response = self._session.post(
            url,
            json=body,
            params=params or None,
            timeout=self._timeout,
        )
        self._raise_for_rest_error(response)
        return response.json() if response.content else None

    @staticmethod
    def _raise_for_rest_error(response: Any) -> None:
        """Raise a clean RuntimeError on HTTP error (SDK error types don't
        apply to hand-written REST calls)."""
        if response.ok:
            return
        try:
            body = response.json()
        except Exception:  # pragma: no cover - diagnostic
            body = response.text
        raise RuntimeError(
            f"{response.request.method} {response.url} "
            f"returned {response.status_code}: {body}"
        )

    def _attach_wire_debug(self) -> None:
        """Hook requests.Session to log every HTTP request/response body.

        Triggered by VCF_INSTALLER_DEBUG. Logs each outgoing POST/PATCH body and
        every non-2xx response, with sensitive fields (passwords, tokens)
        redacted first.
        """
        from wire_redact import redact_body

        original_send = self._session.send

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

        self._session.send = debug_send

    # ---- Public API (matches the old hand-rolled client) ----

    def start_bringup(self, spec: SddcSpec) -> SddcTask:
        """POST the typed bringup spec to ``/v1/sddcs``.

        Args:
            spec: Fully-populated typed ``SddcSpec``. Produced by
                ``SddcSpecBuilder.build()`` (or by round-tripping the
                bringup_spec.json through ``sdk_serde.json_to_typed``).

        Returns:
            The installer's ``SddcTask`` response (contains the workflow id).
        """
        self._ensure_authenticated()
        assert self._sddcs is not None  # set by _ensure_authenticated
        logger.info("POST %s/v1/sddcs", self._base_url)
        task = self._sddcs.deploy_sddc(sddc_spec=spec)
        if task is None:
            raise RuntimeError("Installer /v1/sddcs returned an empty response")
        return task

    def get_bringup(self, workflow_id: str) -> SddcTask:
        """Fetch the current status of a bringup workflow.

        Args:
            workflow_id: The id returned from ``start_bringup``.
        """
        self._ensure_authenticated()
        assert self._sddcs is not None
        logger.debug("GET %s/v1/sddcs/%s", self._base_url, workflow_id)
        task = self._sddcs.get_sddc_task_by_id(id=workflow_id)
        if task is None:
            raise RuntimeError(
                f"Installer /v1/sddcs/{workflow_id} returned an empty response"
            )
        return task

    def retry_sddc(
        self, workflow_id: str, spec: SddcSpec | None = None, skip_validations: bool | None = None,
    ) -> SddcTask:
        """Retry a bringup workflow, optionally with a modified spec.

        Resubmits a bringup that failed password validation with a regenerated
        password, WITHOUT starting a new workflow: a fresh POST against an
        appliance with partial bringup state risks duplicating or corrupting
        the deployment, so ``retry_sddc`` resumes the SAME workflow id.

        Args:
            workflow_id: The id of the failed workflow to retry.
            spec: Optional replacement spec (e.g. with regenerated
                password fields). If omitted, the installer retries with
                whatever spec it already has stored for this workflow.
            skip_validations: Optional -- if True, skip the installer's
                pre-flight validation on retry. Explicit opt-in (default
                None/unset) since skipping validation is a deliberate choice.
        """
        self._ensure_authenticated()
        assert self._sddcs is not None
        logger.info("POST %s/v1/sddcs/%s (retry)", self._base_url, workflow_id)
        task = self._sddcs.retry_sddc(
            id=workflow_id, sddc_spec=spec, skip_validations=skip_validations,
        )
        if task is None:
            raise RuntimeError(f"Installer /v1/sddcs/{workflow_id} retry returned an empty response")
        return task

    def list_bringup_tasks(self) -> list:
        """List all SDDC bringup tasks/workflows known to this installer.

        Used to detect an already-started/succeeded bringup before POSTing a
        new one: ``start_bringup`` has no idempotency, and re-POSTing against a
        partially/fully brought-up SDDC can corrupt or duplicate it.
        """
        self._ensure_authenticated()
        assert self._sddcs is not None
        page = self._sddcs.get_sddc_tasks()
        return list(getattr(page, "elements", None) or [])

    # ---- Depot settings ----

    def configure_depot(
        self,
        *,
        download_token: str,
        username: str | None = None,
        password: str | None = None,
        offline: bool = False,
        offline_hostname: str | None = None,
        offline_port: int | None = None,
        offline_url: str | None = None,
    ) -> None:
        """Configure the installer's depot settings.

        The VMware (Broadcom) depot is the common case — supply a download
        token (and optionally a username/password pair). For an offline
        depot, flip ``offline=True`` and provide hostname/port/url.

        Args:
            download_token: Broadcom download token.
            username: Optional Broadcom account username.
            password: Optional Broadcom account password.
            offline: If True, register an offline/bundled depot instead of
                the VMware cloud depot.
            offline_hostname: Hostname of the offline depot (required when
                ``offline=True``).
            offline_port: Port of the offline depot.
            offline_url: Full URL of the offline depot (alternative to
                host/port).
        """
        self._ensure_authenticated()
        assert self._depot is not None

        vmware_account: DepotAccount | None = None
        if not offline:
            vmware_account = DepotAccount(
                username=username,
                password=password,
                download_token=download_token,
            )

        depot_config: DepotConfiguration | None = None
        if offline:
            depot_config = DepotConfiguration(
                is_offline_depot=True,
                hostname=offline_hostname,
                port=offline_port,
                url=offline_url,
            )

        settings = DepotSettings(
            vmware_account=vmware_account,
            depot_configuration=depot_config,
        )
        logger.info(
            "PATCH %s/v1/system/settings/depot  (offline=%s)",
            self._base_url, offline,
        )
        self._depot.update_depot_settings(depot_settings=settings)

    def get_depot_settings(self) -> DepotSettings:
        """Read the current depot settings (tokens returned redacted)."""
        self._ensure_authenticated()
        assert self._depot is not None
        settings = self._depot.get_depot_settings()
        if settings is None:
            raise RuntimeError(
                "Installer /v1/system/settings/depot returned an empty response"
            )
        return settings

    def sync_depot_metadata(self) -> None:
        """Kick off a depot metadata sync.

        This tells the installer to refresh its catalog of available
        bundles + releases from the configured depot. Call after
        ``configure_depot`` and before ``list_bundles`` / ``list_releases``.
        """
        self._ensure_authenticated()
        assert self._depot_sync is not None
        logger.info("POST %s/v1/system/settings/depot/sync", self._base_url)
        self._depot_sync.sync_depot_metadata()

    def get_depot_sync_info(self) -> DepotSyncInfoModel:
        """Fetch the current depot sync status.

        The returned ``DepotSyncInfo`` carries ``sync_status``
        (IN_PROGRESS / SUCCESS / FAILURE), an error message if any, and
        the last-completion timestamp.
        """
        self._ensure_authenticated()
        assert self._depot_sync is not None
        info = self._depot_sync.get_depot_sync_info()
        if info is None:
            raise RuntimeError(
                "Installer /v1/system/settings/depot/sync-info returned "
                "an empty response"
            )
        return info

    # ---- Bundles ----

    def list_bundles(
        self,
        *,
        product_type: str | None = None,
        bundle_type: str | None = None,
        is_compliant: bool | None = None,
    ) -> PageOfBundle:
        """List bundles available in the depot.

        Args:
            product_type: e.g. ``VCF``, ``NSX``, ``VCENTER``.
            bundle_type: e.g. ``INSTALL``, ``PATCH``, ``UPGRADE``.
            is_compliant: True to only show bundles compatible with the
                currently installed version.
        """
        self._ensure_authenticated()
        assert self._bundles is not None
        return self._bundles.get_bundles(
            product_type=product_type,
            bundle_type=bundle_type,
            is_compliant=is_compliant,
        )

    def get_bundle(self, bundle_id: str) -> Bundle:
        """Fetch a single bundle (including current ``download_status``)."""
        self._ensure_authenticated()
        assert self._bundles is not None
        bundle = self._bundles.get_bundle(id=bundle_id)
        if bundle is None:
            raise RuntimeError(
                f"Installer /v1/bundles/{bundle_id} returned an empty response"
            )
        return bundle

    def download_bundle(self, bundle_id: str) -> Task:
        """Trigger an immediate download for the given bundle.

        Returns the ``Task`` that tracks the download. Poll
        ``get_task(task.id)`` or ``get_bundle(bundle_id).download_status``
        for progress.
        """
        self._ensure_authenticated()
        assert self._bundles is not None
        logger.info(
            "POST %s/v1/bundles/%s  (download_now=true)",
            self._base_url, bundle_id,
        )
        spec = BundleUpdateSpec(
            bundle_download_spec=BundleDownloadSpec(download_now=True),
        )
        return self._bundles.start_bundle_download_by_id(
            id=bundle_id,
            bundle_update_spec=spec,
        )

    # ---- Releases ----

    def list_releases(
        self,
        *,
        applicable_for_version: str | None = None,
        version_ge: str | None = None,
        include_only_compatible: bool | None = None,
    ) -> PageOfRelease:
        """List VCF releases from the installer's release catalog.

        Useful to see what versions are available to deploy after a depot
        sync completes.
        """
        self._ensure_authenticated()
        assert self._releases is not None
        return self._releases.get_releases(
            applicable_for_version=applicable_for_version,
            version_ge=version_ge,
            include_only_compatible=include_only_compatible,
        )

    # ---- Tasks ----

    def get_task(self, task_id: str) -> Task:
        """Generic task fetch (bundle downloads, depot syncs, etc)."""
        self._ensure_authenticated()
        assert self._tasks is not None
        task = self._tasks.get_task(id=task_id)
        if task is None:
            raise RuntimeError(
                f"Installer /v1/tasks/{task_id} returned an empty response"
            )
        return task


# Re-export the SDK types that downstream managers actually consume.
# Everything else can be imported from ``vmware.vcf_installer.model_client``
# directly on demand.
__all__ = [
    "InstallerClient",
    "SddcSpec",
    "SddcTask",
]
