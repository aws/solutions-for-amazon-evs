# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Redact sensitive fields from HTTP wire debug log output."""

import re

_SENSITIVE_RE = re.compile(
    r'("(?:password|Password|secretString|credentials|token|downloadToken'
    r'|rootPassword|sshPassword|localUserPassword|adminPassword'
    r'|nsxtAdminPassword|nsxtAuditPassword|rootNsxtManagerPassword'
    r'|rootUserPassword|adminUserPassword|rootVcenterPassword'
    r'|adminUserSsoPassword|systemUserPassword'
    # snake_case variants — the NSX SDK serializes model fields in snake_case
    # on the wire (unlike the camelCase VCF Installer API).
    r'|cli_password|audit_password|root_password'
    # Broad by design: earlier versions enumerated field names and kept missing
    # new ones (systemUserPassword, edge *_password), leaking them in plaintext
    # under *_DEBUG=1. Suffix patterns catch any *password/*Password field...
    r'|[\w-]*[Pp]assword\w*'
    # ...and any *token/*Token field (accessToken, refresh_token, ...).
    r'|[A-Za-z_]*[Tt]oken'
    r')'
    # Value is a JSON string: consume escaped chars (\" \\) so an embedded
    # escaped quote doesn't end the match early and leak the tail.
    r'"\s*:\s*)"(?:\\.|[^"\\])*"',
    re.IGNORECASE,
)



def redact_body(body: str) -> str:
    """Replace values of known sensitive JSON fields with ***REDACTED***."""
    return _SENSITIVE_RE.sub(r'\1"***REDACTED***"', body)
