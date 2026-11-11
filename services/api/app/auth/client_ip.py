"""Client-address key for per-client auth limits (Phase 13A Batch 2, D-54).

Forwarded headers are attacker-controlled unless a proxy we operate sets them, so they are read
ONLY when `trusted_proxy` is enabled. The API must then also run with `uvicorn --no-proxy-headers`
so `peer` is the real TCP peer and this function is the single place that interprets forwarding
headers (uvicorn's default would silently rewrite the peer from a forged header; see the real-socket
test and docs/OPERATIONS.md).

Returns a normalized IP string, or `None` when no trustworthy per-client key exists. `None` means
"skip the per-client limit" (the per-email limit still applies): behind a local proxy every user
shares one peer address, and one shared bucket would let a single attacker lock everyone out.
"""

import ipaddress

_MAX_HEADER_ENTRIES = 20


def _parse(value: str) -> "ipaddress.IPv4Address | ipaddress.IPv6Address | None":
    try:
        address = ipaddress.ip_address(value.strip())
    except ValueError:
        return None
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        return address.ipv4_mapped
    return address


def _shared(address: "ipaddress.IPv4Address | ipaddress.IPv6Address") -> bool:
    """Anything that is not a globally routable unicast address identifies a proxy, a local network
    or a reserved range, never one remote client (covers loopback, private, link-local, CGNAT,
    documentation ranges, multicast and the unspecified address)."""
    return not address.is_global or address.is_multicast


def client_key(
    peer: str | None,
    forwarded_for: str | None,
    *,
    trusted_proxy: bool,
    hops: int,
) -> str | None:
    if trusted_proxy:
        if not forwarded_for or hops < 1:
            return None
        entries = [part for part in forwarded_for.split(",")]
        if len(entries) > _MAX_HEADER_ENTRIES or len(entries) < hops:
            return None
        # With `hops` trusted proxies each appending the address it saw, the client is the entry
        # `hops` from the right. Anything further left was supplied by the client and is ignored.
        address = _parse(entries[-hops])
        return None if address is None else address.compressed
    if not peer:
        return None
    address = _parse(peer)
    if address is None or _shared(address):
        return None
    return address.compressed
