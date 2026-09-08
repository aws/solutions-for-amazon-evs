"""Transient-error classification — the single list the retry logic reads.

Consolidates the scattered per-step retry decisions into one place (M6 of
the CI/CD plan). Every entry below is traceable to an observed failure
mode, with its source noted. When the pipeline (or a person) hits a
failure, `is_transient(err)` answers: retry, or is the commit/config bad?

Sources:
- AWS error codes the orchestrator already retries ad hoc
  (vlan_route_table_associator: ThrottlingException/RequestLimitExceeded;
  ebs_volume_manager: attach 404 race; deploy_orchestrator: async-secret
  ResourceNotFound bounded retries).
- EVS host/VLAN failure modes from the EVS operational runbook: metal
  host ICE and Skylark VLAN "internal error, please retry".
- Broadcom depot / ovftool / installer-not-ready network flakes
  (deploy_orchestrator's _ensure_ovftool and installer polling).
"""

from __future__ import annotations

import re

# AWS error codes that are unambiguously transient: capacity or rate
# pressure that clears on its own. Matched against a botocore
# ClientError's Error.Code, or as a substring of the message.
TRANSIENT_ERROR_CODES = frozenset({
    "ThrottlingException",            # already retried in vlan associator
    "RequestLimitExceeded",           # EC2's throttle spelling
    "TooManyRequestsException",
    "InternalError",                  # AWS-side blips
    "InternalFailure",
    "ServiceUnavailable",
    "ServiceUnavailableException",
    "RequestTimeout",
    "RequestTimeoutException",
})

# NOT transient, listed here deliberately so nobody "fixes" a red run by
# adding them: quota exceeded needs a quota bump or a leaked-environment
# cleanup, not a retry; auth/validation failures mean the code or config
# is wrong; and metal ICE (InsufficientInstanceCapacity) will NOT clear by
# resuming in a tight loop -- a bare-metal host needs a real On-Demand
# Capacity Reservation (or a different AZ), so retrying just burns ~45 min
# per attempt re-provisioning. Fail fast and surface it to a human.
NON_TRANSIENT_ERROR_CODES = frozenset({
    "InsufficientInstanceCapacity",
    "InsufficientCapacityException",
    "ServiceQuotaExceededException",
    "LimitExceededException",
    "AccessDeniedException",
    "UnauthorizedOperation",
    "ValidationException",
})

# EVS reports host/VLAN failure causes as free text in stateDetails.
# These phrases mark the retryable ones. Note: metal-host ICE ("insufficient
# capacity") is deliberately NOT here -- it is fatal (see
# NON_TRANSIENT_ERROR_CODES). Only the genuinely self-clearing Skylark VLAN
# "internal error, please retry" transients remain.
TRANSIENT_STATE_DETAIL_PATTERNS = (
    re.compile(r"internal error.{0,40}(please )?retry", re.IGNORECASE),
)

# Network-flake patterns from the non-AWS legs of a run: the Broadcom
# depot, the OVA upload (ovftool), and the VCF Installer coming up.
TRANSIENT_MESSAGE_PATTERNS = (
    re.compile(r"depot.{0,60}(timed? ?out|unavailable|reset)", re.IGNORECASE),
    re.compile(r"ovftool.{0,60}(connection (reset|refused|aborted)|timed? ?out)", re.IGNORECASE),
    re.compile(r"connection (reset|aborted|refused) by peer", re.IGNORECASE),
    re.compile(r"read timed out", re.IGNORECASE),
    re.compile(r"installer not (ready|reachable)", re.IGNORECASE),
    re.compile(r"temporary failure in name resolution", re.IGNORECASE),
)


def _error_code(err: object) -> str | None:
    """Extract an AWS error code from a botocore-style exception, if any."""
    response = getattr(err, "response", None)
    if isinstance(response, dict):
        return (response.get("Error") or {}).get("Code")
    return None


def is_transient(err: object) -> bool:
    """True if this failure should be retried rather than treated as a
    bad commit/config.

    Accepts an exception (botocore ClientError or anything else) or a
    plain string (e.g. an EVS host stateDetails value).
    """
    code = _error_code(err)
    if code:
        if code in NON_TRANSIENT_ERROR_CODES:
            return False
        if code in TRANSIENT_ERROR_CODES:
            return True

    text = str(err)
    # Explicit non-transient codes win even in text form.
    for bad in NON_TRANSIENT_ERROR_CODES:
        if bad in text:
            return False
    for good in TRANSIENT_ERROR_CODES:
        if good in text:
            return True
    for pattern in TRANSIENT_STATE_DETAIL_PATTERNS + TRANSIENT_MESSAGE_PATTERNS:
        if pattern.search(text):
            return True
    return False
