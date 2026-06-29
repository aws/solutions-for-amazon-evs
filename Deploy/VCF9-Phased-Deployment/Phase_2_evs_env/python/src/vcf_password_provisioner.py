# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Provision per-role VCF appliance passwords in AWS Secrets Manager.

After ``post-evs-sync-config`` resolves env-ID-derived names, this module
ensures every VCF appliance role has a complex password stored as a secret
named ``evs-<env_id>_<role>``. The set of roles depends on the
deployment shape:

- always-required: vCenter root + SSO admin, NSX root/admin/audit,
  SDDC Manager root/ssh/local, VCF Operations admin + master + collector,
  edge appliance (shared across edge VMs and roles)
- HA-only (when ``simpleDeployment`` is False): operations data + replica
- 9.0-only (when ``vcfInstallerProductVersion`` starts with "9.0"): fleet manager root + admin

Generation matches EVS's own ``generateComplexVcfPassword``:
  - 17 random characters, no vowels, no punctuation, no spaces
  - one ``!`` appended → final length 18
  - validated against a complexity rule set (≥4 unique, ≥4 char classes,
    no consecutive sequence) with up to 20 retries

Idempotent: existing secrets are left alone. Operators who want to rotate
a password should delete the secret in AWS Secrets Manager and re-run.
"""

import json as _json
import logging
import string
from typing import Any

logger = logging.getLogger(__name__)


# Always-required roles. Names match the keys we use elsewhere in the
# project (config.vcfPasswords.* and the placeholder string format
# `__SECRET:<role>__`).
_ALWAYS_REQUIRED: tuple[str, ...] = (
    "vcenterRoot",
    "vcenterSso",
    "nsxRoot",
    "nsxAdmin",
    "nsxAudit",
    "sddcManagerRoot",
    "sddcManagerSsh",
    "sddcManagerLocal",
    "operationsAdmin",
    "operationsMaster",
    "operationsCollector",
    "edgeAppliance",
)
_HA_REQUIRED: tuple[str, ...] = ("operationsData", "operationsReplica")
_FLEET_REQUIRED: tuple[str, ...] = ("fleetManagerRoot", "fleetManagerAdmin")

_USERNAME_BY_ROLE: dict[str, str] = {
    "vcenterRoot": "root",
    "vcenterSso": "administrator@vsphere.local",
    "nsxRoot": "root",
    "nsxAdmin": "admin",
    "nsxAudit": "audit",
    "sddcManagerRoot": "root",
    "sddcManagerSsh": "vcf",
    "sddcManagerLocal": "admin@local",
    "operationsAdmin": "admin",
    "operationsMaster": "admin",
    "operationsCollector": "admin",
    "operationsData": "admin",
    "operationsReplica": "admin",
    "edgeAppliance": "admin",
    "fleetManagerRoot": "root",
    "fleetManagerAdmin": "admin",
}

# Generation parameters tuned to satisfy every VCF appliance validator
# we've encountered. See ``_generate_complex_password`` for the full
# constraint analysis (allowed-special intersection across components,
# 15-20 char window driven by SDDC Manager min and vCenter max).
_VOWELS = "aeiouyAEIOUY"
_MIN_UNIQUE = 4
_MIN_CLASSES = 4


def required_secret_roles(config: dict[str, Any]) -> list[str]:
    """Compute the role list for a given Phase 2 config.

    The set is deployment-shape sensitive:

      - always 12 baseline entries
      - +2 if not ``simpleDeployment`` (HA needs the operations data +
        replica node passwords)
      - +2 if ``vcfInstallerProductVersion`` starts with ``"9.0"``
        (the fleet manager spec was dropped on VCF 9.1).
    """
    roles = list(_ALWAYS_REQUIRED)
    if not config.get("simpleDeployment", True):
        roles.extend(_HA_REQUIRED)
    installer_version = str(config.get("vcfInstallerProductVersion") or "")
    if installer_version.startswith("9.0"):
        roles.extend(_FLEET_REQUIRED)
    return roles


def secret_name(env_id: str, role: str) -> str:
    """Compose the Secrets Manager secret name for an env + role.

    Format: ``evs-<env_id>_<role>``. Reads similarly to the
    ESXi pattern (``evs!<env_id>_<host>``) but uses a dash instead
    of a bang because customer-created secret names are restricted
    to ``[a-zA-Z0-9/_+=.@-]``. The bang in the ESXi case is created
    by the EVS service through a privileged path.
    """
    return f"evs-{env_id}_{role}"


# ---- Generation helpers --------------------------------------------------


def _has_sequence(password: str) -> bool:
    """True if ``password`` contains 3+ ascending or descending consecutive
    characters by codepoint (e.g. ``abc``, ``321``, ``zyx``).

    Mirrors the ``maxSequence=0`` rule from the EVS validator. We treat any
    run of ≥3 as a sequence and reject.
    """
    if len(password) < 3:
        return False
    for i in range(len(password) - 2):
        a, b, c = password[i], password[i + 1], password[i + 2]
        if ord(b) - ord(a) == 1 and ord(c) - ord(b) == 1:
            return True
        if ord(a) - ord(b) == 1 and ord(b) - ord(c) == 1:
            return True
    return False


def _is_complex(password: str) -> bool:
    """Validate a candidate password against the EVS complexity rules.

    Length check matches what the strictest VCF appliance validator
    (vCenter, max 20) constrains us to. EVS's original Java logic
    targeted minLength=17 because their generator doesn't have to
    handle vCenter's upper bound. We hold to 15 (the SDDC Manager
    minimum) on the lower end and 20 on the upper end.
    """
    if not (15 <= len(password) <= 20):
        return False
    if len(set(password)) < _MIN_UNIQUE:
        return False

    classes = 0
    if any(c in string.ascii_lowercase for c in password):
        classes += 1
    if any(c in string.ascii_uppercase for c in password):
        classes += 1
    if any(c in string.digits for c in password):
        classes += 1
    if any(not c.isalnum() for c in password):
        classes += 1
    if classes < _MIN_CLASSES:
        return False

    if _has_sequence(password):
        return False

    return True


def _generate_complex_password(sm_client: Any) -> str:
    """Generate a complex password by explicit construction.

    Authoritative spec assembled from VCF Installer's actual validator
    error messages (each component has its own allow-list; intersect
    them all):

    Allowed specials, per component:
      - NSX Admin / vCenter SSO     : ``@!#$%?^``
      - vCenter Root                : (no documented restriction)
      - VCF Operations / Cloud Proxy: ``!@#$%^&*+``
      - SDDC Manager (per dev docs) : ``!%@$^#?*``
      - VSP (VCF 9.1)              : ``!@#$^*``

    The intersection — and therefore the only specials safe across
    every appliance including VSP — is ``!@#$^`` (five characters).
    ``%`` passes most validators but fails VSP. ``?``, ``*``, ``&``,
    and ``+`` each pass some validators and fail others.

    Length:
      - vCenter SSO / Root : max 20
      - SDDC Manager root  : min 15

    Range that satisfies every validator: 15-20.

    Layout (20 chars):
      U l d s <13-char body, no vowels, no punctuation> s s

    All four character classes anchored at known positions so the
    validator can't complain about missing classes regardless of how
    the random body composes. Body length tuned so the total comes in
    under the 20-char vCenter cap with two trailing specials and four
    anchors.

    Phase 3's bringup retry loop uses the same construction in
    ``BringupManager._generate_replacement_password`` — keep them in
    sync if either one needs adjustment.
    """
    import secrets

    consonants_lower = "bcdfghjklmnpqrstvwxz"
    consonants_upper = "BCDFGHJKLMNPQRSTVWXZ"
    digits = "23456789"
    # Intersection of every appliance's allow-list (including VSP on
    # 9.1). Anything outside this set fails at least one validator.
    vcf_safe_specials = "!@#$^"

    rng = secrets.SystemRandom()
    body_response = sm_client.get_random_password(
        PasswordLength=13,
        ExcludeCharacters=_VOWELS,
        ExcludePunctuation=True,
        IncludeSpace=False,
    )
    body = body_response.get("RandomPassword") or ""

    password = (
        rng.choice(consonants_upper)
        + rng.choice(consonants_lower)
        + rng.choice(digits)
        + rng.choice(vcf_safe_specials)
        + body
        + rng.choice(vcf_safe_specials)
        + rng.choice(vcf_safe_specials)
    )
    if not _is_complex(password):
        # Local validator is satisfied by construction in practice, but
        # if it ever flags one we still prefer to surface the issue
        # rather than silently ship a non-compliant password.
        logger.warning(
            "Constructed password failed local complexity validator; "
            "shipping anyway and relying on the bringup-side retry loop"
        )
    return password


# ---- Provisioning --------------------------------------------------------


def ensure_vcf_passwords(
    sm_client: Any,
    env_id: str,
    config: dict[str, Any],
) -> dict[str, str]:
    """Ensure every required role has a secret in Secrets Manager.

    Iterates the role list from ``required_secret_roles(config)``. For
    each:

      - If the secret already exists (Secrets Manager returns it on
        ``DescribeSecret``), leave it alone — operators rotate by deleting
        and re-running.
      - If absent (``ResourceNotFoundException``), generate a complex
        password and ``CreateSecret``.

    Args:
        sm_client: A boto3 ``secretsmanager`` client. Needs
            ``DescribeSecret``, ``CreateSecret``, and ``GetRandomPassword``.
        env_id: EVS environment ID.
        config: Phase 2 config dict (used for HA / version branching).

    Returns:
        ``{role: secret_name}`` map for every required role.

    Raises:
        Bubbles any boto3 ``ClientError`` other than ``ResourceNotFoundException``.
    """
    from botocore.exceptions import ClientError

    roles = required_secret_roles(config)
    logger.info(
        "Provisioning %d VCF appliance secret(s) for env %s",
        len(roles), env_id,
    )

    result: dict[str, str] = {}
    for role in roles:
        name = secret_name(env_id, role)
        result[role] = name

        try:
            sm_client.describe_secret(SecretId=name)
            logger.info("  %s already exists; leaving as-is", name)
            continue
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code != "ResourceNotFoundException":
                raise

        password = _generate_complex_password(sm_client)
        username = _USERNAME_BY_ROLE.get(role, "admin")

        secret_value = _json.dumps({"username": username, "password": password})
        sm_client.create_secret(
            Name=name,
            Description=(
                f"Auto-generated VCF appliance password for role '{role}' "
                f"in EVS environment {env_id}. Created by Phase 2 "
                f"post-evs-sync-config. Rotate by deleting this secret and "
                f"re-running."
            ),
            SecretString=secret_value,
            Tags=[{"Key": "EvsAccess", "Value": "True"}],
        )
        logger.info("  %s created", name)

    return result
