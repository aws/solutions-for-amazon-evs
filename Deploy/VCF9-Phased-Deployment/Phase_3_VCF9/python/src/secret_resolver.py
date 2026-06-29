# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Resolve ``__SECRET:<role>__`` placeholders against AWS Secrets Manager.

Phase 2's spec builders write placeholder strings like
``__SECRET:vcenterRoot__`` everywhere a VCF appliance password would
normally appear. The on-disk JSON specs (``bringup_spec.json``,
``edge_cluster_spec.json``) are therefore inert — they don't contain
real passwords.

This module walks a typed VAPI struct (or a plain dict) and substitutes
those placeholders with the actual password value, fetched from
``evs-<env_id>_<role>`` in AWS Secrets Manager. The substitution
happens in-memory just before the typed object is POSTed to the
installer / NSX, so passwords never touch disk on the Phase 3 side.

The resolver:

  - caches each looked-up secret value so a spec referencing the same
    role 4 times only triggers one API call
  - handles both typed VAPI structs (via reflection over their ``_attrs``)
    and dict-shaped specs (via recursion)
  - validates upfront that every required placeholder has a corresponding
    secret. Missing secrets surface as a single ``MissingSecretsError``
    listing every absent role at once, instead of failing one at a time.
"""

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

# Placeholder format the Phase 2 spec builders emit. Match group 1 is the
# role token. The token can be either a plain appliance role
# (``vcenterRoot``) or a prefixed ESXi role (``esxi:esxi01``). The colon
# is the only non-word character allowed because the ESXi flavor
# disambiguates the secret-name format (dash for appliances, bang for
# ESXi). Anchored to the full string for the simple case + scanned via
# search() for embedded matches.
_PLACEHOLDER_RE = re.compile(r"__SECRET:([A-Za-z0-9_:]+)__")
_ESXI_PREFIX = "esxi:"


class MissingSecretsError(RuntimeError):
    """Raised when one or more required secrets aren't in Secrets Manager.

    Lists every missing role at once so an operator running the Phase 3
    one-click SDDC + NSX deployment for the first time gets a single
    actionable error rather than a cascade of individual failures.
    """

    def __init__(self, missing_roles: list[str], env_id: str) -> None:
        self.missing_roles = missing_roles
        self.env_id = env_id
        super().__init__(
            f"Missing {len(missing_roles)} VCF secret(s) in AWS Secrets "
            f"Manager for env {env_id}: {', '.join(sorted(missing_roles))}. "
            f"Run Phase 2's post-evs-sync-config (or deploy-environment) to "
            f"provision them, then re-run."
        )


# ---- Helpers -------------------------------------------------------------


def secret_name(env_id: str, role: str) -> str:
    """Compose the Secrets Manager secret name for an env + role.

    Public mirror of Phase 2's ``vcf_password_provisioner.secret_name``.
    Two flavors:

      - **Appliance role** (e.g. ``vcenterRoot``, ``nsxAdmin``,
        ``edgeAppliance``): returns ``evs-<env>_<role>``. Uses a
        dash separator because customer-created secret names are
        restricted to ``[a-zA-Z0-9/_+=.@-]``.
      - **ESXi role** (prefixed with ``esxi:``, e.g. ``esxi:esxi01``):
        returns ``evs!<env>_<host>``. The bang is allowed here
        because the EVS service created these secrets during host
        provisioning before Phase 2 ever runs; we only read them.

    Phase 3 callers (``main.py``) use this for runtime auth (NSX Manager
    admin, vCenter SSO administrator). The resolver uses it on every
    placeholder substitution.
    """
    if role.startswith(_ESXI_PREFIX):
        host = role[len(_ESXI_PREFIX):]
        return f"evs!{env_id}_{host}"
    return f"evs-{env_id}_{role}"


# Internal alias kept for symmetry with the rest of the module.
_secret_name = secret_name


def find_placeholders(value: Any) -> set[str]:
    """Walk ``value`` and return every role name referenced by a placeholder.

    Supports plain dicts, lists, tuples, and strings. Other types are
    left alone — placeholders only live in string-valued password fields
    inside the JSON spec dicts.

    The returned set lets the caller validate "do we have every secret we
    need" with a single batch of ``DescribeSecret`` calls before mutating
    the spec.
    """
    roles: set[str] = set()

    def visit(node: Any) -> None:
        if isinstance(node, str):
            for m in _PLACEHOLDER_RE.finditer(node):
                roles.add(m.group(1))
        elif isinstance(node, dict):
            for v in node.values():
                visit(v)
        elif isinstance(node, (list, tuple)):
            for item in node:
                visit(item)

    visit(value)
    return roles


class SecretResolver:
    """Fetches and caches Secrets Manager values, then substitutes them
    into a spec.

    Usage:
        resolver = SecretResolver(sm_client, env_id)
        resolver.preflight(spec)            # validate all secrets exist
        resolver.resolve_in_place(spec)     # mutate placeholders in-place

    ``preflight`` is the right entry point for the one-click SDDC + NSX
    deployment's "fail fast before doing anything" check.
    ``resolve_in_place`` is what the bringup / edge handlers call right
    before POSTing.
    """

    def __init__(self, sm_client: Any, env_id: str) -> None:
        self._sm = sm_client
        self._env_id = env_id
        self._cache: dict[str, str] = {}

    # ---- preflight ----

    def preflight(self, *specs: Any) -> None:
        """Validate that every placeholder across ``specs`` resolves to an
        existing secret. Doesn't fetch values — uses ``DescribeSecret`` so
        we don't pull plaintext into memory until we actually need it.

        Raises ``MissingSecretsError`` listing every absent role at once.
        """
        from botocore.exceptions import ClientError

        roles: set[str] = set()
        for spec in specs:
            roles |= find_placeholders(spec)

        if not roles:
            return

        missing: list[str] = []
        for role in sorted(roles):
            try:
                self._sm.describe_secret(SecretId=_secret_name(self._env_id, role))
            except ClientError as e:
                code = e.response.get("Error", {}).get("Code", "")
                if code == "ResourceNotFoundException":
                    missing.append(role)
                else:
                    raise

        if missing:
            raise MissingSecretsError(missing, self._env_id)

        logger.info(
            "Preflight verified %d VCF appliance secret(s) for env %s",
            len(roles), self._env_id,
        )

    # ---- resolution ----

    def get(self, role: str) -> str:
        """Fetch the secret value for a role; cache for the duration of
        this resolver instance.
        """
        if role in self._cache:
            return self._cache[role]
        name = _secret_name(self._env_id, role)
        response = self._sm.get_secret_value(SecretId=name)
        raw = response.get("SecretString") or ""
        if not raw and response.get("SecretBinary"):
            raw = response["SecretBinary"].decode("utf-8")
        if not raw:
            raise RuntimeError(
                f"Secret {name} exists but its value is empty; can't continue."
            )
        # Secrets may be stored as JSON {"username": "...", "password": "..."}
        # or as plain password strings (legacy). Handle both.
        import json as _json
        try:
            parsed = _json.loads(raw)
            if isinstance(parsed, dict) and "password" in parsed:
                value = parsed["password"]
            else:
                value = raw
        except (ValueError, _json.JSONDecodeError):
            value = raw
        self._cache[role] = value
        return value

    def _replace(self, s: str) -> str:
        """Replace every placeholder in a string with its secret value.

        Most password fields are exact matches like
        ``"__SECRET:vcenterRoot__"``, but support embedded matches too in
        case a future spec field carries a templated string.
        """
        def sub(m: re.Match) -> str:
            return self.get(m.group(1))
        return _PLACEHOLDER_RE.sub(sub, s)

    def resolve_in_place(self, value: Any) -> None:
        """Mutate ``value`` in-place, replacing every placeholder string.

        Walks dicts and lists. Both call sites resolve on the raw dict
        loaded from JSON before handing it to a typed deserializer, so
        the dict path is the only one exercised in practice. The leaf
        case is a string match inside a dict value or list item — bare
        strings can't be mutated in-place because Python strings are
        immutable.
        """
        if isinstance(value, dict):
            for k, v in value.items():
                if isinstance(v, str) and "__SECRET:" in v:
                    value[k] = self._replace(v)
                else:
                    self.resolve_in_place(v)
        elif isinstance(value, list):
            for i, item in enumerate(value):
                if isinstance(item, str) and "__SECRET:" in item:
                    value[i] = self._replace(item)
                else:
                    self.resolve_in_place(item)
        # Anything else (int, bool, None, plain str, typed VAPI struct) —
        # leave alone. Resolve on the JSON dict before typed conversion
        # if you have a typed object.
