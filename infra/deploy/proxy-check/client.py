import json, sys, urllib.request, urllib.error, uuid
who, count = sys.argv[1], int(sys.argv[2])
codes = []
for _ in range(count):
    body = json.dumps({"email": f"{uuid.uuid4().hex[:10]}@example.com", "password": "wrong-password-123"}).encode()
    req = urllib.request.Request("http://caddy/api/v1/auth/login", data=body, headers={"Content-Type": "application/json"})
    try:
        codes.append(urllib.request.urlopen(req, timeout=15).status)
    except urllib.error.HTTPError as e:
        codes.append(e.code)
print(who, "status codes:", codes)
