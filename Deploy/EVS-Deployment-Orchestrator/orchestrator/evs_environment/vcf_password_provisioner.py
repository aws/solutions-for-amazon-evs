# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Provision per-role VCF appliance passwords in AWS Secrets Manager.

After ``post-evs-sync-config`` resolves env-ID-derived names, ensure every
VCF appliance role has a complex password stored as a secret named
``evs-<env_id>_<role>``. The role set is deployment-shape sensitive:
an always-required baseline, HA-only roles when ``simpleDeployment`` is
False, and fleet-manager roles when ``vcfInstallerProductVersion`` starts
with "9.0".

Generation (``_generate_complex_password``): a fixed 19-char layout checked
against the full 12-rule ``password_validation_rule_engine`` (a port of the
VCF 5.x Java validator), retrying up to ``_PASSWORD_GENERATION_LIMIT`` (20).
Retry is load-bearing: ~half of single draws fail the full rule set, and a
stored non-compliant password would only be rejected ~3h into bringup.

``regenerate_role_password`` overwrites a single role's secret after VCF
Installer rejects it during bringup. It loads the rule engine by file path
because Phase 3 runs with Phase 2's directory excluded from ``sys.path``.

Idempotent (``ensure_vcf_passwords``): existing secrets are left alone;
rotate by deleting the secret and re-running. ``regenerate_role_password``
always overwrites, since it's only called after the value was just rejected.
"""

import json as _json
import logging
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

# Generation parameters tuned to satisfy every VCF appliance validator.
# See ``_generate_complex_password`` for the full constraint analysis
# (allowed-special intersection, 15-20 char window).
_VOWELS = "aeiouyAEIOUY"
_MIN_UNIQUE = 4
_MIN_CLASSES = 4
# Matches the Java PasswordValidationRuleEngine retry limit — enough that
# giving up is rare, given the ~50% single-draw failure rate against the
# full rule engine for this fixed layout.
_PASSWORD_GENERATION_LIMIT = 20


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


# Lazy module-level cache: _load_rule_engine() runs for every candidate
# password in the generation retry loop (hundreds of times per deployment),
# so exec the file once per process and reuse.
_RULE_ENGINE_MODULE = None


def _load_rule_engine():
    """Load ``password_validation_rule_engine`` by path relative to this
    file, not via ``sys.path``/package resolution.

    This module loads two ways: normally (Phase 2 on ``sys.path``) or by
    file path from ``bringup_manager`` — and Phase 3 runs with Phase 2's
    ``sys.path`` entry excluded, so a plain import raises ModuleNotFoundError
    there (confirmed). Loading relative to ``__file__`` works either way.
    Result cached in ``_RULE_ENGINE_MODULE`` (exec'd once per process).
    """
    global _RULE_ENGINE_MODULE
    if _RULE_ENGINE_MODULE is not None:
        return _RULE_ENGINE_MODULE

    import importlib.util
    from pathlib import Path

    module_path = Path(__file__).resolve().parent / "password_validation_rule_engine.py"
    spec = importlib.util.spec_from_file_location("password_validation_rule_engine", module_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    _RULE_ENGINE_MODULE = module
    return module


def _has_sequence(password: str) -> bool:
    """Deprecated: use password_validation_rule_engine.has_monotonic_sequence.

    Kept as a thin wrapper (not removed outright) in case any external
    caller imported this private-by-convention name directly.
    """
    engine = _load_rule_engine()
    return engine.has_monotonic_sequence(password, max_sequence=0)


def _is_complex(password: str) -> bool:
    """Validate a candidate password against the full 12-rule engine.

    Delegates to ``password_validation_rule_engine.is_password_complex()``
    (the VCF 5.x Java port) over the 15-20 char window, min 4 unique, min 4
    classes, max_sequence=0. NO_VOWELS is intentionally excluded: the
    generator already excludes vowels at the character-set level, and
    enforcing it as a hard rule would reject any body chunk still carrying a
    leftover vowel from Secrets Manager's GetRandomPassword.
    """
    engine = _load_rule_engine()

    if not (15 <= len(password) <= 20):
        return False

    rules = [
        engine.PasswordValidationRule.NO_REPETITIVE,
        engine.PasswordValidationRule.NO_CONTINUOUS,
        engine.PasswordValidationRule.UPPERCASE,
        engine.PasswordValidationRule.LOWERCASE,
        engine.PasswordValidationRule.SPECIAL,
        engine.PasswordValidationRule.NUMBER,
        engine.PasswordValidationRule.NO_KEYBOARD_PATTERNS,
        engine.PasswordValidationRule.MIN_UNIQUE,
        engine.PasswordValidationRule.MIN_CLASS,
        engine.PasswordValidationRule.MAX_SEQUENCE,
    ]
    config = engine.PasswordPolicyConfig(
        min_length=15,  # already checked above via the 15-20 window; rules list omits LENGTH
        min_unique=_MIN_UNIQUE,
        min_class=_MIN_CLASSES,
        max_sequence=0,
    )
    is_valid, _reasons = engine.is_password_complex(password, rules, config)
    return is_valid


def _generate_complex_password(sm_client: Any, context: str = "") -> str:
    """Generate a complex password by explicit construction, retrying on
    local validation failure.

    Authoritative spec assembled from VCF Installer's actual validator error
    messages — each component has its own special-char allow-list; intersect
    them all:

      - NSX Admin / vCenter SSO     : ``@!#$%?^``
      - vCenter Root                : (no documented restriction)
      - VCF Operations / Cloud Proxy: ``!@#$%^&*+``
      - SDDC Manager (per dev docs) : ``!%@$^#?*``
      - VSP (VCF 9.1)              : ``!@#$^*``

    Intersection — the only specials safe across every appliance including
    VSP — is ``!@#$^``. ``%`` fails VSP; ``?``, ``*``, ``&``, ``+`` each fail
    some validator.

    Length window 15-20 (vCenter max 20, SDDC Manager min 15). Layout (19
    chars): ``U l d s <13-char body, no vowels, no punctuation> s s`` — all
    four classes anchored at known positions so the validator can't complain
    about missing classes regardless of how the random body composes.

    Retry loop: this layout fails the full rule set on ~half of draws, so
    retry up to ``_PASSWORD_GENERATION_LIMIT``; if all fail, raise to fail
    fast here rather than ~3h into bringup. ``context`` (role name) is in the
    error for diagnosability.
    """
    import secrets

    consonants_lower = "bcdfghjklmnpqrstvwxz"
    consonants_upper = "BCDFGHJKLMNPQRSTVWXZ"
    digits = "23456789"
    # Intersection of every appliance's allow-list (including VSP on
    # 9.1). Anything outside this set fails at least one validator.
    vcf_safe_specials = "!@#$^"

    rng = secrets.SystemRandom()
    password = ""
    for attempt in range(_PASSWORD_GENERATION_LIMIT):
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
        if _is_complex(password):
            if attempt > 0:
                logger.info(
                    "Generated a complexity-passing password after %d retry/retries",
                    attempt,
                )
            return password
        logger.info(
            "Generated password failed local complexity validator "
            "(attempt %d/%d) — retrying",
            attempt + 1, _PASSWORD_GENERATION_LIMIT,
        )

    # Fail fast at provisioning time rather than storing a known-non-
    # compliant password that the VCF Installer's own validator would
    # reject hours into bringup.
    raise RuntimeError(
        f"Exhausted {_PASSWORD_GENERATION_LIMIT} password generation "
        f"attempts without a fully-compliant password"
        f"{f' for {context!r}' if context else ''}; refusing to store a "
        f"known-non-compliant password (the VCF Installer would reject "
        f"it during bringup)."
    )


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
            desc = sm_client.describe_secret(SecretId=name)
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code != "ResourceNotFoundException":
                raise
            desc = None

        if desc is not None:
            if not desc.get("DeletedDate"):
                logger.info("  %s already exists; leaving as-is", name)
                continue
            # Pending deletion (recovery window): DescribeSecret reports a
            # DeletedDate but GetSecretValue fails. Restore + write a fresh
            # password: treating "exists" as usable breaks rotation, and
            # create_secret fails "already scheduled for deletion" while pending.
            logger.info(
                "  %s is pending deletion (rotation in progress) — "
                "restoring with a fresh password", name,
            )
            # Generate BEFORE restoring so a generation failure can't
            # leave the secret restored with its old (deleted) value.
            password = _generate_complex_password(sm_client, context=role)
            username = _USERNAME_BY_ROLE.get(role, "admin")
            sm_client.restore_secret(SecretId=name)
            try:
                sm_client.put_secret_value(
                    SecretId=name,
                    SecretString=_json.dumps({"username": username, "password": password}),
                )
            except Exception:
                # Restore succeeded but the write failed: the secret is now
                # restored with its OLD password, which the next run's "already
                # exists" branch would keep forever. Re-schedule deletion so the
                # next run re-enters rotation, then re-raise to fail loudly.
                try:
                    sm_client.delete_secret(SecretId=name)
                except Exception:
                    logger.exception(
                        "  %s: failed to re-schedule deletion after "
                        "put_secret_value failure; secret is restored "
                        "with its OLD password — delete it manually and "
                        "re-run to rotate", name,
                    )
                raise
            logger.info("  %s restored with a new password", name)
            continue

        password = _generate_complex_password(sm_client, context=role)
        username = _USERNAME_BY_ROLE.get(role, "admin")

        secret_value = _json.dumps({"username": username, "password": password})
        try:
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
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code == "ResourceExistsException":
                # TOCTOU: a concurrent run created this secret between our
                # describe_secret and this create_secret. Desired state is met
                # and our generated password was never used (resolution reads
                # the stored value), so don't crash over a self-resolved race.
                logger.info(
                    "  %s already created by a concurrent run; "
                    "leaving as-is", name,
                )
            else:
                raise

    return result


def regenerate_role_password(sm_client: Any, env_id: str, role: str) -> str:
    """Regenerate the password for a single role's existing secret.

    Used when VCF Installer rejects a specific appliance's password during
    bringup: regenerate just that role's secret and retry the same workflow
    (which re-reads the placeholder on the next spec load). Unlike
    ``ensure_vcf_passwords`` this ALWAYS overwrites — the value was just
    rejected — via ``PutSecretValue``; raises if the secret doesn't exist,
    since regenerating a never-provisioned role signals a deeper bug.

    Args:
        sm_client: A boto3 ``secretsmanager`` client. Needs
            ``PutSecretValue`` and ``GetRandomPassword``.
        env_id: EVS environment ID.
        role: One of the role names from ``required_secret_roles``
            (e.g. ``"sddcManagerLocal"``).

    Returns:
        The new password (also stored in Secrets Manager).

    Raises:
        Bubbles ``ResourceNotFoundException`` if the secret doesn't exist
        yet, and any other boto3 ``ClientError``.
    """
    name = secret_name(env_id, role)
    username = _USERNAME_BY_ROLE.get(role, "admin")
    password = _generate_complex_password(sm_client, context=role)

    secret_value = _json.dumps({"username": username, "password": password})
    sm_client.put_secret_value(SecretId=name, SecretString=secret_value)
    logger.info("Regenerated password for role '%s' (secret %s)", role, name)
    return password
