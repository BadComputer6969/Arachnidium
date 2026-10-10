"""Tests for check_dns_blocklist: O(label-depth) parent-domain matching."""

import pytest

import addon


@pytest.fixture(autouse=True)
def blocklist():
    addon.DNS_BLOCKLIST = {"ads.example.com", "tracker.net", "cdn.foo.org"}
    yield
    addon.DNS_BLOCKLIST = set()


def test_exact_match():
    assert addon.check_dns_blocklist("ads.example.com")


def test_subdomain_match():
    assert addon.check_dns_blocklist("a.ads.example.com")
    assert addon.check_dns_blocklist("deep.sub.tracker.net")


def test_parent_of_entry_does_not_match():
    # "example.com" is the parent of the entry, not a subdomain of it.
    assert not addon.check_dns_blocklist("example.com")
    assert not addon.check_dns_blocklist("net")


def test_no_match():
    assert not addon.check_dns_blocklist("example.org")
    assert not addon.check_dns_blocklist("notads.example.com")
    assert not addon.check_dns_blocklist("ads.example.com.evil.com")


def test_case_insensitive():
    assert addon.check_dns_blocklist("ADS.EXAMPLE.COM")
    assert addon.check_dns_blocklist("A.Tracker.Net")


def test_trailing_dot():
    assert addon.check_dns_blocklist("ads.example.com.")


def test_empty_host_or_empty_set():
    assert not addon.check_dns_blocklist("")
    assert not addon.check_dns_blocklist(".")
    addon.DNS_BLOCKLIST = set()
    assert not addon.check_dns_blocklist("ads.example.com")


def test_ip_literal_never_matches_domain_entries():
    assert not addon.check_dns_blocklist("1.2.3.4")
    assert not addon.check_dns_blocklist("93.184.216.34")


def test_matches_original_semantics_on_random_hosts():
    """Cross-check the new implementation against the old O(n) scan."""
    import random

    random.seed(42)
    entries = {"ads.example.com", "tracker.net", "a.b.c.d", "x.io"}
    addon.DNS_BLOCKLIST = set(entries)

    def old(host):
        return any((host == s or host.endswith("." + s)) for s in entries)

    labels = ["ads", "tracker", "cdn", "www", "a", "b", "x", "evIl"]
    zones = ["example.com", "net", "b.c.d", "io", "other.org"]
    for _ in range(2000):
        host = ".".join(
            random.choice(labels) for _ in range(random.randint(1, 4))
        ) + "." + random.choice(zones)
        # old semantics are case-sensitive; compare on lowered hosts
        host = host.lower()
        assert addon.check_dns_blocklist(host) == old(host), host
