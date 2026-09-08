"""Unit tests for spec-generator/cidr.py — the IP-derivation rules.

These three functions decide the gateway and IP-pool range for every
VLAN pool in a generated spec, so an off-by-one here silently corrupts
every deployment's network layout.
"""

import pytest

from spec_generator.cidr import first_usable, sixth_from_end, tenth_host


class TestFirstUsable:
    def test_slash_24(self):
        assert first_usable("10.0.30.0/24") == "10.0.30.1"

    def test_slash_28(self):
        assert first_usable("192.168.1.16/28") == "192.168.1.17"

    def test_slash_16(self):
        assert first_usable("172.16.0.0/16") == "172.16.0.1"

    def test_non_network_address_input_normalises(self):
        # strict=False lets a host address stand in for its network.
        assert first_usable("10.0.30.55/24") == "10.0.30.1"


class TestTenthHost:
    def test_slash_24(self):
        assert tenth_host("10.0.30.0/24") == "10.0.30.10"

    def test_slash_16_stays_in_first_octet_range(self):
        assert tenth_host("172.16.0.0/16") == "172.16.0.10"

    def test_non_network_address_input_normalises(self):
        assert tenth_host("10.0.30.200/24") == "10.0.30.10"


class TestSixthFromEnd:
    def test_slash_24(self):
        # broadcast .255 - 5 = .250
        assert sixth_from_end("10.0.30.0/24") == "10.0.30.250"

    def test_slash_28(self):
        # network .16, broadcast .31, -5 = .26
        assert sixth_from_end("192.168.1.16/28") == "192.168.1.26"

    def test_slash_16_crosses_octet(self):
        # broadcast 172.16.255.255 - 5 = 172.16.255.250
        assert sixth_from_end("172.16.0.0/16") == "172.16.255.250"


class TestPoolOrdering:
    """The derived values must lay out gateway < pool-start < pool-end."""

    @pytest.mark.parametrize(
        "cidr", ["10.0.30.0/24", "192.168.1.16/28", "172.16.0.0/16", "10.99.0.0/26"]
    )
    def test_gateway_below_pool_start_below_pool_end(self, cidr):
        import ipaddress

        gw = ipaddress.ip_address(first_usable(cidr))
        start = ipaddress.ip_address(tenth_host(cidr))
        end = ipaddress.ip_address(sixth_from_end(cidr))
        # On small subnets (/28) the pool compresses to one address, so
        # start == end is legitimate; the gateway must stay below it.
        assert gw < start <= end

    def test_slash_28_pool_is_usable(self):
        # Smallest pool the layout rules leave room in: /28 gives
        # .26 - .26 after gateway/offsets — still a valid single-address pool.
        import ipaddress

        start = ipaddress.ip_address(tenth_host("192.168.1.16/28"))
        end = ipaddress.ip_address(sixth_from_end("192.168.1.16/28"))
        assert start <= end
