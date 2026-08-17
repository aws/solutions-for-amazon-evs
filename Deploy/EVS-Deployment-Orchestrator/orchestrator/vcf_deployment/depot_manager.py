# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""High-level operations on the VCF Installer depot + bundle catalog.

Thin orchestration on top of :class:`InstallerClient`. Wraps the typed
responses into clean summary dicts the CLI can print, and adds a few
wait-for-completion loops where polling makes sense (depot sync,
bundle download).

New capability unlocked by the VCF Python SDK — the hand-rolled REST
client didn't cover any of this. Each method maps 1:1 to a CLI action:

  configure-depot     configure_depot(...)
  sync-depot          sync_depot(wait=...)
  list-bundles        list_bundles(...)
  download-bundle     download_bundle(bundle_id, wait=...)
  list-releases       list_releases(...)
"""

import logging
import time
from typing import Any

from installer_client import InstallerClient

logger = logging.getLogger(__name__)


class DepotManager:
    """Orchestrates depot configuration, sync, and bundle operations."""

    def __init__(self, client: InstallerClient) -> None:
        self._client = client

    # ---- Depot config ----

    def configure_depot(
        self,
        *,
        download_token: str,
        username: str | None = None,
        password: str | None = None,
    ) -> dict[str, Any]:
        """Configure the VMware (Broadcom) depot account.

        After this call, ``sync_depot`` should be invoked to refresh the
        installer's catalog.
        """
        logger.info("Configuring VMware depot (token length=%d)", len(download_token))
        self._client.configure_depot(
            download_token=download_token,
            username=username,
            password=password,
        )
        # Read back to surface what the installer stored.
        settings = self._client.get_depot_settings()
        return _summarize_depot_settings(settings)

    def get_depot_settings(self) -> dict[str, Any]:
        settings = self._client.get_depot_settings()
        return _summarize_depot_settings(settings)

    # ---- Depot sync ----

    def sync_depot(
        self,
        wait: bool = False,
        timeout_seconds: int = 1800,
        poll_interval_seconds: int = 30,
    ) -> dict[str, Any]:
        """Trigger a depot metadata sync.

        Args:
            wait: If True, poll until the sync completes (or fails).
            timeout_seconds: Total wait budget when ``wait=True``.
            poll_interval_seconds: Delay between status polls.

        Returns:
            A summary of the final (or current) sync state.
        """
        # Capture the prior sync's timestamp BEFORE triggering a new one:
        # get_depot_sync_info() always reports the LAST sync's status, and
        # nothing ties a poll response to the sync we just POSTed. Without this,
        # the first poll can read a stale terminal status from a prior sync and
        # misclassify it (raise, or return before our sync even starts).
        pre_sync_info = self._client.get_depot_sync_info()
        pre_sync_timestamp = pre_sync_info.last_sync_completion_timestamp

        self._client.sync_depot_metadata()
        info = self._client.get_depot_sync_info()
        logger.info("Depot sync kicked off — status=%s", info.sync_status)

        if not wait:
            return _summarize_depot_sync(info)

        deadline = time.time() + timeout_seconds
        observed_in_progress = False
        while time.time() < deadline:
            info = self._client.get_depot_sync_info()
            status = (info.sync_status or "").upper()
            current_timestamp = info.last_sync_completion_timestamp
            is_stale = (
                not observed_in_progress
                and current_timestamp == pre_sync_timestamp
            )
            logger.info(
                "Depot sync status=%s%s",
                status, " (stale — from before this sync)" if is_stale else "",
            )
            if status in {"IN_PROGRESS", "SYNCING", "RUNNING"}:
                observed_in_progress = True
            # The installer uses version-dependent synonyms for each terminal
            # state; accept them all. A terminal status is trusted only once
            # we've either seen an in-progress state for THIS sync or the
            # completion timestamp advanced past the pre-sync value; until then
            # it's presumed stale (from the prior sync) and polling continues.
            if not is_stale:
                if status in {"SUCCESS", "SUCCESSFUL", "COMPLETED", "SYNCED"}:
                    return _summarize_depot_sync(info)
                if status in {"FAILURE", "FAILED", "SYNC_FAILED"}:
                    raise RuntimeError(
                        f"Depot sync failed: {info.error_message or '<no message>'}"
                    )
            time.sleep(poll_interval_seconds)

        raise TimeoutError(
            f"Depot sync did not complete within {timeout_seconds}s "
            f"(last status={info.sync_status})"
        )

    # ---- Bundles ----

    def list_bundles(
        self,
        *,
        product_type: str | None = None,
        bundle_type: str | None = None,
        is_compliant: bool | None = None,
    ) -> list[dict[str, Any]]:
        """List bundles. Returns a flat list of summary dicts, not the typed
        page object, so the CLI can JSON-serialize without fuss.
        """
        page = self._client.list_bundles(
            product_type=product_type,
            bundle_type=bundle_type,
            is_compliant=is_compliant,
        )
        results = getattr(page, "elements", None) or getattr(page, "results", None) or []
        return [_summarize_bundle(b) for b in results]

    def download_bundle(
        self,
        bundle_id: str,
        wait: bool = False,
        timeout_seconds: int = 7200,
        poll_interval_seconds: int = 60,
    ) -> dict[str, Any]:
        """Trigger a bundle download.

        Args:
            bundle_id: Bundle id to download.
            wait: If True, poll ``get_bundle(id).download_status`` until
                the bundle is marked SUCCESSFUL (or fails).
            timeout_seconds: Total wait budget (default 2 hours).
            poll_interval_seconds: Delay between status polls.

        Returns:
            Summary of the bundle's current download state.
        """
        task = self._client.download_bundle(bundle_id)
        logger.info(
            "Bundle download kicked off: bundle=%s task=%s",
            bundle_id, getattr(task, "id", "<no id>"),
        )

        if not wait:
            return self._describe_bundle_status(bundle_id)

        deadline = time.time() + timeout_seconds
        # Pre-bind status so the TimeoutError below is always valid even
        # if the loop body never runs (e.g. timeout_seconds <= 0).
        status = "unknown"
        while time.time() < deadline:
            summary = self._describe_bundle_status(bundle_id)
            status = (summary.get("downloadStatus") or "").upper()
            logger.info(
                "Bundle %s downloadStatus=%s (%sMB downloaded)",
                bundle_id, status, summary.get("downloadedSizeMB", "?"),
            )
            if status in {"SUCCESSFUL", "COMPLETED"}:
                return summary
            if status in {"FAILED", "CANCELLED"}:
                raise RuntimeError(
                    f"Bundle {bundle_id} download failed: "
                    f"{summary.get('message') or '<no message>'}"
                )
            time.sleep(poll_interval_seconds)

        raise TimeoutError(
            f"Bundle {bundle_id} download did not finish within "
            f"{timeout_seconds}s (last status={status})"
        )

    def _describe_bundle_status(self, bundle_id: str) -> dict[str, Any]:
        bundle = self._client.get_bundle(bundle_id)
        return _summarize_bundle(bundle)

    def get_bundle(self, bundle_id: str) -> dict[str, Any]:
        """Fetch a bundle summary by id. Public alias for the CLI."""
        return self._describe_bundle_status(bundle_id)

    # ---- Releases ----

    def list_releases(
        self,
        *,
        applicable_for_version: str | None = None,
        version_ge: str | None = None,
        include_only_compatible: bool | None = None,
    ) -> list[dict[str, Any]]:
        page = self._client.list_releases(
            applicable_for_version=applicable_for_version,
            version_ge=version_ge,
            include_only_compatible=include_only_compatible,
        )
        results = getattr(page, "elements", None) or getattr(page, "results", None) or []
        return [_summarize_release(r) for r in results]


# ---- Summary helpers ----

def _summarize_depot_settings(settings: Any) -> dict[str, Any]:
    vmware = settings.vmware_account
    offline = settings.offline_account
    dconf = settings.depot_configuration
    return {
        "vmwareAccount": (
            {
                "username": vmware.username,
                "hasDownloadToken": bool(vmware.download_token),
                "status": vmware.status,
                "message": vmware.message,
            }
            if vmware
            else None
        ),
        "offlineAccount": (
            {
                "username": offline.username,
                "status": offline.status,
                "message": offline.message,
            }
            if offline
            else None
        ),
        "depotConfiguration": (
            {
                "isOfflineDepot": dconf.is_offline_depot,
                "hostname": dconf.hostname,
                "port": dconf.port,
                "url": dconf.url,
            }
            if dconf
            else None
        ),
    }


def _summarize_depot_sync(info: Any) -> dict[str, Any]:
    return {
        "syncStatus": info.sync_status,
        "errorMessage": info.error_message,
        "lastSyncCompletionTimestamp": info.last_sync_completion_timestamp,
    }


def _summarize_bundle(b: Any) -> dict[str, Any]:
    # download_status is a plain str, not a struct — getattr(ds, "status")
    # always returns None, so an earlier .status check silently never matched
    # and every wait=True download polled to the 2-hour timeout.
    ds = getattr(b, "download_status", None)
    return {
        "id": b.id,
        "type": b.type,
        "description": b.description,
        "version": b.version,
        "severity": getattr(b, "severity", None),
        "releasedDate": getattr(b, "released_date", None),
        "sizeMB": getattr(b, "size_mb", None),
        "isCompliant": getattr(b, "is_compliant", None),
        "downloadStatus": ds,
        # downloadedSizeMB/message have no equivalent on the real Bundle model
        # (download_status carries no size/message sub-fields) -- kept as None
        # rather than invented fields that would silently never populate.
        "downloadedSizeMB": None,
        "message": None,
    }


def _summarize_release(r: Any) -> dict[str, Any]:
    return {
        "product": r.product,
        "version": r.version,
        "releaseDate": getattr(r, "release_date", None),
        "isApplicable": getattr(r, "is_applicable", None),
        "minInstallerVersion": getattr(r, "min_installer_version", None),
        "description": getattr(r, "description", None),
        "bom": [_summarize_product_version(p) for p in (getattr(r, "bom", None) or [])],
        "patchBundles": [
            _summarize_patch_bundle(p)
            for p in (getattr(r, "patch_bundles", None) or [])
        ],
        "sku": getattr(r, "sku", None),
    }


def _summarize_product_version(p: Any) -> dict[str, Any]:
    return {
        "name": getattr(p, "name", None),
        "version": getattr(p, "version", None),
        "publicName": getattr(p, "public_name", None),
        "changeId": getattr(p, "change_id", None),
        "releaseURL": getattr(p, "release_url", None),
        "automatedInstall": getattr(p, "automated_install", None),
        "lifecycleManagedBy": getattr(p, "lifecycle_managed_by", None),
        "additionalMetadata": getattr(p, "additional_metadata", None),
    }


def _summarize_patch_bundle(p: Any) -> dict[str, Any]:
    # Best-effort dump — patchBundles carry bundle metadata whose shape
    # we haven't pinned down yet. Use to_dict if it's a VapiStruct.
    if hasattr(p, "to_dict"):
        try:
            return p.to_dict()
        except Exception:  # pragma: no cover — diagnostic helper
            pass
    return {"_raw": repr(p)}
