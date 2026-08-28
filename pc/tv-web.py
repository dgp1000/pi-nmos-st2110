#!/usr/bin/env python3
"""Atoll TV channel switcher (:8098).

Lists the HDHomeRun lineup; tapping a channel writes it to the tv-channel state file that
tv-send.sh watches, which retunes the multiview "Live TV" tile. Reachable from the iPad/Mac
(192.168.4.85:8098) or the PC (localhost:8098)."""
import http.server, socketserver, os, json, urllib.request, urllib.parse, html

PORT = 8098
HDHR_HOST = os.environ.get("HDHR_HOST", "192.168.7.88")
STATE = os.path.expanduser("~/atoll-run/tv-channel")


def lineup():
    try:
        d = json.load(urllib.request.urlopen(f"http://{HDHR_HOST}/lineup.json", timeout=6))
        return [(c.get("GuideNumber", ""), c.get("GuideName", ""), bool(c.get("HD"))) for c in d]
    except Exception:
        return []


def current():
    try:
        return open(STATE).read().strip()
    except Exception:
        return ""


def set_channel(ch):
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    with open(STATE, "w") as f:
        f.write(ch.strip())


def page():
    cur = current()
    btns = []
    for num, name, hd in lineup():
        sel = " sel" if num == cur else ""
        label = html.escape(f"{num}  {name}") + ("  HD" if hd else "")
        btns.append(f'<button class="ch{sel}" onclick="pick(\'{html.escape(num)}\')">{label}</button>')
    grid = "".join(btns) or '<p style="padding:1rem">HDHomeRun lineup unavailable.</p>'
    return ("""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Atoll TV</title><style>
body{margin:0;background:#0b0f0d;color:#cfe;font-family:system-ui,-apple-system,sans-serif}
h1{font-size:1rem;padding:.7rem 1rem;margin:0;background:#111;color:#9c9;position:sticky;top:0}
#g{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:.5rem;padding:1rem}
.ch{padding:.9rem .6rem;background:#0a1410;border:1px solid #1a3a2a;color:#9c9;border-radius:8px;font-size:.95rem;cursor:pointer;text-align:left}
.ch.sel{background:#093;color:#000;border-color:#0f0;font-weight:bold}
.ch:active{transform:scale(.97)}
</style></head><body>
<h1>Atoll &mdash; Live TV channel &nbsp;(current: """ + html.escape(cur or "?") + """)</h1>
<div id="g">""" + grid + """</div>
<script>
function pick(ch){fetch('/set?ch='+encodeURIComponent(ch)).then(()=>setTimeout(()=>location.reload(),500))}
</script></body></html>""").encode("utf-8")


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass
    def do_GET(self):
        if self.path.startswith("/set"):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            ch = (q.get("ch", [""])[0]).strip()
            if ch:
                set_channel(ch)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"channel": ch}).encode())
        else:
            b = page()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


with Server(("0.0.0.0", PORT), Handler) as s:
    print(f"Atoll TV switcher on http://localhost:{PORT}")
    try:
        s.serve_forever()
    except KeyboardInterrupt:
        pass
