# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Fetch SHA-256 SSL thumbprints from ESXi hosts.

The VCF Installer UI (and the POST /v1/sddcs validator on 9.0.2) require an
``sslThumbprint`` on each host spec even when ``skipEsxThumbprintValidation``
is set. The server-side code honors the skip flag, but the UI validates the
field's presence anyway and the API validator follows suit.

Rather than make the operator run ``openssl s_client`` by hand, we grab the
cert directly over the wire during ``post-evs-sync-config`` — the host is
already up, we already know its FQDN, and no credentials are needed to pull
a public cert.
"""

import hashlib
import logging
import socket
import ssl
from typing import Any

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


def fetch_all_host_thumbprints(
    config: dict[str, Any],
) -> dict[str, str]:
    """Fetch SSL thumbprints for every ESXi host listed in the config.

    Reads ``esxiHostnames`` (short names) and ``fqdn`` from the config,
    connects to each full FQDN over 443, and returns a dict mapping the
    short hostname to its thumbprint.

    Args:
        config: Phase 2 config dict.

    Returns:
        Dict of ``{short_hostname: thumbprint}`` for every host we could
        reach. Hosts that fail (network, TLS, etc.) raise — we don't silently
        swallow, because a missing thumbprint downstream will surface as a
        spec validation error anyway.
    """
    fqdn_suffix = config.get("fqdn", "")
    esxi_hostnames = config.get("esxiHostnames", [])
    if not fqdn_suffix:
        raise RuntimeError(
            "config.fqdn is required to fetch thumbprints. "
            "Run pre-evs-sync-config first."
        )
    if not esxi_hostnames:
        logger.warning("No esxiHostnames in config; skipping thumbprint fetch")
        return {}

    results: dict[str, str] = {}
    for short in esxi_hostnames:
        fqdn = f"{short}.{fqdn_suffix}"
        logger.info("Fetching SSL thumbprint from %s", fqdn)
        thumbprint = fetch_ssl_thumbprint(fqdn)
        results[short] = thumbprint
        logger.info("  %s -> %s", short, thumbprint)
    return results
