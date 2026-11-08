"""SSRF controls: URL validation, DNS resolution and the public-address policy.

A URL is only fetched when (1) it parses to http/https on an allowed port with no credentials,
and (2) EVERY address its host resolves to is a public, routable address. The caller then connects
to the validated address itself (never re-resolving), which defeats DNS rebinding. Hosts written as
decimal/hex/octal integers or short IPv4 forms are handled because the check runs on the resolved
addresses, not on the host string.
"""

import ipaddress
import socket
from collections.abc import Callable
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from app.jobs_import import errors

MAX_URL_LENGTH = 2048
IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
Resolver = Callable[[str, int], list[str]]

# Everything that is not an ordinary public unicast address. `is_global` already rejects most of
# these; the explicit list adds IPv6 transition ranges that can embed private IPv4 addresses.
_BLOCKED_NETWORKS = tuple(
    ipaddress.ip_network(n)
    for n in (
        "0.0.0.0/8",
        "10.0.0.0/8",
        "100.64.0.0/10",
        "127.0.0.0/8",
        "169.254.0.0/16",
        "172.16.0.0/12",
        "192.0.0.0/24",
        "192.0.2.0/24",
        "192.88.99.0/24",
        "192.168.0.0/16",
        "198.18.0.0/15",
        "198.51.100.0/24",
        "203.0.113.0/24",
        "224.0.0.0/4",
        "240.0.0.0/4",
        "::/128",
        "::1/128",
        "::ffff:0:0/96",  # IPv4-mapped
        "64:ff9b::/96",  # NAT64
        "64:ff9b:1::/48",
        "100::/64",
        "2001::/32",  # Teredo
        "2001:db8::/32",
        "2002::/16",  # 6to4
        "fc00::/7",
        "fe80::/10",
        "ff00::/8",
    )
)


def is_public_ip(ip: IPAddress) -> bool:
    if any(ip in net for net in _BLOCKED_NETWORKS if net.version == ip.version):
        return False
    return ip.is_global and not (ip.is_multicast or ip.is_reserved or ip.is_unspecified)


@dataclass(frozen=True)
class UrlPolicy:
    """What may be fetched. The default is strict; only tests construct a looser one."""

    allowed_schemes: frozenset[str] = frozenset({"http", "https"})
    allowed_ports: frozenset[int] = frozenset({80, 443})
    ip_allowed: Callable[[IPAddress], bool] = field(default=is_public_ip)


DEFAULT_POLICY = UrlPolicy()


@dataclass(frozen=True)
class Target:
    scheme: str
    host: str  # lower-case ASCII (IDNA), no trailing dot
    port: int
    path: str  # path + query, never a fragment

    @property
    def url(self) -> str:
        default = 443 if self.scheme == "https" else 80
        netloc = f"[{self.host}]" if ":" in self.host else self.host
        if self.port != default:
            netloc = f"{netloc}:{self.port}"
        return f"{self.scheme}://{netloc}{self.path}"


def parse_target(url: str, policy: UrlPolicy = DEFAULT_POLICY) -> Target:
    """Validate and normalise a URL. Raises ImportFailure; never returns an unsafe target."""
    if not isinstance(url, str) or not url or len(url) > MAX_URL_LENGTH:
        raise errors.ImportFailure(errors.INVALID_URL)
    # No whitespace, control characters or backslashes (request splitting / parser confusion).
    if any(ord(c) <= 32 or ord(c) == 127 or c == "\\" for c in url):
        raise errors.ImportFailure(errors.INVALID_URL)
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        raise errors.ImportFailure(errors.INVALID_URL) from None
    if parts.scheme.lower() not in policy.allowed_schemes or not parts.hostname:
        raise errors.ImportFailure(errors.INVALID_URL)
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        raise errors.ImportFailure(errors.INVALID_URL)  # credentials in the URL are never sent
    scheme = parts.scheme.lower()
    if port is None:
        port = 443 if scheme == "https" else 80
    if port not in policy.allowed_ports:
        raise errors.ImportFailure(errors.BLOCKED)
    host = parts.hostname.rstrip(".")
    if "%" in host or not host:
        raise errors.ImportFailure(errors.INVALID_URL)
    try:
        host = host.encode("idna").decode("ascii").lower()
    except UnicodeError:
        raise errors.ImportFailure(errors.INVALID_URL) from None
    if len(host) > 253:
        raise errors.ImportFailure(errors.INVALID_URL)
    path = parts.path or "/"
    if parts.query:
        path = f"{path}?{parts.query}"
    return Target(scheme=scheme, host=host, port=port, path=path)


def system_resolver(host: str, port: int) -> list[str]:
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return [str(info[4][0]) for info in infos]


def resolve_and_check(
    host: str, port: int, resolver: Resolver = system_resolver, policy: UrlPolicy = DEFAULT_POLICY
) -> str:
    """Resolve `host` and return one address to connect to, only if ALL of them are allowed."""
    try:
        addresses = resolver(host, port)
    except (OSError, UnicodeError):
        raise errors.ImportFailure(errors.DNS) from None
    if not addresses:
        raise errors.ImportFailure(errors.DNS)
    parsed: list[IPAddress] = []
    for raw in addresses:
        try:
            parsed.append(ipaddress.ip_address(raw.split("%", 1)[0]))
        except ValueError:
            raise errors.ImportFailure(errors.DNS) from None
    # Every record must pass: a host that also resolves to a private address is rejected outright.
    if not all(policy.ip_allowed(ip) for ip in parsed):
        raise errors.ImportFailure(errors.BLOCKED)
    return str(parsed[0])
