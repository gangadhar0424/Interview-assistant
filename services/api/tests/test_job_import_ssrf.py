"""SSRF suite (Phase 7C): address policy, URL validation, resolution, no-connection guarantee."""

import ipaddress
import socket
from typing import Any

import pytest

from app.jobs_import import errors
from app.jobs_import.errors import ImportFailure
from app.jobs_import.fetch import SafeFetcher
from app.jobs_import.ssrf import (
    DEFAULT_POLICY,
    MAX_URL_LENGTH,
    is_public_ip,
    parse_target,
    resolve_and_check,
)

BLOCKED_IPS = [
    "127.0.0.1", "127.1.2.3", "127.255.255.255", "10.0.0.1", "10.255.255.255", "172.16.0.1",
    "172.31.255.255", "192.168.0.1", "192.168.255.255", "169.254.169.254", "169.254.0.1",
    "100.64.0.1", "100.127.255.255", "0.0.0.0", "0.1.2.3", "224.0.0.1", "239.255.255.255",
    "240.0.0.1", "255.255.255.255", "192.0.0.1", "192.0.2.1", "198.18.0.1", "198.51.100.1",
    "203.0.113.1", "::1", "::", "fe80::1", "fc00::1", "fd12:3456::1", "ff02::1",
    "::ffff:127.0.0.1", "::ffff:10.0.0.1", "::ffff:169.254.169.254", "::ffff:8.8.8.8",
    "64:ff9b::7f00:1", "2002:7f00:1::1", "2002:a00:1::1", "2001:db8::1", "2001::1",
]  # fmt: skip
PUBLIC_IPS = [
    "8.8.8.8", "1.1.1.1", "93.184.216.34", "172.15.255.255", "172.32.0.1", "100.63.255.255",
    "100.128.0.1", "11.0.0.1", "169.253.0.1", "192.167.255.255", "192.169.0.1", "126.255.255.255",
    "128.0.0.1", "2606:4700:4700::1111", "2001:4860:4860::8888", "2a00:1450:4001::1",
]  # fmt: skip


@pytest.mark.parametrize("ip", BLOCKED_IPS)
def test_non_public_addresses_are_blocked(ip: str) -> None:
    assert is_public_ip(ipaddress.ip_address(ip)) is False


@pytest.mark.parametrize("ip", PUBLIC_IPS)
def test_public_addresses_including_range_boundaries_are_allowed(ip: str) -> None:
    assert is_public_ip(ipaddress.ip_address(ip)) is True


# ---------------------------------------------------------------- URL parsing


@pytest.mark.parametrize(
    "url",
    [
        "", "   ", "example.com/job", "//example.com/job", "http://", "https:///path",
        "ftp://example.com/x", "file:///etc/passwd", "gopher://example.com/", "data:text/html,hi",
        "javascript:alert(1)", "ws://example.com/", "mailto:a@b.c",
        "http://user:pw@example.com/", "http://user@example.com/", "http://example.com@evil.com/",
        "http://:pw@example.com/", "http://exa mple.com/", "http://example.com/a b",
        "http://example.com/\r\nHost: evil.test", "http://example.com/a\nb",
        "http://example.com\\@evil.com/", "http://example.com/\\x", "http://exa\x00mple.com/",
        "http://example.com:notaport/", "http://example.com:99999/", "http://example.com:-1/",
        "http://[::1/", "http://%65xample.com/", "https://" + "a" * 300 + ".com/",
        "http://example.com/" + "a" * MAX_URL_LENGTH,
    ],
)  # fmt: skip
def test_malformed_and_dangerous_urls_are_rejected(url: str) -> None:
    with pytest.raises(ImportFailure) as exc:
        parse_target(url)
    assert exc.value.reason in {errors.INVALID_URL, errors.BLOCKED}


@pytest.mark.parametrize(
    "port", [0, 21, 22, 25, 81, 3306, 5432, 6379, 8000, 8080, 8443, 9200, 11211]
)
def test_only_ports_80_and_443_are_allowed(port: int) -> None:
    with pytest.raises(ImportFailure) as exc:
        parse_target(f"http://example.com:{port}/")
    assert exc.value.reason == errors.BLOCKED


def test_non_string_urls_are_rejected() -> None:
    for bad in (None, 5, b"http://example.com"):
        with pytest.raises(ImportFailure):
            parse_target(bad)  # type: ignore[arg-type]


def test_valid_urls_are_normalised() -> None:
    t = parse_target("HTTPS://Jobs.Example.COM./careers/42?ref=a&b=c#fragment")
    assert (t.scheme, t.host, t.port) == ("https", "jobs.example.com", 443)
    assert t.path == "/careers/42?ref=a&b=c"  # fragment dropped, query kept
    assert (
        parse_target("http://example.com").path == "/"
        and parse_target("http://example.com").port == 80
    )
    assert parse_target("http://example.com:80/x").port == 80
    assert parse_target("https://example.com:443/x").port == 443
    assert parse_target("https://bücher.example/x").host == "xn--bcher-kva.example"


def test_target_url_round_trips_without_credentials() -> None:
    assert parse_target("https://example.com/a?b=1").url == "https://example.com/a?b=1"


# ---------------------------------------------------------------- resolution


def fake(addresses: list[str]) -> Any:
    return lambda host, port: addresses


@pytest.mark.parametrize(
    "literal_host",
    ["2130706433", "0x7f000001", "0x7f.0.0.1", "017700000001", "0177.0.0.1", "127.1", "127.0.1",
     "0", "0.0.0.0", "localhost", "LOCALHOST", "ip6-localhost", "metadata.google.internal"],
)  # fmt: skip
def test_alternative_ip_spellings_are_blocked_because_the_resolved_address_is_checked(
    literal_host: str,
) -> None:
    # What a Unix resolver returns for these spellings: loopback / link-local / unspecified.
    for resolved in ("127.0.0.1", "::1", "0.0.0.0", "169.254.169.254", "10.0.0.1"):
        with pytest.raises(ImportFailure) as exc:
            resolve_and_check(literal_host, 80, fake([resolved]))
        assert exc.value.reason == errors.BLOCKED


def test_a_host_with_any_private_record_is_blocked() -> None:
    for records in (["8.8.8.8", "10.0.0.1"], ["10.0.0.1", "8.8.8.8"], ["2606:4700::1", "::1"],
                    ["8.8.8.8", "::ffff:127.0.0.1"]):  # fmt: skip
        with pytest.raises(ImportFailure) as exc:
            resolve_and_check("mixed.example", 80, fake(records))
        assert exc.value.reason == errors.BLOCKED


def test_public_hosts_resolve_to_a_connectable_address() -> None:
    assert resolve_and_check("ok.example", 443, fake(["93.184.216.34"])) == "93.184.216.34"
    assert resolve_and_check("ok.example", 443, fake(["2606:4700:4700::1111"])) == (
        "2606:4700:4700::1111"
    )


def test_resolution_failures_are_failures_not_passes() -> None:
    def boom(host: str, port: int) -> list[str]:
        raise socket.gaierror("no such host")

    for resolver in (boom, fake([]), fake(["not-an-ip"]), fake([""])):
        with pytest.raises(ImportFailure) as exc:
            resolve_and_check("x.example", 80, resolver)
        assert exc.value.reason == errors.DNS


def test_zone_ids_are_stripped_before_the_check() -> None:
    with pytest.raises(ImportFailure) as exc:
        resolve_and_check("x.example", 80, fake(["fe80::1%eth0"]))
    assert exc.value.reason == errors.BLOCKED


# ------------------------------------------------ nothing is connected when blocked


@pytest.fixture
def no_network(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    attempts: list[Any] = []

    def refuse(address: Any, *args: Any, **kwargs: Any) -> None:
        attempts.append(address)
        raise AssertionError("a connection was attempted to a blocked target")

    monkeypatch.setattr(socket, "create_connection", refuse)
    return attempts


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1/", "http://127.0.0.1:80/admin", "http://[::1]/", "http://10.0.0.1/",
        "http://192.168.1.1/", "http://172.16.0.1/", "http://169.254.169.254/latest/meta-data/",
        "http://[::ffff:127.0.0.1]/", "http://[fe80::1]/", "http://0.0.0.0/", "http://localhost/",
        "http://LOCALHOST./", "https://127.0.0.1/", "http://127.0.0.1:8080/", "http://127.0.0.1:22/",
        "http://100.64.0.1/", "http://[fd00::1]/", "http://224.0.0.1/",
    ],
)  # fmt: skip
def test_blocked_targets_never_open_a_connection(url: str, no_network: list[Any]) -> None:
    fetcher = SafeFetcher()  # strict default policy and the real system resolver
    with pytest.raises(ImportFailure) as exc:
        fetcher.fetch(url)
    assert exc.value.reason in {errors.BLOCKED, errors.DNS, errors.INVALID_URL}
    assert no_network == []


def test_the_default_policy_is_strict() -> None:
    assert DEFAULT_POLICY.allowed_schemes == frozenset({"http", "https"})
    assert DEFAULT_POLICY.allowed_ports == frozenset({80, 443})
    assert DEFAULT_POLICY.ip_allowed is is_public_ip
    assert SafeFetcher().policy is DEFAULT_POLICY
