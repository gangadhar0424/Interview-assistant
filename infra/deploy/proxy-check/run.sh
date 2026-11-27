#!/usr/bin/env bash
# Real reverse-proxy check for the per-client login limit (SEC-14, D-82). Runs on any machine with Docker.
#
# It builds the real chain  Caddy -> (web app | API)  on a private network with FIXED, DISTINCT client
# addresses, then asks two questions in each of two Caddy layouts:
#   1. what X-Forwarded-For does the API process actually receive (echo server in place of the API)?
#   2. does one client's login failures throttle a different client (real API)?
#
# Required: API_IMAGE, WEB_IMAGE (the images you deploy), DATABASE_URL (asyncpg URL of an EMPTY, migrated
# throwaway database reachable from containers, e.g. host.docker.internal), REDIS_URL. Everything it
# creates is named ia-proxycheck-* and removed at the end. It never touches other containers.
set -euo pipefail
: "${API_IMAGE:?}" "${WEB_IMAGE:?}" "${DATABASE_URL:?}" "${REDIS_URL:?}"
HERE="$(cd "$(dirname "$0")" && pwd)"
export MSYS_NO_PATHCONV=1
NET=ia-proxycheck
JWT="$(python -c 'import secrets;print(secrets.token_urlsafe(48))')"
cleanup() { for c in $NET-caddy $NET-api $NET-web; do docker rm -f "$c" >/dev/null 2>&1 || true; done; docker network rm $NET >/dev/null 2>&1 || true; }
trap cleanup EXIT
cleanup
docker network create --subnet 172.30.0.0/24 $NET >/dev/null
docker run -d --name $NET-web --network $NET --network-alias web "$WEB_IMAGE" >/dev/null
caddy() { docker rm -f $NET-caddy >/dev/null 2>&1 || true; docker run -d --name $NET-caddy --network $NET --network-alias caddy -v "$HERE/Caddyfile.$1:/etc/caddy/Caddyfile:ro" caddy:2-alpine >/dev/null; sleep 3; }
client() { docker run --rm --network $NET --ip "$3" -v "$HERE/client.py:/client.py:ro" python:3.12-slim python /client.py "$1 ($3)" "$2"; }
echo "### 1. what the API process receives (echo server in place of the API)"
docker run -d --name $NET-api --network $NET --network-alias api -v "$HERE/echo.py:/echo.py:ro" python:3.12-slim python /echo.py >/dev/null; sleep 2
for layout in old new; do caddy "$layout"; echo "layout $layout:"; docker run --rm --network $NET --ip 172.30.0.51 -v "$HERE/ask.py:/ask.py:ro" python:3.12-slim python /ask.py http://caddy/api/v1/ping; done
docker rm -f $NET-api >/dev/null
echo "### 2. does one client's failures throttle another (real API, TRUSTED_PROXY_HOPS=1)"
for layout in old new; do
  docker rm -f $NET-api >/dev/null 2>&1 || true
  docker run -d --name $NET-api --network $NET --network-alias api -e APP_ENV=local -e "DATABASE_URL=$DATABASE_URL" -e "REDIS_URL=$REDIS_URL" -e "JWT_SECRET=$JWT" -e CORS_ORIGINS=http://localhost -e TRUSTED_PROXY_ENABLED=true -e TRUSTED_PROXY_HOPS=1 "$API_IMAGE" >/dev/null
  for i in $(seq 1 40); do docker exec $NET-api python -c "import urllib.request,sys;sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health',timeout=2).status==200 else 1)" >/dev/null 2>&1 && break; sleep 2; done
  caddy "$layout"; echo "layout $layout:"
  client A 24 172.30.0.51; client B 3 172.30.0.52; client C 3 172.30.0.53
done
echo "Expected: A reaches 429 after 20 failures; B and C stay 401 (their own buckets)."
