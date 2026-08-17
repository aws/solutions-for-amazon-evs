# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""AWS client wrapper for centralized boto3 session and client management."""

import logging
from typing import Any

import boto3
from botocore.config import Config
from botocore.credentials import RefreshableCredentials
from botocore.session import get_session

logger = logging.getLogger(__name__)


class AWSClient:
    """Manages a boto3 session and provides access to AWS service clients.

    Centralizes session creation, region configuration, and client caching.

    Args:
        region: AWS region name (e.g. 'us-east-2').
        profile: Optional AWS credentials profile name.
        role_arn: Optional IAM role ARN to assume.
        session_name: Session name used when assuming a role.
    """

    def __init__(
        self,
        region: str,
        profile: str | None = None,
        role_arn: str | None = None,
        session_name: str = "evs-deployment",
    ) -> None:
        self._region = region
        self._profile = profile
        self._role_arn = role_arn
        self._session_name = session_name
        self._session: boto3.Session | None = None
        self._clients: dict[Any, Any] = {}
        self._account_id: str | None = None

    @property
    def session(self) -> boto3.Session:
        """Lazily create and return the boto3 session."""
        if self._session is None:
            self._session = self._build_session()
        return self._session

    def _build_session(self) -> boto3.Session:
        """Build a boto3 session, optionally assuming a role.

        Assumed-role credentials are REFRESHABLE: a one-shot
        ``sts.assume_role()`` gives static creds with a 1-hour lifetime that
        would raise ExpiredTokenException partway through this tool's 4-7 hour
        unattended deployment. RefreshableCredentials re-assumes the role
        before expiry on every subsequent call.
        """
        kwargs: dict[str, Any] = {"region_name": self._region}

        if self._profile:
            kwargs["profile_name"] = self._profile

        base_session = boto3.Session(**kwargs)

        if self._role_arn:
            logger.info("Assuming role (with auto-refresh): %s", self._role_arn)

            def _refresh() -> dict[str, str]:
                sts = base_session.client("sts")
                creds = sts.assume_role(
                    RoleArn=self._role_arn,
                    RoleSessionName=self._session_name,
                )["Credentials"]
                return {
                    "access_key": creds["AccessKeyId"],
                    "secret_key": creds["SecretAccessKey"],
                    "token": creds["SessionToken"],
                    "expiry_time": creds["Expiration"].isoformat(),
                }

            refreshable_creds = RefreshableCredentials.create_from_metadata(
                metadata=_refresh(),
                refresh_using=_refresh,
                method="sts-assume-role",
            )

            botocore_session = get_session()
            botocore_session._credentials = refreshable_creds
            botocore_session.set_config_variable("region", self._region)

            return boto3.Session(botocore_session=botocore_session)

        return base_session

    def client(self, service_name: str, **kwargs: Any) -> Any:
        """Return a cached boto3 client for the given service.

        Args:
            service_name: AWS service name (e.g. 'evs', 'ec2', 'route53').
            **kwargs: Additional arguments passed to session.client().

        Returns:
            A boto3 service client.
        """
        cache_key = (service_name, tuple(sorted(kwargs.items())))
        if cache_key not in self._clients:
            config = Config(
                retries={"max_attempts": 3, "mode": "adaptive"},
                connect_timeout=10,
                read_timeout=60,
            )
            self._clients[cache_key] = self.session.client(
                service_name, config=config, **kwargs
            )
            logger.debug("Created client for service: %s", service_name)

        return self._clients[cache_key]

    @property
    def region(self) -> str:
        """Return the configured AWS region."""
        return self._region

    @property
    def account_id(self) -> str:
        """Return the AWS account ID for the current session (memoized)."""
        if self._account_id is None:
            self._account_id = self.client("sts").get_caller_identity()["Account"]
        return self._account_id

    def __repr__(self) -> str:
        return (
            f"AWSClient(region={self._region!r}, "
            f"profile={self._profile!r}, "
            f"role_arn={self._role_arn!r})"
        )
