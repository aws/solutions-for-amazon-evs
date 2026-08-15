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

The 9.0.2 spec validator requires each ``hostSpecs[i].sslThumbprint`` even
when ``skipEsxThumbprintValidation`` is set. Fetched live from each ESXi host
at POST time because the Phase 2 workstation usually can't resolve the hosts'
private DNS; Phase 3 runs closer to the VPC.

Secret resolution
-----------------

Phase 2's spec builder writes ``__SECRET:<role>__`` placeholders for every VCF
appliance password, so the on-disk JSON has no real passwords. Actual values
are fetched from ``evs-<env_id>_<role>`` in Secrets Manager just before
serializing back to the wire.

SDDC Manager local user password
--------------------------------

The Installer appliance is transformed in-place into SDDC Manager, so
``sddcManagerSpec.useExistingDeployment`` is ``True`` and the installer uses
``localUserPassword`` to authenticate against the existing appliance -- the
value must match the operator-set ``admin@local`` from OVA deploy time, so we
overlay the InstallerClient's password right before POST. The Secrets Manager
``sddcManagerLocal`` value is unused on the wire today, reserved for future
password-rotation flows.
"""

import json
import logging
import time
from pathlib import Path
from typing import Any

from host_thumbprints import fetch_ssl_thumbprint
from installer_client import InstallerClient, SddcSpec, SddcTask
from secret_resolver import SecretResolver
from sdk_serde import json_to_typed, typed_to_json

logger = logging.getLogger(__name__)

# Exact-match terminal states for SddcTask.status -- NOT substring-matched:
# ROLLBACK_SUCCESS reports a FAILED bringup that auto-rolled back (a failure
# outcome despite "SUCCESS" in the string), so callers must use these sets.
_BRINGUP_SUCCESS_STATUSES = frozenset({"COMPLETED_WITH_SUCCESS"})
_BRINGUP_FAILURE_STATUSES = frozenset({
    "COMPLETED_WITH_FAILURE", "ROLLBACK_SUCCESS", "CANCELLED", "CANCELED",
})


def is_bringup_success(status: str) -> bool:
    """True if ``status`` (case-insensitive) is a successful terminal state."""
    return (status or "").upper() in _BRINGUP_SUCCESS_STATUSES


def is_bringup_failure(status: str) -> bool:
    """True if ``status`` (case-insensitive) is a failed terminal state.

    Includes ROLLBACK_SUCCESS — see module note above.
    """
    return (status or "").upper() in _BRINGUP_FAILURE_STATUSES

# Maps each password-bearing role (matching vcf_password_provisioner) to the
# exact "Validation for X failed" prefix the VCF Installer emits in
# SddcSubTask.errors[].message when that component's password fails validation.
# Wording is the Installer API's (confirmed against a VCF 5.x fixture and the
# VCF 9 SDK's identical Error/nested_errors shape). VCF
# Operations roles have no confirmed prefix -- extend, don't assume this list
# is exhaustive, if one surfaces.
COMPONENT_VALIDATION_ERROR_PREFIXES: dict[str, str] = {
    "vcenterSso": "Validation for PSC Admin User failed",
    "vcenterRoot": "Validation for vCenter Root User failed",
    "nsxRoot": "Validation for NSX Root User failed",
    "nsxAdmin": "Validation for NSX Admin User failed",
    "nsxAudit": "Validation for NSX Audit User failed",
    "sddcManagerRoot": "Validation for SDDC Manager Root User failed",
    "sddcManagerSsh": "Validation for SDDC Manager Second User failed",
    "sddcManagerLocal": "Validation for SDDC Local User failed",
}

# Marks a validation failure as password-related (vs a network/DNS/licensing
# failure reported under the same "Validation for X failed" family). Loose
# match on the fixture wording ("Password failed simplicity test...") to
# survive minor rewording across VCF releases.
_PASSWORD_FAILURE_MARKER = "password"


def _load_regenerate_role_password():
    """Load ``vcf_password_provisioner.regenerate_role_password`` by explicit
    file path, bypassing ``sys.path``/package resolution entirely.

    Phase 3's ``sys.path`` excludes ``PHASE2_SRC`` (deploy_orchestrator's
    ``_activate_phase3()``), so a normal import raises ``ModuleNotFoundError``
    at runtime. The regeneration logic belongs with Phase 2's password code
    (it overwrites the secrets ``ensure_vcf_passwords`` creates), not
    duplicated into Phase 3, so load it directly from disk.
    """
    import importlib.util

    module_path = Path(__file__).resolve().parent.parent / "evs_environment" / "vcf_password_provisioner.py"
    if not module_path.exists():
        raise RuntimeError(
            f"Cannot regenerate passwords: vcf_password_provisioner.py not "
            f"found at expected path {module_path} (Phase 2/3 directory "
            f"layout may have changed)."
        )
    spec = importlib.util.spec_from_file_location("vcf_password_provisioner", module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.regenerate_role_password


def _iter_error_texts(error: Any, _depth: int = 0):
    """Yield one combined text per ``Error`` node, recursing into nested errors.

    The VCF 9 SDK ``Error`` carries text in ``message``, ``arguments``, and
    ``nested_errors``; real failures can populate ``arguments``/
    ``nested_errors`` with no flat ``message``, so matching ``message`` alone
    would miss them. Per node, ``message`` + joined ``arguments`` are combined
    into one candidate (so a prefix in one and a password detail in the other
    still match together); nested errors yield their own. Recursion is
    depth-bounded against pathological/cyclic structures.
    """
    if error is None or _depth > 5:
        return
    parts = []
    message = getattr(error, "message", None)
    if message:
        parts.append(str(message))
    arguments = getattr(error, "arguments", None) or []
    joined_arguments = " ".join(str(a) for a in arguments if a)
    if joined_arguments:
        parts.append(joined_arguments)
    combined = " ".join(parts)
    if combined:
        yield combined
    for nested in getattr(error, "nested_errors", None) or []:
        yield from _iter_error_texts(nested, _depth + 1)


def _extract_component_validation_errors(task: SddcTask) -> dict[str, list[str]]:
    """Find which component(s)' passwords failed validation in a bringup task.

    Walks ``task.sddc_sub_tasks[].errors[]`` (recursing into ``nested_errors``)
    and matches the combined message+arguments text (see ``_iter_error_texts``)
    against the known "Validation for X failed" prefixes, keeping only texts
    that also mention "password" -- so a non-password failure under the same
    sub-task (e.g. network reachability) doesn't trigger regeneration.

    Args:
        task: The SddcTask from ``InstallerClient.get_bringup``/
            ``start_bringup``.

    Returns:
        ``{role: [matching error texts]}`` for every role whose
        password validation failed. Empty dict if none did (including
        the case where the task failed for an unrelated reason).
    """
    found: dict[str, list[str]] = {}
    sub_tasks = getattr(task, "sddc_sub_tasks", None) or []

    for sub_task in sub_tasks:
        errors = getattr(sub_task, "errors", None) or []
        for error in errors:
            for text in _iter_error_texts(error):
                logger.debug(
                    "Considering bringup error text for password-validation "
                    "match: %r", text[:500],
                )
                if _PASSWORD_FAILURE_MARKER not in text.lower():
                    continue
                for role, prefix in COMPONENT_VALIDATION_ERROR_PREFIXES.items():
                    if prefix in text:
                        found.setdefault(role, []).append(text)

    return found


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
        # Populated by load_spec() from the spec's __env__ field; used later
        # by _regenerate_and_retry, which runs after load_spec's own local
        # env_id has gone out of scope.
        self._env_id: str | None = None

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
                f"Run the post-evs-sync-config stage first (it writes the spec)."
            )
        json_text = self._spec_path.read_text()
        # Parse to a raw dict so we can resolve placeholders and strip the
        # project-private ``__env__`` / ``__region__`` fields before the SDK's
        # typed deserializer (which rejects fields outside the SddcSpec schema).
        spec_dict = json.loads(json_text)
        env_id = spec_dict.pop("__env__", None)
        spec_dict.pop("__region__", None)
        if env_id:
            # Stashed for the regenerate-and-retry path (_regenerate_and_retry),
            # which runs after load_spec's local env_id would go out of scope.
            self._env_id = env_id
        if resolve_secrets and self._sm_client is not None and env_id:
            resolver = SecretResolver(self._sm_client, env_id)
            resolver.resolve_in_place(spec_dict)
            # Fail fast if any placeholders survived resolution (e.g. due to
            # regex mismatch with hostnames containing special characters).
            residual = json.dumps(spec_dict)
            if "__SECRET:" in residual:
                import re
                unresolved = re.findall(r"__SECRET:[^_]+__", residual)
                raise RuntimeError(
                    f"Secret resolution left {len(unresolved)} unresolved "
                    f"placeholder(s) in the spec. This usually means a hostname "
                    f"contains characters the resolver doesn't handle. "
                    f"Unresolved: {unresolved[:5]}"
                )
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

        existing = self._find_existing_bringup_task()
        if existing is not None:
            summary = _summarize_task(existing)
            status = (summary.get("status") or "").upper()
            logger.info(
                "Bringup workflow %s already exists (status=%s) — not "
                "starting a new one. This happens on --resume after a "
                "crash/restart post-POST; re-POSTing against a partially "
                "or fully brought-up SDDC would be unsafe.",
                summary.get("id"), status,
            )
            if not wait:
                return summary
            if is_bringup_success(status) or is_bringup_failure(status):
                return summary  # already terminal, nothing to wait for
            return self._wait_for_terminal(summary["id"])

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

        def _version_key(value: str) -> tuple[int, ...]:
            # Version-aware comparison key: split on '.' and '-' and
            # compare numeric segments as ints ('9.10.0' > '9.9.0').
            # Non-numeric segments fall back to 0 rather than crashing.
            return tuple(
                int(seg) if seg.isdigit() else 0
                for seg in value.replace("-", ".").split(".")
            )

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
                    if not existing or _version_key(version) > _version_key(
                        existing
                    ):
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

    def _regenerate_and_retry(self, workflow_id: str, roles: list[str]) -> dict[str, Any]:
        """Regenerate the given roles' passwords and retry the SAME bringup workflow.

        Called when ``_wait_for_terminal`` sees the bringup fail on a component
        password the VCF Installer's validator rejected. The ~12-16 passwords
        are generated up front from a best-effort rule set, but the only REAL
        validation happens here, 3+ hours in -- so regenerate and retry rather
        than surface a failure that's usually fixable without a human.

        Steps:
          1. Regenerate each failed role's secret
             (``regenerate_role_password`` always overwrites, unlike
             provision-time ``ensure_vcf_passwords``).
          2. Reload the spec from disk. ``load_spec`` builds a fresh
             ``SecretResolver``, so it picks up the regenerated values with no
             manual field mutation (the field->role mapping lives in the spec's
             ``__SECRET:<role>__`` placeholders).
          3. Re-apply the overlays ``start()`` applies before a normal POST
             (thumbprints, existing-local-user-password) -- retry_sddc sends a
             full replacement spec, so a skipped overlay silently regresses.
          4. Call ``retry_sddc`` (NOT ``start_bringup``) -- resumes the same
             workflow id instead of starting a fresh one against an appliance
             with partial bringup state.

        Args:
            workflow_id: The failed workflow's id.
            roles: Which roles to regenerate (from
                ``_extract_component_validation_errors``'s keys).

        Returns:
            A summary dict for the retried task (same shape as
            ``_summarize_task``), reflecting the installer's immediate
            post-retry status -- NOT polled to a new terminal state here; the
            caller resumes its own polling loop.
        """
        if not self._env_id:
            raise RuntimeError(
                "Cannot regenerate passwords: no environment id captured "
                "from the bringup spec (load_spec must run at least once "
                "before a retry)."
            )
        if self._sm_client is None:
            raise RuntimeError(
                "Cannot regenerate passwords: no Secrets Manager client "
                "configured for this BringupManager."
            )

        logger.warning(
            "Bringup workflow %s failed password validation for role(s): %s. "
            "Regenerating and retrying (not starting a new workflow).",
            workflow_id, roles,
        )
        regenerate_role_password = _load_regenerate_role_password()
        for role in roles:
            regenerate_role_password(self._sm_client, self._env_id, role)

        # Reload with the fresh secrets and re-apply the same overlays a
        # normal start() would — retry_sddc sends a full replacement spec.
        spec = self.load_spec(resolve_secrets=True)
        self._populate_host_thumbprints(spec)
        self._overlay_existing_local_user_password(spec)
        self._populate_bundle_versions(spec)

        task = self._client.retry_sddc(workflow_id, spec=spec)
        summary = _summarize_task(task)
        logger.info(
            "Retry submitted for workflow %s (status=%s) after regenerating: %s",
            workflow_id, summary.get("status"), roles,
        )
        return summary

    def _find_existing_bringup_task(self) -> Any | None:
        """Return the most recent bringup task known to this installer, if any.

        Used by ``start()`` to avoid re-POSTing: the installer runs only one
        SDDC bringup per appliance, so any task returned IS the one start()
        would duplicate. None only on a fresh appliance. Transient listing
        failures retry with backoff; a persistent one raises rather than risk
        a duplicate POST.
        """
        last_err = None
        tasks = None
        for attempt in range(3):
            try:
                tasks = self._client.list_bringup_tasks()
                break
            except Exception as e:  # noqa: BLE001
                last_err = e
                logger.warning(
                    "Could not list existing bringup tasks (attempt %d/3): %s",
                    attempt + 1, e,
                )
                if attempt < 2:
                    time.sleep(10 * (attempt + 1))
        else:
            # Persistent failure: can't conclude no bringup exists. Falling
            # through to a fresh POST could duplicate an in-progress bringup and
            # corrupt a multi-hour deployment, so fail loud instead of guessing.
            raise RuntimeError(
                "Could not list existing bringup tasks after 3 attempts "
                f"({last_err}); refusing to start a new bringup because a "
                "duplicate POST against an in-progress SDDC would be unsafe. "
                "Resolve VCF Installer connectivity and retry."
            )
        if not tasks:
            return None
        # Tasks don't come with a documented sort order; pick the one with
        # the latest creation_timestamp as "the" bringup for this appliance.
        return max(tasks, key=lambda t: getattr(t, "creation_timestamp", "") or "")

    def _wait_for_terminal(self, workflow_id: str) -> dict[str, Any]:
        """Poll a bringup workflow every 10 minutes until it terminates.

        Bringup runs 2-4.5 hours; the 10-minute cadence keeps log volume sane.

        Classification is by EXACT match against the success/failure sets,
        NOT substring: a naive ``"SUCCESS" in status`` would match
        ``ROLLBACK_SUCCESS`` (a FAILED bringup that auto-rolled back) and
        report a rolled-back SDDC as clean success, marching the pipeline into
        edge-cluster deploy against a nonexistent SDDC. Unknown values keep
        polling so we don't early-exit on a status we haven't seen.

        Appliance transition handling
        -----------------------------

        At the end of a successful bringup the Installer appliance becomes SDDC
        Manager in place: ``/v1/sddcs/{id}`` stays up but speaks SDDC Manager's
        error JSON, so the SDK raises ``UnresolvedError`` and the workflow id is
        lost. On that error we probe ``/v1/system`` (unauthenticated, stable
        error JSON); if it matches, synthesize ``COMPLETED_WITH_SUCCESS`` and
        return. Otherwise wait one interval and re-probe, surfacing the original
        error only after two consecutive non-matching probes. Other failures
        (network blips, RuntimeError) keep the "log + retry" behavior.
        """
        poll_interval_seconds = 600  # 10 minutes
        # No hard timeout — bringup can run 2-4.5 hours and is the longest
        # blocking operation in the whole pipeline. The operator can
        # ctrl-C and resume polling with ``check-bringup`` if needed.

        # Bounded regenerate-and-retry: a password-validation failure gets ONE
        # automatic attempt total per wait. Unbounded retries would mask a real
        # non-password root cause behind an infinite regenerate/retry loop.
        max_password_retries = 1
        password_retries_used = 0

        last_summary: dict[str, Any] = {}
        unresolved_streak = 0
        # Bounds the "token refresh succeeded, retry immediately" branch
        # below — see its comment for why this is needed to avoid a
        # zero-delay infinite hot loop against the installer.
        refresh_succeeded_streak = 0
        max_refresh_succeeded_retries = 2
        while True:
            try:
                last_summary = self.check(workflow_id)
                unresolved_streak = 0  # any clean response resets the streak
                refresh_succeeded_streak = 0
            except Exception as e:  # noqa: BLE001
                if _looks_like_appliance_transition_error(e):
                    # Could be a stale JWT (installer still running) or a
                    # real appliance transition (installer gone, SDDC Manager
                    # up). Try refreshing the token first — if the installer
                    # is alive it will succeed and we can keep polling.
                    if self._client.refresh_token():
                        refresh_succeeded_streak += 1
                        if refresh_succeeded_streak > max_refresh_succeeded_retries:
                            # Token keeps refreshing but check() keeps failing
                            # the same way -- a persistent server-side error, not
                            # a stale-JWT blip. Without this bound the branch
                            # spins at zero delay forever; fall through to the
                            # paced transition-probe path, which surfaces the
                            # error via max_transition_attempts.
                            logger.warning(
                                "Token refresh has succeeded %d times in a "
                                "row but polling workflow %s still can't "
                                "decode a response; treating as a "
                                "persistent error instead of a transient "
                                "stale-JWT blip.",
                                refresh_succeeded_streak, workflow_id,
                            )
                        else:
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
            if is_bringup_success(status):
                logger.info("Bringup workflow %s reached %s — done.",
                            workflow_id, status)
                return last_summary
            if is_bringup_failure(status):
                # Before giving up, check whether this is a self-healable
                # password-validation rejection: regenerate the rejected role(s)
                # and retry the SAME workflow once before surfacing the failure.
                if password_retries_used < max_password_retries:
                    try:
                        full_task = self._client.get_bringup(workflow_id)
                        failed_roles = sorted(
                            _extract_component_validation_errors(full_task).keys()
                        )
                    except Exception as e:  # noqa: BLE001
                        logger.warning(
                            "Could not inspect workflow %s for password "
                            "validation errors (%s); treating as a "
                            "non-recoverable failure.", workflow_id, e,
                        )
                        failed_roles = []

                    if failed_roles:
                        password_retries_used += 1
                        logger.warning(
                            "Bringup workflow %s ended in %s due to password "
                            "validation failure(s) for %s — attempting "
                            "regenerate-and-retry %d/%d before giving up.",
                            workflow_id, status, failed_roles,
                            password_retries_used, max_password_retries,
                        )
                        try:
                            self._regenerate_and_retry(workflow_id, failed_roles)
                        except Exception as e:  # noqa: BLE001
                            logger.error(
                                "Regenerate-and-retry failed for workflow %s "
                                "(%s); surfacing the original failure.",
                                workflow_id, e,
                            )
                            return last_summary
                        # Give the installer a moment to register the retry, then
                        # resume the loop. (M9 deferred: ideally wait until the
                        # workflow leaves the pre-retry terminal status before
                        # re-evaluating, but that needs status-probe semantics.)
                        time.sleep(30)
                        continue

                logger.error(
                    "Bringup workflow %s ended in %s. Check the installer "
                    "UI / domainmanager.log for the failing task.",
                    workflow_id, status,
                )
                # Query the workflow for the specific failed task and its error
                try:
                    full_task = self._client.get_bringup(workflow_id)
                    # get_bringup returns a typed VapiStruct, not a dict — use getattr
                    sub_tasks = getattr(full_task, "sddc_sub_tasks", None) or []
                    failed_tasks = [
                        t for t in sub_tasks
                        if str(getattr(t, "status", "")).upper()
                        in ("FAILED", "COMPLETED_WITH_FAILURE")
                    ]
                    if failed_tasks:
                        logger.error("Failed task(s) in workflow %s:", workflow_id)
                        for ft in failed_tasks[:3]:
                            logger.error(
                                "  Task: %s | Status: %s",
                                getattr(ft, "name", "?"), getattr(ft, "status", "?"),
                            )
                            for err in (getattr(ft, "errors", None) or [])[:2]:
                                if hasattr(err, "arguments"):
                                    args_str = ", ".join(getattr(err, "arguments", []) or [])
                                    logger.error("    Error: %s", args_str[:300])
                                else:
                                    logger.error("    Error: %s", str(err)[:300])
                except Exception as e:
                    logger.warning(
                        "Could not query workflow %s for failure details: %s",
                        workflow_id, e,
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

        VCF Installer returns a different shape or a non-JSON 4xx. Checking both
        ``errorCode`` and the ``Authorization Header`` substring stays robust to
        message tweaks across VCF releases. Returns ``True`` on a match,
        ``False`` otherwise (including network errors, which keep the poll
        loop's retry).
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

        The validator requires this field even when
        ``skipEsxThumbprintValidation`` is set. Hosts with a pre-populated
        thumbprint are left alone; an unreachable host (e.g. via an SSH tunnel
        without ESXi port forwarding) gets a placeholder, and
        skipEsxThumbprintValidation must then be True.
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

    Skips the verbose nested sub-task lists; callers pass the id to ``check``
    for details when needed.
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
    """Heuristic: does this exception look like the VCF Installer -> SDDC
    Manager appliance transition?

    The SDK raises ``UnresolvedError`` when ``/v1/sddcs/{id}`` returns valid
    JSON that doesn't match the typed ``SddcTask`` -- what happens once the
    appliance transitions and speaks SDDC Manager's error JSON on the same
    path. Matched by class name (not an import) to keep the vapi-internals
    dependency soft; anything else keeps the "log and retry" behavior.
    """
    cls_name = type(exc).__name__
    if cls_name == "UnresolvedError":
        return True
    # Belt-and-suspenders: also look for the specific text fragment in
    # case future SDK versions wrap or rename the exception.
    return "Unable to convert unexpected vAPI error" in str(exc)


