"""thermallcd dashboard platform server (plan step 3).

Stdlib ThreadingHTTPServer (PF2) serving:
  /                  the dashboard page — scene-first (D22), widgets as per-widget
                     iframes (D21) on the D18 grid, stale contract in boot.js (D23)
  /api/login         token login -> cookie; first success flips the watermark (D20)
  /api/metrics       per-tick snapshot (PF1), token-gated
  /api/nvtop         image-source widget (T4): nvtop TUI rendered from a pty
                     as JPEG, sized to the widget's layout slot (D19/D33)
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
import time
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
# a profile file older than this reads as the panel being gone (the capture
# touches it every ~10 s while it streams — T15)
ONLINE_WINDOW_S = 30.0


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


def load_profiles(data_dir) -> dict:
    """Per-device panel profiles, written by the capture at handshake (T15).

    data/profiles/<id>.json carries {id, serial, w, h, pm, sub}; its mtime is
    the heartbeat (the capture touches it while streaming), so `online` here
    is "a capture pipeline ran for this panel in the last ONLINE_WINDOW_S".
    Unknown/missing profiles fall back to the 9.16" default at the callers."""
    out = {}
    profiles = Path(data_dir) / "profiles"
    if not profiles.is_dir():
        return out
    for path in sorted(profiles.glob("*.json")):
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
            if not (isinstance(doc.get("id"), str) and doc.get("w") and doc.get("h")):
                continue
            doc["online"] = time.time() - path.stat().st_mtime < ONLINE_WINDOW_S
            out[doc["id"]] = doc
        except (json.JSONDecodeError, OSError):
            continue
    return out


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
        if route == "/":
            # a human opening the printed URL gets a sign-in page, not a
            # JSON 401; the API routes below stay strict (D7/R2)
            if self._authed():
                return self._dashboard(query)
            return self._file(ASSETS / "login.html", "text/html; charset=utf-8")
        if route == "/editor":
            if self._authed():
                return self._file(ASSETS / "editor.html", "text/html; charset=utf-8")
            return self._file(ASSETS / "login.html", "text/html; charset=utf-8")
        if route.startswith("/assets/"):
            if not self._guarded():
                return
            return self._assets_static(route)
        if not self._guarded():
            return
        if route == "/api/metrics":
            return self._json(200, self.server.metrics.snapshot())
        if route == "/api/nvtop":
            # image-source widget (T4): the pane's TUI as a transparent
            # PNG; 404 when the layout has no nvtop slot — the widget dims,
            # never fabricates. The pane's size is derived server-side from
            # the slot (see NvtopSource.image) — query params are ignored.
            data = self.server.nvtop.image()
            if data is None:
                return self._json(404, {"error": "nvtop not placed"})
            return self._send(200, data, "image/png")
        if route == "/api/tty":
            # tty widget (T18): the placement's command runs in a pty, served
            # as a transparent PNG. The command comes from the layout's tty
            # item options — never from this request, whose query params are
            # ignored (D19). 404 when unplaced or no command set: the widget
            # dims and says so, never fabricating a pane.
            data = self.server.tty.image()
            if data is None:
                return self._json(404, {"error": "tty not placed or no command set"})
            return self._send(200, data, "image/png")
        if route == "/api/widgets":
            return self._json(200, {"widgets": self.server.registry.scan()})
        if route == "/api/devices":
            # T15 device selector: panels the capture pipelines have reported
            # (profiles dir), online = profile heartbeat inside the window
            return self._json(200, {"devices": sorted(
                self.server.profiles().values(),
                key=lambda d: (not d["online"], d["id"]))})
        if route == "/api/frame":
            # live editor preview: one widget frame's doc, so the editor's
            # preview iframe can add a newly placed widget without a page
            # reload (the editor pushes its unsaved layout on every change)
            name = query.get("id", [""])[0]
            entry = self.server.registry.get(name)
            if entry is None:
                return self._json(404, {"error": "not found"})
            # T17: per-instance options ride as opt_<key> params — the frame
            # doc bakes them in as window.TLCD_OPTIONS
            opts = {k[4:]: v[0] for k, v in query.items()
                    if k.startswith("opt_") and len(k) > 4}
            return self._json(200, {
                "id": name,
                "title": entry.get("title") or name,
                "srcdoc": _widget_doc(self.server.registry, name,
                                      self._token_supplied(), opts),
            })
        if route == "/api/layout":
            device = query.get("device", ["default"])[0]
            doc = self.server.layout.load(device)
            state = self.server.layout.shuffle_state(device)
            if state:
                doc = {**doc, "shuffled": state}
            # the dashboard's 1 s layout poll (D27) reloads on any change to
            # this doc — carrying the claim state here is what flips the
            # on-panel watermark to a lock within a second of first login (D20)
            doc = {**doc, "claimed": self.server.token.claimed()}
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
        # D24 "remember on this device": unchecked -> session cookie
        cookie = f"{TOKEN_COOKIE}={supplied}; HttpOnly; SameSite=Strict; Path=/"
        if body.get("remember", True):
            cookie += "; Max-Age=31536000"
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

    def _assets_static(self, route: str):
        """Editor/vendor assets under assets/ — traversal-guarded."""
        rel = route[len("/assets/"):]
        target = (ASSETS / rel).resolve()
        if ASSETS.resolve() not in target.parents or not target.is_file():
            return self._json(404, {"error": "not found"})
        ctypes = {".css": "text/css", ".js": "text/javascript",
                  ".html": "text/html"}
        return self._file(target, ctypes.get(target.suffix,
                                             "application/octet-stream"))

    def _dashboard(self, query):
        registry = self.server.registry
        # T15: the capture loads its page with ?device=<panel id>; a browser
        # without the param still renders the shared default arrangement
        device = query.get("device", ["default"])[0]
        layout = self.server.layout.load(device)
        shuffle = self.server.layout.shuffle_state(device)
        if shuffle:
            layout = {**layout, "widgets": shuffle["widgets"]}
        # D29: synthwave on from first boot — a layout without a flair key
        # (or one saved before T13) renders the defaults
        flair = {**layout_mod.DEFAULT_FLAIR, **(layout.get("flair") or {})}
        token = self._token_supplied()
        slots, srcdocs = [], []
        for item in layout["widgets"]:
            entry = registry.get(item["id"])
            if entry is None:
                continue  # absent/malformed widget: the scene shows through (D22/D25)
            slots.append((item, entry))
            srcdocs.append(_widget_srcdoc(self.server.registry, item["id"], token,
                                          item.get("options")))
        profile = self.server.profiles().get(device) or {}
        page = _render_dashboard(self.server, slots, srcdocs, token, flair,
                                 device, profile)
        return self._send(200, page.encode("utf-8"), "text/html; charset=utf-8")


_WID_BAD = '&="\'\\[]'


def _wid_escape(s: str) -> str:
    """Escape the characters that would break the client's querySelector or
    the k=v wid format. Mirrored by widEscape() in dashboard.html — the wid
    is only a DOM lookup key, so both sides must agree byte for byte."""
    return "".join("%{:02x}".format(ord(c)) if c in _WID_BAD else c
                   for c in s)


def _wid(item_id: str, options: dict | None) -> str:
    """Frame identity (T17): the widget id, suffixed with its per-instance
    options so an options change is a NEW frame — the client drops and
    refetches it through the same path as a newly placed widget. The server
    render, the shuffle engine and the editor preview must build this
    identically; nothing parses it back apart."""
    if not options:
        return item_id
    return item_id + "@" + "&".join(
        f"{_wid_escape(k)}={_wid_escape(options[k])}"
        for k in sorted(options))


def _widget_doc(registry, name: str, token: str,
                options: dict | None = None) -> str:
    """Widget index.html with the auth token injected (raw, JSON-safe)."""
    path = registry.widget_file(name, "index.html")
    doc = path.read_text(encoding="utf-8") if path else (
        "<!doctype html><body><div class='tlcd-title'>%s</div>"
        "widget files missing</body>" % html.escape(name))
    inject = (f'<script>window.TLCD_TOKEN={json.dumps(token)};'
              f'window.TLCD_OPTIONS={json.dumps(options or {})};</script>')
    if "</head>" in doc.lower():
        idx = doc.lower().index("</head>")
        return doc[:idx] + inject + doc[idx:]
    return inject + doc


def _widget_srcdoc(registry, name: str, token: str,
                   options: dict | None = None) -> str:
    """_widget_doc html-escaped for a srcdoc attribute."""
    return html.escape(_widget_doc(registry, name, token, options), quote=True)


def _render_dashboard(server, slots, srcdocs, token: str, flair: dict,
                      device: str = "default", profile: dict | None = None) -> str:
    url = lan_url(server.port)
    claimed = server.token.claimed()
    # D20: the full token prints on the panel until the first successful
    # login — physical presence on the device is the proof of ownership
    shown = "" if claimed else server.token.token
    watermark = (
        f"{url} · token {shown}"
        if shown else f"{url} &#128274;"
    )
    frames = []
    for (item, entry), srcdoc in zip(slots, srcdocs):
        col = f"grid-column: {item['x'] + 1} / span {item['w']};"
        row = f"grid-row: {item['y'] + 1} / span {item['h']};"
        wid = _wid(item["id"], item.get("options"))
        frames.append(
            f'<iframe class="widget" data-wid="{html.escape(wid, quote=True)}" '
            f'style="{col} {row}" srcdoc="{srcdoc}" '
            f'title="{html.escape(entry.get("title") or item["id"])}"></iframe>')
    # the effective arrangement (canonical or the D28 shuffle state): the
    # shuffle engine permutes from what is actually rendered, not from the
    # canonical doc the layout API serves
    placed = [{"id": item["id"], "x": item["x"], "y": item["y"],
               "w": item["w"], "h": item["h"],
               "options": item.get("options")} for (item, _e) in slots]
    # T15: the panel profile sizes the canvas; unknown/absent -> 9.16" default
    w = int((profile or {}).get("w") or 1920)
    h = int((profile or {}).get("h") or 462)
    template = (ASSETS / "dashboard.html").read_text(encoding="utf-8")
    return (template
            .replace("/*__WATERMARK__*/", watermark)
            .replace("/*__FRAMES__*/", "\n".join(frames))
            .replace("__FLAIR__", json.dumps(flair))
            .replace("__WIDGETS__", json.dumps(placed))
            .replace("__GLASS__", json.dumps(flair.get("glass", 0.82)))
            .replace("__TOKEN__", json.dumps(token))
            # __TOKEN__ is JSON-quoted for JS context; __TOKEN_URL__ is the
            # raw value for attribute context (guarded subresource URLs)
            .replace("__TOKEN_URL__", urllib.parse.quote(token, safe=""))
            .replace("__DEVICE__", json.dumps(device))
            .replace("__PANEL_W__", str(w))
            .replace("__PANEL_H__", str(h)))


class DashboardServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, addr, token: TokenStore, registry, layout, metrics, port: int,
                 data_dir=None):
        self.token = token
        self.registry = registry
        self.layout = layout
        self.metrics = metrics
        self.port = port
        self.data_dir = Path(data_dir) if data_dir else None
        self._nvtop = None
        self._tty = None
        self._profiles = (None, 0.0)   # (cache, loaded-at) — re-read each 5 s
        super().__init__(addr, Handler)

    @property
    def nvtop(self):
        """Lazy nvtop image source — importing pyte/PIL (and forking nvtop)
        must not happen for tests or startup, only on first widget request."""
        if self._nvtop is None:
            from thermallcd import nvtop_pane
            self._nvtop = nvtop_pane.NvtopSource(self.layout)
        return self._nvtop

    def profiles(self) -> dict:
        """Device profiles with a small cache — /api/devices and the page
        render hit this per poll; the dir walk must not run per request."""
        if self.data_dir is None:
            return {}
        now = time.monotonic()
        cache, loaded = self._profiles
        if now - loaded > 5.0:
            cache = load_profiles(self.data_dir)
            self._profiles = (cache, now)
        return cache

    def nvtop_stop(self):
        if self._nvtop is not None:
            self._nvtop.stop()

    @property
    def tty(self):
        """Lazy tty image source (T18) — same laziness contract as nvtop:
        importing pyte/PIL and forking the pane's shell must not happen for
        tests or startup, only on first widget request."""
        if self._tty is None:
            from thermallcd import tty_pane
            self._tty = tty_pane.TtySource(self.layout)
        return self._tty

    def tty_stop(self):
        if self._tty is not None:
            self._tty.stop()


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
                             args.port, data_dir=data_dir)
    metrics.start()
    print(f"dashboard: http://{args.bind}:{args.port} "
          f"(token in {data_dir / 'token'})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        metrics.stop()
        server.nvtop_stop()
        server.tty_stop()
        server.server_close()


if __name__ == "__main__":
    main()
