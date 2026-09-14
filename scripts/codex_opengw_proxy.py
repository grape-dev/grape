"""Codex → opengw proxy: strips unsupported tools (web_search) from responses API body.

Run as a background service on port 18899.
Codex config base_url should point to http://127.0.0.1:18899/v1
"""
import http.server, json, sys, requests, urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

_UNSTRIPPABLE = {'host', 'content-length', 'transfer-encoding', 'authorization'}

class Handler(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(length) if length else b''

        # Get auth from incoming request
        auth = self.headers.get('Authorization', '')

        # Strip unsupported tools from body
        try:
            data = json.loads(body)
            tools = data.get('tools', [])
            filtered = [t for t in tools if t.get('type') != 'web_search']
            if len(filtered) < len(tools):
                data['tools'] = filtered
                body = json.dumps(data).encode()
                sys.stderr.write(f"Stripped web_search: {len(tools)} -> {len(filtered)} tools\n")
        except Exception:
            pass

        # Forward to opengw via requests (explicit headers, no auto-override)
        url = f"https://opengw.clouddeveloper.club{self.path}"
        fwd_headers = {
            'Authorization': auth,
            'Content-Type': self.headers.get('Content-Type', 'application/json'),
            'Accept': self.headers.get('Accept', 'text/event-stream'),
        }

        try:
            r = requests.post(url, headers=fwd_headers, data=body, timeout=120, verify=False, stream=True)
            self.send_response(r.status_code)
            for k, v in r.headers.items():
                if k.lower() not in ('transfer-encoding', 'connection', 'content-encoding', 'content-length'):
                    self.send_header(k, v)
            self.end_headers()
            for chunk in r.iter_content(8192):
                if chunk:
                    self.wfile.write(chunk)
            r.close()
        except Exception as e:
            sys.stderr.write(f"Proxy error: {e}\n")
            self.send_response(502)
            self.end_headers()
            self.wfile.write(str(e).encode())

    def log_message(self, *a):
        pass

server = http.server.ThreadingHTTPServer(('127.0.0.1', 18899), Handler)
print("Codex proxy ready on :18899 (strips web_search, forwards to opengw)", file=sys.stderr)
server.serve_forever()
