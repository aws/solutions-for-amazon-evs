# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Fetch SHA-256 SSL thumbprints from ESXi hosts.

The VCF Installer UI (and the POST /v1/sddcs validator on 9.0.2) require an
``sslThumbprint`` on each host spec even when ``skipEsxThumbprintValidation``
is set. The server-side code honors the skip flag, but the UI and the REST
validator require the field anyway.

Phase 3 runs on a box that can reach the ESXi hosts (the Installer VM or a
VPC-internal workstation). Phase 2's workstation usually can't resolve
``esxi*.<fqdn>``, so we fetch at POST time rather than at spec-build time.
"""

import hashlib
import logging
import socket
import ssl

logger = logging.getLogger(__name__)


def fetch_ssl_thumbprint(
    host: str, port: int = 443, timeout: int = 10,
) -> str:
    """Fetch the SHA-256 fingerprint of a host's TLS certificate.

    Args:
        host: Hostname or IP.
        port: TLS port (default 443).
        timeout: Connect timeout in seconds.

    Returns:
        SHA-256 fingerprint in uppercase colon-separated hex, e.g.
        ``"3D:D0:EE:B5:A0:CC:45:..."``. This is the format the VCF Installer
        expects on ``hostSpecs[].sslThumbprint``.
    """
    # CERT_NONE: ESXi's default cert is self-signed. We aren't verifying
    # identity — we just want to read the cert bytes off the wire to fingerprint.
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

    with socket.create_connection((host, port), timeout=timeout) as sock:
        with ctx.wrap_socket(sock, server_hostname=host) as tls:
            der = tls.getpeercert(binary_form=True)

    if not der:
        raise RuntimeError(
            f"Empty TLS certificate returned by {host}:{port}"
        )

    digest = hashlib.sha256(der).hexdigest().upper()
    return ":".join(digest[i:i + 2] for i in range(0, len(digest), 2))
