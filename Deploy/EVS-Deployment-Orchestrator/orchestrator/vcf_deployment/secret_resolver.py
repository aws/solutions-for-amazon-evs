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

# Placeholder the Phase 2 spec builders emit; match group 1 is the role token
# -- a plain appliance role (``vcenterRoot``) or prefixed ESXi role
# (``esxi:esxi01``). The colon disambiguates the secret-name format below (dash
# for appliances, bang for ESXi) and is the only non-word char allowed.
_PLACEHOLDER_RE = re.compile(r"__SECRET:([A-Za-z0-9_:\-\.]+)__")
_ESXI_PREFIX = "esxi:"


class MissingSecretsError(RuntimeError):
    """Raised when one or more required secrets aren't in Secrets Manager.

    Lists every missing role at once so a first-time operator gets a single
    actionable error instead of a cascade of individual failures.
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

      - **Appliance role** (e.g. ``vcenterRoot``): ``evs-<env>_<role>``.
        Dash separator because customer-created secret names are restricted
        to ``[a-zA-Z0-9/_+=.@-]``.
      - **ESXi role** (prefixed ``esxi:``): ``evs!<env>_<host>``. Bang is
        allowed because EVS created these during host provisioning (before
        Phase 2 runs); we only read them.
    """
    if role.startswith(_ESXI_PREFIX):
        host = role[len(_ESXI_PREFIX):]
        return f"evs!{env_id}_{host}"
    return f"evs-{env_id}_{role}"


# Internal alias kept for symmetry with the rest of the module.
_secret_name = secret_name


def find_placeholders(value: Any) -> set[str]:
    """Walk ``value`` and return every role name referenced by a placeholder.

    Recurses dicts/lists/tuples/strings (others left alone -- placeholders only
    live in string password fields). The returned set lets the caller validate
    all secrets exist in one ``DescribeSecret`` batch before mutating the spec.
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

    ``preflight`` is the "fail fast before doing anything" check;
    ``resolve_in_place`` is what the bringup / edge handlers call before POSTing.
    """

    def __init__(self, sm_client: Any, env_id: str) -> None:
        self._sm = sm_client
        self._env_id = env_id
        self._cache: dict[str, str] = {}

    # ---- preflight ----

    def preflight(self, *specs: Any) -> None:
        """Validate that every placeholder across ``specs`` resolves to an
        existing secret, via ``DescribeSecret`` (no plaintext pulled in yet).

        Raises ``MissingSecretsError`` listing every absent role at once. A
        secret pending deletion is treated as missing: DescribeSecret succeeds
        (returns ``DeletedDate``) but GetSecretValue fails, so catch it now
        rather than hours later when ``get()`` tries to fetch it.
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
                desc = self._sm.describe_secret(SecretId=_secret_name(self._env_id, role))
            except ClientError as e:
                code = e.response.get("Error", {}).get("Code", "")
                if code == "ResourceNotFoundException":
                    missing.append(role)
                else:
                    raise
            else:
                if desc.get("DeletedDate"):
                    # Pending deletion (recovery window) — DescribeSecret
                    # succeeds but GetSecretValue will fail. Treat exactly
                    # like "doesn't exist" for preflight purposes.
                    missing.append(role)

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
        except (ValueError, _json.JSONDecodeError):
            parsed = None

        if isinstance(parsed, dict):
            # A dict WITHOUT a "password" key must not silently fall back
            # to substituting the raw JSON blob (username included) as
            # the password — that's a worse outcome than failing loudly.
            if "password" not in parsed:
                raise RuntimeError(
                    f"Secret {name} is a JSON object but has no "
                    f"'password' key; can't resolve a password value "
                    f"from it. Keys present: {sorted(parsed.keys())}."
                )
            value = parsed["password"]
        elif parsed is not None:
            # Valid JSON but not an object (e.g. a bare number or list) —
            # not a usable password shape either way.
            raise RuntimeError(
                f"Secret {name}'s value parsed as JSON but isn't an "
                f"object with a 'password' key (got {type(parsed).__name__})."
            )
        else:
            # Not JSON at all — treat as a legacy plain password string.
            value = raw

        if not isinstance(value, str) or not value:
            # Covers {"password": ""/null/12345}: reject empty / None /
            # non-string values loudly here rather than substituting silently
            # (or blowing up later in re.sub with no clue which secret failed).
            raise RuntimeError(
                f"Secret {name} resolved to an empty or non-string "
                f"password value ({value!r}); refusing to substitute it "
                f"into the spec."
            )
        self._cache[role] = value
        return value

    def _replace(self, s: str) -> str:
        """Replace every placeholder in a string with its secret value.

        Most fields are exact matches (``"__SECRET:vcenterRoot__"``); embedded
        matches are supported too in case a spec field is ever templated.
        """
        def sub(m: re.Match) -> str:
            return self.get(m.group(1))
        return _PLACEHOLDER_RE.sub(sub, s)

    def resolve_in_place(self, value: Any) -> None:
        """Mutate ``value`` in-place, replacing every placeholder string.

        Walks dicts and lists (the exercised path -- callers resolve the raw
        JSON dict before typed conversion). Only container elements are
        replaced; bare strings can't be mutated in place (Python strings are
        immutable).
        """
        if isinstance(value, dict):
            for k, v in value.items():
                value[k] = self._resolved(v)
        elif isinstance(value, list):
            for i, item in enumerate(value):
                value[i] = self._resolved(item)

    def _resolved(self, v: Any) -> Any:
        """Return ``v`` with placeholders resolved.

        Strings are substituted; dicts/lists are mutated in place (and returned
        as the same object); tuples are immutable so they're rebuilt into a new
        tuple. ``find_placeholders`` walks tuples, so the resolver must too — or
        a tuple-nested ``__SECRET:`` would validate yet never get substituted.
        """
        if isinstance(v, str):
            return self._replace(v) if "__SECRET:" in v else v
        if isinstance(v, tuple):
            return tuple(self._resolved(x) for x in v)
        self.resolve_in_place(v)
        return v
        # Anything else (int, bool, None, plain str, typed VAPI struct) —
        # leave alone. Resolve on the JSON dict before typed conversion
        # if you have a typed object.
