# SPDX-FileCopyrightText: 2026 Veyllo GmbH
# SPDX-License-Identifier: AGPL-3.0-or-later
# Additional permissions and terms under AGPL Section 7: see LICENSING.md
"""classify_address: one answer for every outbound destination check.

The old checks trusted ipaddress.is_global, and Python 3.13 reports the NAT64 and the
IPv4-compatible forms of 127.0.0.1 as global, so a mail server or a fetched URL that
resolved to 64:ff9b::7f00:1 passed as public. The table pins every class the outbound
guards rely on; each row names the mutation it catches."""
import pytest

from vaf.network.binding import assert_ip_safe, classify_address

TABLE = [
    # this machine, in every spelling that reaches it
    ("127.0.0.1", "loopback"),
    ("127.10.20.30", "loopback"),
    ("::1", "loopback"),
    ("::ffff:127.0.0.1", "loopback"),          # MUTATION: drop the ipv4_mapped unwrap
    ("64:ff9b::7f00:1", "loopback"),           # MUTATION: drop the NAT64 unwrap
    ("64:ff9b:1::7f00:1", "loopback"),         # local-use NAT64 prefix
    ("::7f00:1", "loopback"),                  # MUTATION: drop the IPv4-compatible unwrap
    ("2002:7f00:1::", "loopback"),             # MUTATION: drop the 6to4 unwrap
    ("2001:0:4136:e378:8000:63bf:80ff:fffe", "loopback"),  # Teredo client 127.0.0.1
    # LANs and overlay networks
    ("10.1.2.3", "private"),
    ("172.16.0.1", "private"),
    ("192.168.1.1", "private"),
    ("100.64.0.1", "private"),                 # carrier NAT / Tailscale
    ("198.18.0.1", "private"),                 # fake-IP proxies answer from here
    ("fd00::1", "private"),
    ("::ffff:192.168.1.1", "private"),
    # never a destination
    ("169.254.169.254", "forbidden"),          # MUTATION: fold link-local into private
    ("fe80::1", "forbidden"),
    ("fe80::1%eth0", "forbidden"),
    ("0.0.0.0", "forbidden"),
    ("0.1.2.3", "forbidden"),
    ("::", "forbidden"),
    ("224.0.0.1", "forbidden"),
    ("ff02::1", "forbidden"),
    ("255.255.255.255", "forbidden"),
    ("192.0.2.1", "forbidden"),
    ("2001:db8::1", "forbidden"),
    ("fec0::1", "forbidden"),                  # Python 3.13 calls this global
    ("::ffff:169.254.169.254", "forbidden"),
    ("not-an-address", "forbidden"),
    # the internet
    ("8.8.8.8", "public"),
    ("1.1.1.1", "public"),
    ("2606:4700:4700::1111", "public"),
    ("64:ff9b::808:808", "public"),            # MUTATION: refuse every NAT64 address
    ("::ffff:8.8.8.8", "public"),
    ("2002:808:808::1", "public"),
]


@pytest.mark.parametrize("ip,kind", TABLE)
def test_every_destination_class(ip, kind):
    assert classify_address(ip) == kind


def test_the_nat64_form_of_loopback_is_no_longer_a_public_mail_server():
    """The gap the classifier closes for mail: without the opt-in, this passed as global."""
    with pytest.raises(ValueError):
        assert_ip_safe("64:ff9b::7f00:1")


def test_mail_keeps_its_meaning():
    """Loopback and LAN pass only with the mail opt-in (a bridge on this machine);
    forbidden addresses never do."""
    assert_ip_safe("8.8.8.8")
    assert_ip_safe("127.0.0.1", allow_private=True)
    assert_ip_safe("192.168.1.10", allow_private=True)
    for ip in ("127.0.0.1", "192.168.1.10"):
        with pytest.raises(ValueError):
            assert_ip_safe(ip)
    for ip in ("169.254.169.254", "0.0.0.0", "fe80::1"):
        with pytest.raises(ValueError):
            assert_ip_safe(ip, allow_private=True)
