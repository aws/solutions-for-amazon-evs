# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Create and attach the EBS volume that hosts the VCF Installer VMFS.

One 256 GB encrypted gp3 volume per environment, attached at ``/dev/sdf``
to the EVS host that will run the VCF Installer OVA. The matching ESXi host formats it as a
local VMFS during the manual pre-bringup steps; once SDDC Manager / NSX /
vCenter are running on vSAN, Phase 3's ``remove-installer-datastore``
unmounts the VMFS and Phase 3's ``destroy-ebs-volume`` (this module's
detach + delete path) frees the underlying volume.

Tagging strategy
----------------

Every volume we create is tagged so the destroy path can identify it
unambiguously by environment:

  - ``Name = EVS-Host-Volume`` (matches the prior Terraform-managed value)
  - ``ManagedBy = phase2-automation``
  - ``EnvironmentId = <env_id>``
  - ``Phase = 2``

The destroy path refuses to act on any volume that doesn't carry both
``ManagedBy=phase2-automation`` and ``EnvironmentId=<env_id>`` tags.
That defends against accidentally deleting an operator-attached volume
or one belonging to a sibling environment.

Idempotency
-----------

``ensure_volume_attached`` is the entry point. It:

  1. Looks for an existing volume tagged for this env — if found, skip
     the create.
  2. Creates the volume if not.
  3. Waits for ``available``.
  4. Looks at the volume's current attachment — if already attached to
     the requested instance at the requested device, skip the attach.
  5. Otherwise attaches it.
  6. Waits for ``attached``.

Re-running on a fully-provisioned env is a no-op that just confirms the
expected state.
"""

import logging
import time
from typing import Any

from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)


# Volume defaults match the prior Terraform values exactly.
_DEFAULT_VOLUME_SIZE_GB = 256
_DEFAULT_VOLUME_TYPE = "gp3"
_DEFAULT_DEVICE_NAME = "/dev/sdf"
_VOLUME_NAME_TAG = "EVS-Host-Volume"

# Tag values used to mark our volumes so the destroy path can identify
# them safely.
_MANAGED_BY_TAG_VALUE = "phase2-automation"

# Wait budgets. A gp3 volume normally hits ``available`` within seconds
# but cold-start regions can drag; an attach can take a minute or two
# behind the scenes. Cap each to 5 minutes so a stuck call surfaces
# rather than blocking forever.
_AVAILABLE_TIMEOUT_SECONDS = 300
_ATTACH_TIMEOUT_SECONDS = 300
_DETACH_TIMEOUT_SECONDS = 300
_POLL_INTERVAL_SECONDS = 5


class EbsVolumeManager:
    """Owns the EBS volume's full lifecycle for an EVS environment.

    Args:
        ec2_client: A boto3 ``ec2`` client.
        environment_id: EVS environment id; used in tags for ownership.
    """

    def __init__(self, ec2_client: Any, environment_id: str) -> None:
        if not environment_id:
            raise ValueError(
                "EbsVolumeManager requires an environment_id; can't tag "
                "the volume without one."
            )
        self._ec2 = ec2_client
        self._environment_id = environment_id

    # ---- create + attach -------------------------------------------------

    def ensure_volume_attached(
        self,
        *,
        availability_zone: str,
        target_instance_id: str,
        volume_size_gb: int = _DEFAULT_VOLUME_SIZE_GB,
        volume_type: str = _DEFAULT_VOLUME_TYPE,
        device_name: str = _DEFAULT_DEVICE_NAME,
        dry_run: bool = False,
    ) -> dict[str, str]:
        """Idempotently create + attach the env's EBS volume.

        Args:
            availability_zone: AZ for the volume. Must match the instance's
                AZ; AWS rejects cross-AZ attaches.
            target_instance_id: EC2 instance id to attach to.
            volume_size_gb: Size in GB. Default 256.
            volume_type: EBS volume type. Default gp3.
            device_name: Block device name on the instance. Default
                ``/dev/sdf``.
            dry_run: If True, log the plan without creating or attaching.

        Returns:
            Dict with ``volumeId``, ``volumeAttachmentId``, and a
            ``created`` boolean indicating whether this run created the
            volume.
        """
        existing = self._find_existing_volume()
        if existing is None:
            if dry_run:
                logger.info(
                    "DRY RUN — would create %d GB %s volume in AZ %s, "
                    "tagged for env %s, attach to %s at %s",
                    volume_size_gb, volume_type, availability_zone,
                    self._environment_id, target_instance_id, device_name,
                )
                return {
                    "volumeId": "",
                    "volumeAttachmentId": "",
                    "created": True,
                }
            volume_id = self._create_volume(
                availability_zone=availability_zone,
                volume_size_gb=volume_size_gb,
                volume_type=volume_type,
            )
            self._wait_for_state(
                volume_id, "available",
                timeout_seconds=_AVAILABLE_TIMEOUT_SECONDS,
            )
            created = True
        else:
            volume_id = existing["VolumeId"]
            existing_size = existing.get("Size", 0)
            existing_type = existing.get("VolumeType", "")
            if existing_size != volume_size_gb or existing_type != volume_type:
                logger.warning(
                    "Existing volume %s for env %s has size=%dGB type=%s — "
                    "differs from requested size=%dGB type=%s. Leaving "
                    "as-is; an operator change to volume size/type "
                    "won't propagate without explicit recreation.",
                    volume_id, self._environment_id,
                    existing_size, existing_type,
                    volume_size_gb, volume_type,
                )
            else:
                logger.info(
                    "Found existing volume %s (%dGB %s) tagged for env %s; "
                    "skipping create",
                    volume_id, existing_size, existing_type,
                    self._environment_id,
                )
            created = False

        attachment_id = self._ensure_attached(
            volume_id=volume_id,
            instance_id=target_instance_id,
            device_name=device_name,
            dry_run=dry_run,
        )

        return {
            "volumeId": volume_id,
            "volumeAttachmentId": attachment_id,
            "created": created,
        }

    def _create_volume(
        self,
        *,
        availability_zone: str,
        volume_size_gb: int,
        volume_type: str,
    ) -> str:
        """``CreateVolume`` with the project's standard tags."""
        logger.info(
            "Creating %d GB %s volume in AZ %s for env %s",
            volume_size_gb, volume_type, availability_zone,
            self._environment_id,
        )
        response = self._ec2.create_volume(
            AvailabilityZone=availability_zone,
            Size=volume_size_gb,
            VolumeType=volume_type,
            Encrypted=True,
            TagSpecifications=[{
                "ResourceType": "volume",
                "Tags": [
                    {"Key": "Name", "Value": _VOLUME_NAME_TAG},
                    {"Key": "ManagedBy", "Value": _MANAGED_BY_TAG_VALUE},
                    {"Key": "EnvironmentId", "Value": self._environment_id},
                    {"Key": "Phase", "Value": "2"},
                ],
            }],
        )
        volume_id = response["VolumeId"]
        logger.info("  -> volume %s", volume_id)
        return volume_id

    def _ensure_attached(
        self,
        *,
        volume_id: str,
        instance_id: str,
        device_name: str,
        dry_run: bool,
    ) -> str:
        """Idempotently attach ``volume_id`` to ``instance_id`` at ``device_name``.

        If the volume's current attachment matches what we'd create,
        skip the ``AttachVolume`` call. If it's attached somewhere else,
        raise — we don't want to silently steal a volume from another
        instance.
        """
        attachments = self._describe_attachments(volume_id)
        for att in attachments:
            att_instance = att.get("InstanceId", "")
            att_device = att.get("Device", "")
            att_state = (att.get("State") or "").lower()
            if att_instance == instance_id and att_device == device_name:
                logger.info(
                    "Volume %s already attached to %s at %s (state=%s); "
                    "skipping attach",
                    volume_id, instance_id, device_name, att_state,
                )
                return att.get("VolumeAttachmentId") or ""
            if att_state in {"attached", "attaching"}:
                raise RuntimeError(
                    f"Volume {volume_id} is already attached to a "
                    f"different instance ({att_instance} at {att_device}, "
                    f"state={att_state}). Refusing to detach — manual "
                    f"review required."
                )

        if dry_run:
            logger.info(
                "DRY RUN — would attach volume %s to %s at %s",
                volume_id, instance_id, device_name,
            )
            return ""

        logger.info(
            "Attaching volume %s to instance %s at %s",
            volume_id, instance_id, device_name,
        )
        self._ec2.attach_volume(
            VolumeId=volume_id,
            InstanceId=instance_id,
            Device=device_name,
        )
        # AWS doesn't return an explicit ``VolumeAttachmentId`` on
        # ``AttachVolume``; the attachment is uniquely identified by
        # the (volume_id, instance_id) pair. Re-describe to capture it
        # for the return value.
        self._wait_for_attachment_state(
            volume_id=volume_id,
            instance_id=instance_id,
            target_state="attached",
            timeout_seconds=_ATTACH_TIMEOUT_SECONDS,
        )
        attachments = self._describe_attachments(volume_id)
        for att in attachments:
            if att.get("InstanceId") == instance_id:
                return att.get("VolumeAttachmentId") or ""
        return ""

    # ---- destroy --------------------------------------------------------

    def detach_and_delete(self, dry_run: bool = False) -> dict[str, Any]:
        """Detach (if attached) and delete the env's EBS volume.

        Refuses to act on a volume that doesn't carry both our
        ``ManagedBy=phase2-automation`` and ``EnvironmentId=<env>`` tags.
        Idempotent: if the volume is already gone, returns
        ``{"deleted": False, "reason": "not present"}``.

        Returns a summary dict with ``deleted`` (bool), ``volumeId``,
        and a ``previousAttachment`` describing where it was attached.
        """
        existing = self._find_existing_volume()
        if existing is None:
            logger.info(
                "No volume tagged for env %s; nothing to detach/delete",
                self._environment_id,
            )
            return {"deleted": False, "reason": "not present"}

        volume_id = existing["VolumeId"]
        previous: dict[str, str] = {}

        attachments = existing.get("Attachments") or []
        if attachments:
            att = attachments[0]
            previous = {
                "instanceId": att.get("InstanceId", ""),
                "device": att.get("Device", ""),
                "state": (att.get("State") or "").lower(),
            }
            if dry_run:
                logger.info(
                    "DRY RUN — would detach volume %s from %s at %s",
                    volume_id, previous["instanceId"], previous["device"],
                )
            else:
                logger.info(
                    "Detaching volume %s from %s at %s",
                    volume_id, previous["instanceId"], previous["device"],
                )
                self._ec2.detach_volume(VolumeId=volume_id)
                self._wait_for_state(
                    volume_id, "available",
                    timeout_seconds=_DETACH_TIMEOUT_SECONDS,
                )

        if dry_run:
            logger.info("DRY RUN — would delete volume %s", volume_id)
            return {
                "deleted": False,
                "volumeId": volume_id,
                "previousAttachment": previous,
                "reason": "dry-run",
            }

        logger.info("Deleting volume %s", volume_id)
        self._ec2.delete_volume(VolumeId=volume_id)
        return {
            "deleted": True,
            "volumeId": volume_id,
            "previousAttachment": previous,
        }

    # ---- internal lookups -----------------------------------------------

    def _find_existing_volume(self) -> dict[str, Any] | None:
        """Find this env's volume by tag.

        Returns the volume dict (as ``DescribeVolumes`` returns it), or
        ``None`` if no matching volume exists. Raises if more than one
        volume matches — the destroy path is ambiguous in that case and
        we refuse to guess.
        """
        response = self._ec2.describe_volumes(
            Filters=[
                {
                    "Name": "tag:ManagedBy",
                    "Values": [_MANAGED_BY_TAG_VALUE],
                },
                {
                    "Name": "tag:EnvironmentId",
                    "Values": [self._environment_id],
                },
            ],
        )
        volumes = response.get("Volumes") or []
        if not volumes:
            return None
        if len(volumes) > 1:
            ids = sorted(v["VolumeId"] for v in volumes)
            raise RuntimeError(
                f"Found {len(volumes)} volumes tagged for env "
                f"{self._environment_id}: {ids}. Expected exactly one. "
                f"Manual cleanup required."
            )
        return volumes[0]

    def _describe_attachments(self, volume_id: str) -> list[dict[str, Any]]:
        """Return the volume's current attachments (may be empty)."""
        response = self._ec2.describe_volumes(VolumeIds=[volume_id])
        volumes = response.get("Volumes") or []
        if not volumes:
            return []
        return volumes[0].get("Attachments") or []

    def _wait_for_state(
        self,
        volume_id: str,
        target_state: str,
        *,
        timeout_seconds: int,
    ) -> None:
        """Poll ``DescribeVolumes`` until the volume reaches ``target_state``."""
        deadline = time.time() + timeout_seconds
        last_state = ""
        while time.time() < deadline:
            response = self._ec2.describe_volumes(VolumeIds=[volume_id])
            volumes = response.get("Volumes") or []
            if not volumes:
                # ``DescribeVolumes`` can briefly fail to surface a
                # just-created volume; retry rather than fail fast.
                time.sleep(_POLL_INTERVAL_SECONDS)
                continue
            state = (volumes[0].get("State") or "").lower()
            if state != last_state:
                logger.info("Volume %s state=%s", volume_id, state)
                last_state = state
            if state == target_state:
                return
            if state == "error":
                raise RuntimeError(
                    f"Volume {volume_id} entered 'error' state while "
                    f"waiting for '{target_state}'."
                )
            time.sleep(_POLL_INTERVAL_SECONDS)
        raise TimeoutError(
            f"Volume {volume_id} did not reach state '{target_state}' "
            f"within {timeout_seconds}s (last state: {last_state})."
        )

    def _wait_for_attachment_state(
        self,
        *,
        volume_id: str,
        instance_id: str,
        target_state: str,
        timeout_seconds: int,
    ) -> None:
        """Poll until the (volume, instance) attachment reaches ``target_state``."""
        deadline = time.time() + timeout_seconds
        last_state = ""
        while time.time() < deadline:
            try:
                attachments = self._describe_attachments(volume_id)
            except ClientError as e:
                # A just-created attachment can briefly 404; retry.
                code = e.response.get("Error", {}).get("Code", "")
                if code == "InvalidVolume.NotFound":
                    time.sleep(_POLL_INTERVAL_SECONDS)
                    continue
                raise
            for att in attachments:
                if att.get("InstanceId") != instance_id:
                    continue
                state = (att.get("State") or "").lower()
                if state != last_state:
                    logger.info(
                        "Attachment %s -> %s state=%s",
                        volume_id, instance_id, state,
                    )
                    last_state = state
                if state == target_state:
                    return
            time.sleep(_POLL_INTERVAL_SECONDS)
        raise TimeoutError(
            f"Attachment of {volume_id} to {instance_id} did not reach "
            f"state '{target_state}' within {timeout_seconds}s "
            f"(last state: {last_state})."
        )
