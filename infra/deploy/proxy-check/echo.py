import json
from http.server import BaseHTTPRequestHandler, HTTPServer
class H(BaseHTTPRequestHandler):
    def _go(self):
        body = json.dumps({"peer": self.client_address[0], "xff": self.headers.get("X-Forwarded-For"), "host": self.headers.get("Host"), "path": self.path}).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)
    do_GET = _go
    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0); self.rfile.read(n); self._go()
    def log_message(self, *a): pass
HTTPServer(("0.0.0.0", 8000), H).serve_forever()
