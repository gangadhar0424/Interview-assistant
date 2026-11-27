"""SEC-14: the per-client auth limit must see the real client behind the documented proxy layout."""

from pathlib import Path

from app.auth.client_ip import client_key

CADDYFILE = Path(__file__).resolve().parents[3] / "infra" / "deploy" / "Caddyfile.example"
CLIENT = "203.0.113.50"  # documentation address standing in for a real client
PROXY = "172.18.0.5"  # the address the web container's proxy would append


def test_one_proxy_hop_reads_the_client() -> None:
    # Caddy replaces X-Forwarded-For with the address it saw: the documented single-hop layout.
    assert client_key("127.0.0.1", CLIENT, trusted_proxy=True, hops=1) == CLIENT


def test_a_client_supplied_prefix_is_ignored() -> None:
    forged = f"198.51.100.9, {CLIENT}"
    assert client_key("127.0.0.1", forged, trusted_proxy=True, hops=1) == CLIENT


def test_an_extra_appended_hop_would_key_every_user_to_the_proxy() -> None:
    """The failure mode a proxy layout must avoid: if a layer appended a second entry, hops=1
    would return the proxy for every user (one shared bucket). A real-chain check
    (infra/deploy/proxy-check) found that the Next.js rewrite does NOT append one; this test pins
    what the API would do if one did."""
    chain = f"{CLIENT}, {PROXY}"
    assert client_key("127.0.0.1", chain, trusted_proxy=True, hops=1) == PROXY
    assert client_key("127.0.0.1", chain, trusted_proxy=True, hops=2) == CLIENT


def test_too_few_entries_for_the_configured_hops_is_not_trusted() -> None:
    assert client_key("127.0.0.1", CLIENT, trusted_proxy=True, hops=2) is None


def _site_block(text: str, host: str) -> str:
    start = text.index(f"{host} {{")
    depth, i = 0, text.index("{", start)
    for j in range(i, len(text)):
        depth += text[j] == "{"
        depth -= text[j] == "}"
        if depth == 0:
            return text[start : j + 1]
    raise AssertionError("unbalanced Caddyfile")


def test_example_proxy_sends_web_api_calls_straight_to_the_api() -> None:
    block = _site_block(CADDYFILE.read_text(encoding="utf-8"), "app.example.com")
    api_route = block[block.index("handle /api/*") :]
    assert "127.0.0.1:8000" in api_route.split("handle {")[0]
    assert "header_up X-Forwarded-For {remote_host}" in api_route


def test_example_proxy_keeps_operational_endpoints_off_the_public_hosts() -> None:
    text = CADDYFILE.read_text(encoding="utf-8")
    api_block = _site_block(text, "api.example.com")
    assert "/metrics" in api_block and "respond @internal 404" in api_block
    # /metrics and /ready are not under /api/, so the web host's /api/* route cannot reach them.
    assert "handle /api/*" in _site_block(text, "app.example.com")
