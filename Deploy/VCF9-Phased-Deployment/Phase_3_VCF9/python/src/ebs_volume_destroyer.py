# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Detach and delete the EBS volume that hosted the VCF Installer VMFS.

Phase 2's ``ebs_volume_manager.EbsVolumeManager`` creates the volume
during environment provisioning, tagging it with
``ManagedBy=phase2-automation`` + ``EnvironmentId=<env>`` so we can
identify it unambiguously here. This module mirrors just the destroy
half of that lifecycle — by phase boundary the one-click SDDC + NSX
deployment doesn't need to create EBS volumes, only release them after
the local installer VMFS has been unmounted.

Destroys are gated by tag:

  - Refuses to act on a volume that doesn't carry both
    ``ManagedBy=phase2-automation`` and ``EnvironmentId=<env>`` tags.
    Defends against accidentally deleting an operator-attached volume
    or one belonging to a sibling environment.

Idempotent: if the volume is already gone, the call returns
``{"deleted": False, "reason": "not present"}`` and the one-click
SDDC + NSX deployment proceeds.
"""

import logging
import time
from typing import Any

from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)


# Tag values that mark a volume as ours. Must match what Phase 2's
# ``ebs_volume_manager.py`` writes when it creates the volume —
# changing one without the other breaks the destroy path.
_MANAGED_BY_TAG_VALUE = "phase2-automation"

# Wait budget for the detach. AWS detach is async; the volume reaches
# ``available`` shortly after the call returns. Cap at 5 minutes so a
# stuck call surfaces rather than blocking forever.
_DETACH_TIMEOUT_SECONDS = 300
_POLL_INTERVAL_SECONDS = 5


class EbsVolumeDestroyer:
    """Identify, detach, and delete a tagged EBS volume.

    Args:
        ec2_client: A boto3 ``ec2`` client.
        environment_id: EVS environment id; only volumes tagged with
            this exact id are eligible for deletion.
    """

    def __init__(self, ec2_client: Any, environment_id: str) -> None:
        if not environment_id:
            raise ValueError(
                "EbsVolumeDestroyer requires an environment_id; can't "
                "identify the volume safely without one."
            )
        self._ec2 = ec2_client
        self._environment_id = environment_id

    def detach_and_delete(self, dry_run: bool = False) -> dict[str, Any]:
        """Detach (if attached) and delete the env's EBS volume.

        Sequence:
          1. Find the volume by tag. If absent, return
             ``{"deleted": False, "reason": "not present"}``.
          2. If attached, ``DetachVolume`` and poll until the volume
             reaches ``available``.
          3. ``DeleteVolume``.

        Returns a summary dict with ``deleted`` (bool), ``volumeId``,
        and a ``previousAttachment`` describing where the volume was
        attached before this run (useful for log audits).
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

    def _find_existing_volume(self) -> dict[str, Any] | None:
        """Find this env's volume by tag.

        Returns the volume dict (as ``DescribeVolumes`` returns it), or
        ``None`` if no matching volume exists. Raises if more than one
        volume matches — the destroy path is ambiguous in that case
        and we refuse to guess.
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
            try:
                response = self._ec2.describe_volumes(VolumeIds=[volume_id])
            except ClientError as e:
                # If the volume disappeared while we were waiting (rare
                # race where a parallel cleanup ran), treat that as
                # success since the next step would have been delete.
                code = e.response.get("Error", {}).get("Code", "")
                if code == "InvalidVolume.NotFound":
                    logger.info(
                        "Volume %s vanished mid-wait; treating as terminal",
                        volume_id,
                    )
                    return
                raise
            volumes = response.get("Volumes") or []
            if not volumes:
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
