# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Redact sensitive fields from HTTP wire debug log output."""

import re

_SENSITIVE_RE = re.compile(
    r'("(?:password|Password|secretString|credentials|token|downloadToken'
    r'|rootPassword|sshPassword|localUserPassword|adminPassword'
    r'|nsxtAdminPassword|nsxtAuditPassword|rootNsxtManagerPassword'
    r'|rootUserPassword|adminUserPassword|rootVcenterPassword'
    r'|adminUserSsoPassword)'
    r'"\s*:\s*)"[^"]*"',
    re.IGNORECASE,
)


def redact_body(body: str) -> str:
    """Replace values of known sensitive JSON fields with ***REDACTED***."""
    return _SENSITIVE_RE.sub(r'\1"***REDACTED***"', body)
