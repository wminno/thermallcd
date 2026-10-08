"""thermallcd dashboard platform server (plan step 3).

Stdlib ThreadingHTTPServer (PF2) serving:
  /                  the dashboard page — scene-first (D22), widgets as per-widget
                     iframes (D21) on the D18 grid, stale contract in boot.js (D23)
  /api/login         token login -> cookie; first success flips the watermark (D20)
  /api/metrics       per-tick snapshot (PF1), token-gated
  /api/widgets       registry listing incl. malformed widgets' failure reasons (D25)
  /api/layout        per-device canonical layout (GET) + atomic writes (PUT);
                     ?shuffled=1 routes to the shuffle state (D28)
  /widgets/...       widget statics + theme.css + boot.js (public GET — repo content)

Auth (D7/R2): random token in data/token (0600, gitignored). Protected routes
accept the session cookie, `Authorization: Bearer`, or ?token= (capture's
chromium uses the query param). Editor mutations all sit behind it; the token
prints on the panel only until the first successful login (D20).
"""

import argparse
import hmac
import html
import json
import secrets
import socket
import sys
import threading
import urllib.parse
from http import cookies
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import layout as layout_mod
from . import registry as registry_mod
from . import metrics as metrics_mod

ASSETS = Path(__file__).parent / "assets"
TOKEN_COOKIE = "tlcd"
HTTP_TIMEOUT_S = 30  # PF2: no connection without a timeout


def lan_url(port: int) -> str:
    """Best-effort LAN address for the on-panel watermark."""
    ip = "127.0.0.1"
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0)
        s.connect(("8.8.8.8", 80))  # no packets sent; just picks a route
        ip = s.getsockname()[0]
        s.close()
    except OSError:
        pass
    return f"http://{ip}:{port}"


class TokenStore:
    def __init__(self, data_dir: Path):
        self.path = Path(data_dir) / "token"
        self.claimed_path = Path(data_dir) / "token.claimed"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            token = secrets.token_hex(16)
            self.path.write_text(token, encoding="utf-8")
            self.path.chmod(0o600)  # never group/world readable
        self._token = self.path.read_text(encoding="utf-8").strip()

    @property
    def token(self) -> str:
        return self._token

    def claimed(self) -> bool:
        return self.claimed_path.exists()

    def verify(self, supplied: str) -> bool:
        return hmac.compare_digest(supplied, self._token)

    def mark_claimed(self):
        if not self.claimed():
            self.claimed_path.write_text("claimed", encoding="utf-8")
            self.claimed_path.chmod(0o600)


class Handler(BaseHTTPRequestHandler):
    timeout = HTTP_TIMEOUT_S
    server_version = "thermallcd"

    # -- plumbing ----------------------------------------------------------

    def log_message(self, fmt, *args):  # quiet unless something is wrong
        pass

    def _send(self, code: int, body: bytes, ctype: str, extra: dict | None = None):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, code: int, obj, extra: dict | None = None):
        self._send(code, json.dumps(obj).encode(), "application/json", extra)

    def _file(self, path: Path, ctype: str):
        try:
            self._send(200, path.read_bytes(), ctype)
        except OSError:
            self._json(404, {"error": "not found"})

    def _token_supplied(self) -> str:
        auth = self.headers.get("Authorization", "")
        if auth.startswith("Bearer "):
            return auth[7:]
        cookie_header = self.headers.get("Cookie", "")
        try:
            jar = cookies.SimpleCookie(cookie_header)
            if TOKEN_COOKIE in jar:
                return jar[TOKEN_COOKIE].value
        except cookies.CookieError:
            pass
        return urllib.parse.parse_qs(
            urllib.parse.urlparse(self.path).query).get("token", [""])[0]

    def _authed(self) -> bool:
        return self.server.token.verify(self._token_supplied())

    def _guarded(self) -> bool:
        """True when auth passed; sends the 401 itself otherwise."""
        if self._authed():
            return True
        self._json(401, {"error": "token required"})
        return False

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if length <= 0 or length > 1_000_000:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {}

    # -- routes ------------------------------------------------------------

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        route, query = parsed.path, urllib.parse.parse_qs(parsed.query)
        if route == "/api/health":
            return self._json(200, {"ok": True})
        if route.startswith("/widgets/"):
            return self._widget_static(route)
        if not self._guarded():
            return
        if route == "/":
            return self._dashboard(query)
        if route == "/api/metrics":
            return self._json(200, self.server.metrics.snapshot())
        if route == "/api/widgets":
            return self._json(200, {"widgets": self.server.registry.scan()})
        if route == "/api/layout":
            device = query.get("device", ["default"])[0]
            doc = self.server.layout.load(device)
            state = self.server.layout.shuffle_state(device)
            if state:
                doc = {**doc, "shuffled": state}
            return self._json(200, doc)
        return self._json(404, {"error": "not found"})

    def do_POST(self):
        route = urllib.parse.urlparse(self.path).path
        if route != "/api/login":
            return self._json(404, {"error": "not found"})
        body = self._body()
        supplied = str(body.get("token", ""))
        if not self.server.token.verify(supplied):
            return self._json(401, {"error": "wrong token"})
        self.server.token.mark_claimed()  # first success -> watermark locks (D20)
        cookie = (f"{TOKEN_COOKIE}={supplied}; HttpOnly; SameSite=Strict; Path=/; "
                  "Max-Age=31536000")
        return self._json(200, {"ok": True}, {"Set-Cookie": cookie})

    def do_PUT(self):
        if not self._guarded():
            return
        route = urllib.parse.urlparse(self.path).path
        if route != "/api/layout":
            return self._json(404, {"error": "not found"})
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        device = query.get("device", ["default"])[0]
        shuffled = query.get("shuffled", ["0"])[0] == "1"
        body = self._body()
        try:
            doc = self.server.layout.save(device, body, shuffled=shuffled)
        except ValueError as e:
            return self._json(400, {"error": str(e)})
        return self._json(200, {"ok": True, "saved": doc})

    # -- dashboard page ------------------------------------------------------

    def _widget_static(self, route: str):
        rel = route[len("/widgets/"):]
        path = self.server.registry.widget_file("", rel)
        if path is None:
            return self._json(404, {"error": "not found"})
        ctypes = {".css": "text/css", ".js": "text/javascript",
                  ".html": "text/html", ".json": "application/json",
                  ".png": "image/png", ".svg": "image/svg+xml"}
        return self._file(path, ctypes.get(path.suffix, "application/octet-stream"))

    def _dashboard(self, query):
        registry = self.server.registry
        layout = self.server.layout.load("default")
        shuffle = self.server.layout.shuffle_state("default")
        if shuffle:
            layout = {**layout, "widgets": shuffle["widgets"]}
        token = self._token_supplied()
        slots, srcdocs = [], []
        for item in layout["widgets"]:
            entry = registry.get(item["id"])
            if entry is None:
                continue  # absent/malformed widget: the scene shows through (D22/D25)
            slots.append((item, entry))
            srcdocs.append(_widget_srcdoc(self.server.registry, item["id"], token))
        page = _render_dashboard(self.server, slots, srcdocs, token)
        return self._send(200, page.encode("utf-8"), "text/html; charset=utf-8")


def _widget_srcdoc(registry, name: str, token: str) -> str:
    """Widget index.html with the auth token injected, html-escaped for srcdoc."""
    path = registry.widget_file(name, "index.html")
    doc = path.read_text(encoding="utf-8") if path else (
        "<!doctype html><body><div class='tlcd-title'>%s</div>"
        "widget files missing</body>" % html.escape(name))
    inject = f'<script>window.TLCD_TOKEN={json.dumps(token)};</script>'
    if "</head>" in doc.lower():
        idx = doc.lower().index("</head>")
        doc = doc[:idx] + inject + doc[idx:]
    else:
        doc = inject + doc
    return html.escape(doc, quote=True)


def _render_dashboard(server, slots, srcdocs, token: str) -> str:
    url = lan_url(server.port)
    claimed = server.token.claimed()
    short = server.token.token[:4] if not claimed else ""
    watermark = (
        f"{url} · token {short} (until first login)"
        if short else f"{url} &#128274;"
    )
    frames = []
    for (item, entry), srcdoc in zip(slots, srcdocs):
        col = f"grid-column: {item['x'] + 1} / span {item['w']};"
        row = f"grid-row: {item['y'] + 1} / span {item['h']};"
        frames.append(
            f'<iframe class="widget" style="{col} {row}" srcdoc="{srcdoc}" '
            f'title="{html.escape(entry.get("title") or item["id"])}"></iframe>')
    template = (ASSETS / "dashboard.html").read_text(encoding="utf-8")
    return (template
            .replace("/*__WATERMARK__*/", watermark)
            .replace("/*__FRAMES__*/", "\n".join(frames))
            .replace("__TOKEN__", json.dumps(token)))


class DashboardServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, addr, token: TokenStore, registry, layout, metrics, port: int):
        self.token = token
        self.registry = registry
        self.layout = layout
        self.metrics = metrics
        self.port = port
        super().__init__(addr, Handler)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--bind", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--data", default=str(Path(__file__).parent.parent / "data"),
                    help="state dir (token, layouts); gitignored")
    ap.add_argument("--widgets", default=str(Path(__file__).parent.parent / "widgets"))
    ap.add_argument("--show-token", action="store_true",
                    help="print the token (same as `thermallcd token`)")
    args = ap.parse_args(argv)

    data_dir = Path(args.data)
    token = TokenStore(data_dir)
    if args.show_token:
        print(token.token)
        return 0
    registry = registry_mod.Registry(Path(args.widgets))
    store = layout_mod.LayoutStore(data_dir / "layouts")
    metrics = metrics_mod.MetricsSampler(interval=1.0)

    server = DashboardServer((args.bind, args.port), token, registry, store, metrics,
                             args.port)
    metrics.start()
    print(f"dashboard: http://{args.bind}:{args.port} "
          f"(token in {data_dir / 'token'})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        metrics.stop()
        server.server_close()


if __name__ == "__main__":
    main()
