# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Associate EVS-created VLAN subnets with Phase 1's service access route table.

EVS creates one subnet per VLAN when the environment lands; left alone, those subnets
inherit the VPC's main route table. We re-associate them with the
service-access route table Phase 1 created so every VLAN shares the same
egress path (NAT gateway, TGW, or wherever the service-access RTB
points).

Idempotent: re-running on already-associated subnets logs ``skip`` and
moves on. Tolerates the race window between EVS creating a subnet and
the subnet becoming visible to ``DescribeRouteTables`` — retries with
exponential backoff before giving up.
"""

import logging
import time
from typing import Any

from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)


# Retry parameters for the ``AssociateRouteTable`` call. EVS reports a
# subnet as ``CREATED`` slightly before AWS-internal propagation finishes,
# so the first call can race and surface as ``InvalidSubnetID.NotFound``.
# Try a handful of times with exponential backoff before giving up.
_MAX_ATTEMPTS = 5
_INITIAL_BACKOFF_SECONDS = 2.0
_BACKOFF_MULTIPLIER = 2.0


# Error codes that we consider transient and worth retrying. Anything else
# (auth failure, malformed request) bubbles immediately.
_RETRYABLE_ERROR_CODES = frozenset({
    "InvalidSubnetID.NotFound",
    "InvalidRouteTableID.NotFound",
    "RequestLimitExceeded",
    "Throttling",
    "ThrottlingException",
})


class VlanRouteTableAssociator:
    """Manages the (idempotent) association of EVS VLAN subnets to a
    service-access route table.

    Args:
        ec2_client: A boto3 ``ec2`` client.
    """

    def __init__(self, ec2_client: Any) -> None:
        self._ec2 = ec2_client

    def associate_subnets(
        self,
        route_table_id: str,
        subnet_ids: list[str],
        dry_run: bool = False,
    ) -> dict[str, str]:
        """Associate every subnet in ``subnet_ids`` with ``route_table_id``.

        Args:
            route_table_id: Phase 1's service-access route table id.
            subnet_ids: Subnet ids EVS created for the environment.
            dry_run: If True, log what would happen without making any
                ``AssociateRouteTable`` calls.

        Returns:
            ``{subnet_id: association_id}`` for every subnet that ended
            up associated (whether by this run or a prior one). Mirrors
            the Terraform output shape so callers can treat the two
            interchangeably during the migration window.
        """
        if not subnet_ids:
            logger.warning(
                "No subnet ids passed to associate_subnets; nothing to do"
            )
            return {}

        existing = self._existing_associations(route_table_id)
        logger.info(
            "Route table %s already associated with %d subnet(s): %s",
            route_table_id, len(existing), sorted(existing.keys()) or "<none>",
        )

        results: dict[str, str] = dict(existing)
        for subnet_id in subnet_ids:
            if subnet_id in existing:
                logger.info(
                    "skip subnet %s — already associated (assoc=%s)",
                    subnet_id, existing[subnet_id],
                )
                continue
            if dry_run:
                logger.info(
                    "DRY RUN — would associate subnet %s with route table %s",
                    subnet_id, route_table_id,
                )
                continue
            assoc_id = self._associate_with_retry(
                route_table_id=route_table_id, subnet_id=subnet_id,
            )
            results[subnet_id] = assoc_id

        if dry_run:
            return results

        # Post-confirmation read: re-describe the route table and assert
        # every requested subnet is still associated. Catches the rare
        # edge where a concurrent disassociation lands between our
        # AssociateRouteTable success and the operator's next look.
        # Replaces our in-memory accumulator with AWS's authoritative
        # view so the returned dict is verified, not just hoped-for.
        final = self._existing_associations(route_table_id)
        missing = [s for s in subnet_ids if s not in final]
        if missing:
            raise RuntimeError(
                f"Post-association verification failed: subnets "
                f"{sorted(missing)} aren't associated with route table "
                f"{route_table_id} despite AssociateRouteTable returning "
                f"success. Possible concurrent disassociation. Re-run "
                f"to retry."
            )
        return final

    def _associate_with_retry(
        self, *, route_table_id: str, subnet_id: str,
    ) -> str:
        """Call ``AssociateRouteTable`` with exponential-backoff retries.

        Returns the association id. Raises on permanent failure or after
        exhausting the retry budget.
        """
        backoff = _INITIAL_BACKOFF_SECONDS
        last_error: Exception | None = None
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            try:
                logger.info(
                    "Associating subnet %s with route table %s "
                    "(attempt %d/%d)",
                    subnet_id, route_table_id, attempt, _MAX_ATTEMPTS,
                )
                response = self._ec2.associate_route_table(
                    SubnetId=subnet_id,
                    RouteTableId=route_table_id,
                )
                assoc_id = response.get("AssociationId") or ""
                logger.info(
                    "  -> assoc=%s subnet=%s rtb=%s",
                    assoc_id, subnet_id, route_table_id,
                )
                return assoc_id
            except ClientError as e:
                code = e.response.get("Error", {}).get("Code", "")
                last_error = e
                if code == "Resource.AlreadyAssociated":
                    # Race: between our existing-association check and
                    # this call, something else associated the subnet.
                    # Treat as success and re-read to get the id.
                    logger.info(
                        "Subnet %s was already associated (likely by a "
                        "concurrent run); reading association id",
                        subnet_id,
                    )
                    existing = self._existing_associations(route_table_id)
                    if subnet_id in existing:
                        return existing[subnet_id]
                    # If we still can't find it, that's odd — surface the
                    # original error so the operator can investigate.
                    raise
                if code not in _RETRYABLE_ERROR_CODES:
                    # Auth, malformed request, etc. — no point retrying.
                    raise
                if attempt == _MAX_ATTEMPTS:
                    logger.error(
                        "Exhausted %d attempts associating subnet %s "
                        "with route table %s; last error %s",
                        _MAX_ATTEMPTS, subnet_id, route_table_id, code,
                    )
                    raise
                logger.warning(
                    "Transient error %s associating subnet %s; "
                    "retrying in %.1fs",
                    code, subnet_id, backoff,
                )
                time.sleep(backoff)
                backoff *= _BACKOFF_MULTIPLIER
        # Unreachable — loop either returns or re-raises.
        raise RuntimeError(
            f"associate_subnets retry loop exited without resolving "
            f"(last error: {last_error})"
        )

    def _existing_associations(self, route_table_id: str) -> dict[str, str]:
        """Read the route table's current associations.

        Returns a ``{subnet_id: association_id}`` map for explicit subnet
        associations only. The "main" association (no subnet) is skipped.
        """
        response = self._ec2.describe_route_tables(
            RouteTableIds=[route_table_id],
        )
        tables = response.get("RouteTables") or []
        if not tables:
            raise RuntimeError(
                f"Route table {route_table_id} not found — "
                f"is the Phase 1 Terraform applied?"
            )

        associations: dict[str, str] = {}
        for assoc in tables[0].get("Associations") or []:
            subnet_id = assoc.get("SubnetId")
            if not subnet_id:
                # Main-table associations don't have a subnet id.
                continue
            associations[subnet_id] = assoc.get("RouteTableAssociationId", "")
        return associations

    def disassociate_subnets(
        self,
        route_table_id: str,
        subnet_ids: list[str] | None = None,
        dry_run: bool = False,
    ) -> list[str]:
        """Tear down the associations created by :meth:`associate_subnets`.

        Args:
            route_table_id: The route table whose associations to remove.
            subnet_ids: Subnets to disassociate. ``None`` means "every
                subnet currently associated with the table" — useful for
                full-environment teardown.
            dry_run: If True, log what would happen.

        Returns:
            The subnet ids whose associations were removed (or would be).
        """
        existing = self._existing_associations(route_table_id)
        targets = list(existing.keys()) if subnet_ids is None else [
            s for s in subnet_ids if s in existing
        ]
        if not targets:
            logger.info(
                "No associations to remove on route table %s",
                route_table_id,
            )
            return []

        removed: list[str] = []
        for subnet_id in targets:
            assoc_id = existing[subnet_id]
            if dry_run:
                logger.info(
                    "DRY RUN — would disassociate %s (subnet %s) from %s",
                    assoc_id, subnet_id, route_table_id,
                )
                removed.append(subnet_id)
                continue
            logger.info(
                "Disassociating %s (subnet %s) from %s",
                assoc_id, subnet_id, route_table_id,
            )
            self._ec2.disassociate_route_table(AssociationId=assoc_id)
            removed.append(subnet_id)
        return removed
