"""CIDR helpers mirroring the automation's IP-derivation rules.

Given a per-pool CIDR, the automation derives the gateway and the DHCP /
static IP-pool range deterministically. These functions reproduce that
so a manually-built spec lays out identically to an automated one.
"""

import ipaddress


def first_usable(cidr):
    """First usable host address (network + 1). Used as the gateway."""
    net = ipaddress.ip_network(cidr, strict=False)
    return str(net.network_address + 1)


def tenth_host(cidr):
    """Network address + 10 — the start of the IP-pool range."""
    net = ipaddress.ip_network(cidr, strict=False)
    return str(net.network_address + 10)


def sixth_from_end(cidr):
    """Broadcast address - 5 — the end of the IP-pool range."""
    net = ipaddress.ip_network(cidr, strict=False)
    return str(net.broadcast_address - 5)
