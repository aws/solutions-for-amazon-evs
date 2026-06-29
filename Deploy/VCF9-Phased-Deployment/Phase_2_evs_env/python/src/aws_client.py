# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""AWS client wrapper for centralized boto3 session and client management."""

import logging
from typing import Any

import boto3
from botocore.config import Config

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
        self._clients: dict[str, Any] = {}

    @property
    def session(self) -> boto3.Session:
        """Lazily create and return the boto3 session."""
        if self._session is None:
            self._session = self._build_session()
        return self._session

    def _build_session(self) -> boto3.Session:
        """Build a boto3 session, optionally assuming a role."""
        kwargs: dict[str, Any] = {"region_name": self._region}

        if self._profile:
            kwargs["profile_name"] = self._profile

        base_session = boto3.Session(**kwargs)

        if self._role_arn:
            logger.info("Assuming role: %s", self._role_arn)
            sts = base_session.client("sts")
            credentials = sts.assume_role(
                RoleArn=self._role_arn,
                RoleSessionName=self._session_name,
            )["Credentials"]

            return boto3.Session(
                aws_access_key_id=credentials["AccessKeyId"],
                aws_secret_access_key=credentials["SecretAccessKey"],
                aws_session_token=credentials["SessionToken"],
                region_name=self._region,
            )

        return base_session

    def client(self, service_name: str, **kwargs: Any) -> Any:
        """Return a cached boto3 client for the given service.

        Args:
            service_name: AWS service name (e.g. 'evs', 'ec2', 'route53').
            **kwargs: Additional arguments passed to session.client().

        Returns:
            A boto3 service client.
        """
        if service_name not in self._clients:
            config = Config(
                retries={"max_attempts": 3, "mode": "adaptive"},
            )
            self._clients[service_name] = self.session.client(
                service_name, config=config, **kwargs
            )
            logger.debug("Created client for service: %s", service_name)

        return self._clients[service_name]

    @property
    def region(self) -> str:
        """Return the configured AWS region."""
        return self._region

    @property
    def account_id(self) -> str:
        """Return the AWS account ID for the current session."""
        return self.client("sts").get_caller_identity()["Account"]

    def __repr__(self) -> str:
        return (
            f"AWSClient(region={self._region!r}, "
            f"profile={self._profile!r}, "
            f"role_arn={self._role_arn!r})"
        )
