# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Higher-level orchestration of a VCF bringup workflow.

Loads the bringup spec JSON from disk, resolves any
``__SECRET:<role>__`` placeholders against AWS Secrets Manager,
deserializes it into a typed ``SddcSpec`` via the VCF SDK's own
converter, and hands the typed object to
``InstallerClient.start_bringup``. Also wraps the status-check call and
logs useful summary fields.

SSL thumbprint fill-in
----------------------

The VCF 9.0.2 installer's spec validator requires each ``hostSpecs[i].
sslThumbprint`` to be present even when ``skipEsxThumbprintValidation`` is
set. We fetch those live from each ESXi host at POST time because the
Phase 2 workstation usually can't resolve the private DNS for the hosts —
Phase 3 runs closer to the VPC (often on the Installer VM itself).

Secret resolution
-----------------

Phase 2's spec builder writes ``__SECRET:<role>__`` placeholders for
every VCF appliance password (vCenter, NSX, SDDC Manager, Operations,
Fleet on 9.0). The on-disk JSON has no real passwords. ``_resolve_secrets``
fetches the actual values from
``evs-<env_id>_<role>`` in AWS Secrets Manager just before
serializing the typed object back out to the wire.

SDDC Manager local user password
--------------------------------

The VCF Installer appliance is transformed in-place into the SDDC
Manager during bringup, so ``sddcManagerSpec.useExistingDeployment`` is
``True``. With that flag set, the installer uses
``localUserPassword`` to authenticate against the **existing**
appliance — meaning the value we send must match the operator-set
``admin@local`` password baked into the OVA at deploy time. We get
that value from the ``InstallerClient`` (it's the same password we
authenticate every other installer call with) and overlay it onto the
spec right before POST, replacing whatever ``__SECRET:sddcManagerLocal__``
resolved to. The Secrets Manager value for ``sddcManagerLocal`` is
unused on the wire today; future password-rotation flows that re-set
the local user can use it then.
"""

import json
import logging
from pathlib import Path
from typing import Any

from src.host_thumbprints import fetch_ssl_thumbprint
from src.installer_client import InstallerClient, SddcSpec, SddcTask
from src.secret_resolver import SecretResolver
from src.sdk_serde import json_to_typed, typed_to_json

logger = logging.getLogger(__name__)


class BringupManager:
    """Wraps an ``InstallerClient`` with workflow-level concerns.

    Args:
        client: An initialized ``InstallerClient``.
        spec_path: Path to the bringup spec JSON file on disk.
        sm_client: A boto3 ``secretsmanager`` client. Used to resolve
            ``__SECRET:<role>__`` placeholders in the spec at POST time.
    """

    def __init__(
        self,
        client: InstallerClient,
        spec_path: str | Path,
        sm_client: Any | None = None,
    ) -> None:
        self._client = client
        self._spec_path = Path(spec_path)
        self._sm_client = sm_client

    def load_spec(self, resolve_secrets: bool = True) -> SddcSpec:
        """Read the bringup spec JSON from disk as a typed ``SddcSpec``.

        Raises:
            FileNotFoundError: If the spec file does not exist.
            secret_resolver.MissingSecretsError: If any required secret
                is absent from Secrets Manager.
        """
        if not self._spec_path.exists():
            raise FileNotFoundError(
                f"Bringup spec not found: {self._spec_path}. "
                f"Run pre-evs-sync-config and post-evs-sync-config first."
            )
        json_text = self._spec_path.read_text()
        # Parse to a raw dict so we can resolve placeholders and pull
        # out the project-private ``__env__`` / ``__region__`` fields
        # before handing the cleaned-up JSON to the SDK's typed
        # deserializer (which would complain about either one not
        # being part of the SddcSpec schema).
        spec_dict = json.loads(json_text)
        env_id = spec_dict.pop("__env__", None)
        spec_dict.pop("__region__", None)
        if resolve_secrets and self._sm_client is not None and env_id:
            resolver = SecretResolver(self._sm_client, env_id)
            resolver.resolve_in_place(spec_dict)
        elif resolve_secrets and "__SECRET:" in json_text:
            logger.warning(
                "Spec contains __SECRET placeholders but no Secrets Manager "
                "client is wired up. Real bringup will fail with the "
                "placeholders on the wire."
            )

        return json_to_typed(json.dumps(spec_dict), SddcSpec)

    def start(self, dry_run: bool = False, wait: bool = False) -> dict[str, Any]:
        """Kick off the bringup workflow.

        Args:
            dry_run: If True, print the spec that would be sent without
                posting to the installer.
            wait: If True, block after starting and poll the workflow
                state every 10 minutes until it reaches a terminal
                status (success or failure). Otherwise return the
                initial response immediately and let the caller poll
                with ``check_bringup``.

        Returns:
            A dict summary of the installer response (id, status, name).
            Empty dict for a dry run. When ``wait`` is True, the summary
            reflects the final terminal state.
        """
        spec = self.load_spec(resolve_secrets=not dry_run)
        self._populate_host_thumbprints(spec)
        if not dry_run:
            self._overlay_existing_local_user_password(spec)

        if dry_run:
            logger.info(
                "DRY RUN — would POST to installer /v1/sddcs with:"
            )
            print(typed_to_json(spec, indent=2))
            return {}

        logger.info("Starting VCF bringup workflow...")
        self._populate_bundle_versions(spec)
        task = self._client.start_bringup(spec)
        summary = _summarize_task(task)
        logger.info(
            "Bringup workflow started: id=%s status=%s",
            summary.get("id"), summary.get("status"),
        )

        if not wait:
            return summary
        if not summary.get("id"):
            logger.warning(
                "Wait requested but the installer didn't return a workflow "
                "id; can't poll. Returning the initial response."
            )
            return summary

        return self._wait_for_terminal(summary["id"])

    def _populate_bundle_versions(self, spec: SddcSpec) -> None:
        """Fill in bundle versions on the typed spec from the installer catalog.

        VCF 9.1 components (VSP, VIDB) need their ``version`` field set
        to the build version of the downloaded INSTALL bundle (e.g.
        ``9.1.0-25370367``). This queries ``/v1/bundles`` once and sets
        the version on any spec section that's present but has no version.
        """
        needs_version = []
        if spec.vsp_cluster_spec and not spec.vsp_cluster_spec.version:
            needs_version.append(("VSP", spec.vsp_cluster_spec))
        if spec.vidb_spec and not spec.vidb_spec.version:
            needs_version.append(("VIDB", spec.vidb_spec))

        if not needs_version:
            return

        try:
            catalog = self._client.raw_get("/v1/bundles")
        except Exception as e:
            logger.warning(
                "Couldn't fetch bundle catalog for version resolution: %s", e
            )
            return

        bundles = (catalog or {}).get("elements") or []
        type_to_version: dict[str, str] = {}
        for bundle in bundles:
            status = (bundle.get("downloadStatus") or "").upper()
            if status not in ("SUCCESSFUL", "COMPLETED"):
                continue
            for comp in bundle.get("components") or []:
                if (comp.get("imageType") or "").upper() != "INSTALL":
                    continue
                ctype = comp.get("type", "")
                version = comp.get("toVersion") or bundle.get("version", "")
                if ctype and version:
                    existing = type_to_version.get(ctype, "")
                    if not existing or version > existing:
                        type_to_version[ctype] = version

        for component_type, spec_obj in needs_version:
            version = type_to_version.get(component_type)
            if version:
                spec_obj.version = version
                logger.info(
                    "Resolved %s bundle version -> %s", component_type, version
                )
            else:
                logger.warning(
                    "No downloaded INSTALL bundle found for %s; "
                    "version left empty", component_type,
                )

    def _overlay_existing_local_user_password(self, spec: SddcSpec) -> None:
        """Replace ``sddcManagerSpec.localUserPassword`` with the
        installer's actual ``admin@local`` password.

        The bringup spec has ``useExistingDeployment=True`` for the
        SDDC Manager (the VCF Installer appliance is transformed into
        the SDDC Manager rather than a fresh appliance being deployed).
        With that flag set, the installer treats ``localUserPassword``
        as a credential to authenticate against the **existing**
        appliance — so the value must match the operator-set
        ``admin@local`` password from OVA deploy time.

        The Phase 2 placeholder ``__SECRET:sddcManagerLocal__`` resolves
        to a Phase-2-generated value that doesn't match what's actually
        on the installer. Overlay the InstallerClient's password (which
        is what we authenticate every other installer call with) so the
        spec carries the correct credential. The Secrets Manager value
        for ``sddcManagerLocal`` is unused on the wire today; future
        password-rotation flows that re-set the local user can use it
        then.
        """
        if spec.sddc_manager_spec is None:
            return
        installer_password = getattr(self._client, "_password", None)
        if not installer_password:
            logger.warning(
                "InstallerClient has no password set; can't overlay "
                "local_user_password. The bringup will likely reject "
                "with QUICK_START_VALIDATION_FAILED."
            )
            return
        spec.sddc_manager_spec.local_user_password = installer_password
        logger.info(
            "Set sddcManagerSpec.localUserPassword to the installer's "
            "admin@local password (useExistingDeployment=True path)"
        )

    def _wait_for_terminal(self, workflow_id: str) -> dict[str, Any]:
        """Poll a bringup workflow every 10 minutes until it terminates.

        VCF bringup typically takes 2-4 hours; the 10-minute cadence keeps
        the log volume reasonable while still catching failures within a
        useful window.

        Terminal statuses come from the installer's ``SddcTask.status``
        enum. We treat anything containing ``SUCCESS`` as a clean exit
        and anything containing ``FAIL`` / ``CANCEL`` as a failure;
        unknown values keep polling so we don't accidentally early-exit
        on a status we haven't seen.

        Appliance transition handling
        -----------------------------

        At the very end of a successful bringup, the VCF Installer
        appliance is transformed in-place into the SDDC Manager. The
        ``/v1/sddcs/{id}`` endpoint stays available but starts speaking
        the SDDC Manager's error JSON shape instead of the VCF
        Installer's typed ``SddcTask`` schema, so the SDK raises
        ``UnresolvedError: {}`` (it got valid JSON back, just not
        decodable to the type it expected). The original workflow id
        is essentially lost — there's no SDDC-Manager-side equivalent.

        On the first ``UnresolvedError`` we probe ``/v1/system`` (an
        unauthenticated endpoint that returns a stable error JSON) to
        see if the appliance has transitioned. If yes, we synthesize
        a ``COMPLETED_WITH_SUCCESS`` summary and return — the one-click
        SDDC + NSX deployment proceeds to step 3 (deploy-edge-cluster).
        If the probe doesn't
        match yet, we wait one more interval and re-probe; only after
        two consecutive non-matching probes do we surface the original
        ``UnresolvedError``.

        Other failure modes (network blips, raw RuntimeError) keep the
        original "log + retry" behavior.
        """
        import time

        poll_interval_seconds = 600  # 10 minutes
        # No hard timeout — bringup can run 2-4 hours and is the longest
        # blocking operation in the whole pipeline. The operator can
        # ctrl-C and resume polling with ``check-bringup`` if needed.

        last_summary: dict[str, Any] = {}
        unresolved_streak = 0
        while True:
            try:
                last_summary = self.check(workflow_id)
                unresolved_streak = 0  # any clean response resets the streak
            except Exception as e:  # noqa: BLE001
                if _looks_like_appliance_transition_error(e):
                    # Could be a stale JWT (installer still running) or a
                    # real appliance transition (installer gone, SDDC Manager
                    # up). Try refreshing the token first — if the installer
                    # is alive it will succeed and we can keep polling.
                    if self._client.refresh_token():
                        logger.info(
                            "Token refreshed — installer is still alive. "
                            "Retrying poll immediately.",
                        )
                        unresolved_streak = 0
                        continue

                    # Token refresh failed — installer is gone. Check if
                    # SDDC Manager is up (the appliance transition).
                    unresolved_streak += 1
                    max_transition_attempts = 6
                    logger.info(
                        "Token refresh failed and poll %s couldn't decode "
                        "response (streak=%d/%d); probing /v1/system for "
                        "SDDC Manager transition",
                        workflow_id, unresolved_streak, max_transition_attempts,
                    )
                    if self._appliance_is_sddc_manager():
                        logger.info(
                            "Bringup workflow %s — SDDC Manager is up at %s. "
                            "The VCF Installer appliance has transitioned to "
                            "SDDC Manager (final stage of bringup); the workflow "
                            "id no longer resolves on the new API surface. "
                            "Treating as COMPLETED_WITH_SUCCESS.",
                            workflow_id, self._client._base_url,
                        )
                        return {
                            "id": workflow_id,
                            "status": "COMPLETED_WITH_SUCCESS",
                            "name": last_summary.get("name"),
                            "deploymentType": last_summary.get("deploymentType"),
                            "vcfInstanceName": last_summary.get("vcfInstanceName"),
                            "creationTimestamp": last_summary.get("creationTimestamp"),
                        }
                    if unresolved_streak >= max_transition_attempts:
                        logger.error(
                            "Bringup poll %s failed with appliance-transition "
                            "error %d times in a row, but /v1/system probes don't "
                            "match the SDDC Manager shape. Surfacing the "
                            "underlying error.",
                            workflow_id, max_transition_attempts,
                        )
                        raise
                    logger.info(
                        "Probe inconclusive; retrying in %d minutes",
                        poll_interval_seconds // 60,
                    )
                    time.sleep(poll_interval_seconds)
                    continue
                # Network blips, installer restarts, etc. Log and keep
                # polling — partial bringup state isn't useful, but a
                # dropped poll shouldn't fail the whole wait.
                logger.warning(
                    "Poll failed (%s); will retry in %ds",
                    e, poll_interval_seconds,
                )
                time.sleep(poll_interval_seconds)
                continue

            status = (last_summary.get("status") or "").upper()
            if "SUCCESS" in status:
                logger.info("Bringup workflow %s reached %s — done.",
                            workflow_id, status)
                return last_summary
            if "FAIL" in status or "CANCEL" in status:
                logger.error(
                    "Bringup workflow %s ended in %s. Check the installer "
                    "UI / domainmanager.log for the failing task.",
                    workflow_id, status,
                )
                return last_summary

            logger.info(
                "Bringup workflow %s status=%s; retrying in %d minutes",
                workflow_id, status or "unknown", poll_interval_seconds // 60,
            )
            time.sleep(poll_interval_seconds)

    def _appliance_is_sddc_manager(self) -> bool:
        """Probe ``/v1/system`` (no auth) to detect the SDDC Manager shape.

        SDDC Manager returns a stable JSON error body for an
        unauthenticated GET:

            {"errorCode":"Unauthorized",
             "message":"Authorization Header is missing or not in correct format"}

        VCF Installer returns either a different shape or a 4xx with no
        JSON body. Checking both ``errorCode`` and the
        ``Authorization Header`` substring keeps us robust to small
        message tweaks across VCF point releases.

        Returns ``True`` if the probe matches, ``False`` on anything
        else (including network errors — those keep the poll loop's
        retry behavior intact).
        """
        url = f"{self._client._base_url}/v1/system"
        try:
            response = self._client._session.get(url, timeout=10)
        except Exception as e:  # noqa: BLE001
            logger.info("Probe %s failed (%s); not declaring transition", url, e)
            return False
        if response.status_code != 401:
            logger.info(
                "Probe %s returned %d (expected 401); not declaring transition",
                url, response.status_code,
            )
            return False
        try:
            body = response.json()
        except ValueError:
            logger.info(
                "Probe %s returned 401 but body wasn't JSON; not declaring transition",
                url,
            )
            return False
        error_code = body.get("errorCode") if isinstance(body, dict) else None
        message = body.get("message") if isinstance(body, dict) else None
        if error_code == "Unauthorized" and isinstance(message, str) \
                and ("Authorization Header" in message or "JWT" in message):
            logger.info(
                "Probe %s matches SDDC Manager shape (errorCode=%s)",
                url, error_code,
            )
            return True
        logger.info(
            "Probe %s body didn't match SDDC Manager shape (got %r)",
            url, body,
        )
        return False

    def _populate_host_thumbprints(self, spec: SddcSpec) -> None:
        """Fill in ``hostSpecs[i].sslThumbprint`` for any host missing one.

        The installer's spec validator requires this field even when
        ``skipEsxThumbprintValidation`` is set on the top-level spec.
        Hosts that already have a thumbprint (pre-populated via config)
        are left alone. If a host is unreachable (e.g. running through
        an SSH tunnel without ESXi port forwarding), a placeholder is
        used and skipEsxThumbprintValidation must be True.
        """
        host_specs = spec.host_specs or []
        for host in host_specs:
            if host.ssl_thumbprint:
                continue
            if not host.hostname:
                continue
            logger.info("Fetching SSL thumbprint from %s", host.hostname)
            try:
                thumbprint = fetch_ssl_thumbprint(host.hostname)
                host.ssl_thumbprint = thumbprint
                logger.info("  %s -> %s", host.hostname, thumbprint)
            except (OSError, RuntimeError) as e:
                placeholder = "00:" * 31 + "00"
                host.ssl_thumbprint = placeholder
                logger.warning(
                    "  %s unreachable (%s); using placeholder thumbprint "
                    "(skipEsxThumbprintValidation must be True)",
                    host.hostname, e,
                )

    def check(self, workflow_id: str) -> dict[str, Any]:
        """Fetch the current state of a running workflow.

        Args:
            workflow_id: The workflow id returned from ``start()``.

        Returns:
            A dict summary with id / status / name / deploymentType fields.
        """
        logger.info("Checking bringup workflow: %s", workflow_id)
        task = self._client.get_bringup(workflow_id)
        summary = _summarize_task(task)
        logger.info(
            "Workflow %s — status=%s", workflow_id, summary.get("status"),
        )
        return summary


def _summarize_task(task: SddcTask) -> dict[str, Any]:
    """Pull the fields we actually log/print out of a typed ``SddcTask``.

    The task object has verbose nested sub-task lists that we don't want to
    dump by default — callers pass the id to ``check`` for details when
    they need them.
    """
    return {
        "id": task.id,
        "name": task.name,
        "deploymentType": task.deployment_type,
        "vcfInstanceName": task.vcf_instance_name,
        "status": task.status,
        "creationTimestamp": task.creation_timestamp,
    }


def _looks_like_appliance_transition_error(exc: Exception) -> bool:
    """Heuristic: does this exception look like the VCF Installer →
    SDDC Manager appliance transition?

    The SDK raises ``vmware.vapi.bindings.error.UnresolvedError: {}``
    when ``/v1/sddcs/{id}`` returns valid JSON whose schema doesn't
    match the typed ``SddcTask`` it expected — exactly what happens
    once the appliance has transitioned and the SDDC Manager API
    starts speaking its own error JSON shape on the same path.

    We match on class name rather than importing
    ``UnresolvedError`` so the dependency on vapi internals stays
    soft. Anything else (RuntimeError, ConnectionError, generic
    Exception) keeps the original "log and retry" behavior in
    ``_wait_for_terminal``.
    """
    cls_name = type(exc).__name__
    if cls_name == "UnresolvedError":
        return True
    # Belt-and-suspenders: also look for the specific text fragment in
    # case future SDK versions wrap or rename the exception.
    return "Unable to convert unexpected vAPI error" in str(exc)
