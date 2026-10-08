"""Platform-core tests: auth matrix, registry failure reasons, layout store.

Covers the verify obligations from the plan (T1/T5/T6): 401/401/200 auth,
registry skip + failure-reason payload (D25), layout round-trip + corrupt
recovery (EC1) + the D28 canonical/shuffle split.
"""

import json
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path

from thermallcd import dashboard as dash
from thermallcd import layout as layout_mod


def _http(url, method="GET", token=None, body=None, cookie=None):
    req = urllib.request.Request(url, method=method)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    if cookie:
        req.add_header("Cookie", cookie)
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        req.add_header("Content-Type", "application/json")

    def _parse(raw):
        try:
            return json.loads(raw.decode())
        except (json.JSONDecodeError, UnicodeDecodeError):
            return raw.decode(errors="replace")  # HTML pages come back as text

    try:
        with urllib.request.urlopen(req, data=data, timeout=5) as r:
            return r.status, dict(r.headers), _parse(r.read())
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), _parse(e.read())


class PlatformTestBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        widgets = root / "widgets"
        (widgets / "w-good").mkdir(parents=True)
        (widgets / "w-good" / "manifest.json").write_text(json.dumps({
            "name": "w-good", "title": "GOOD", "size": {"w": 2, "h": 1},
            "resize": "free"}))
        (widgets / "w-good" / "index.html").write_text(
            "<!doctype html><html><head><title>x</title></head><body>ok</body></html>")
        (widgets / "w-bad").mkdir()
        (widgets / "w-bad" / "manifest.json").write_text('{"name": "w-bad", "size": "oops"}')
        (widgets / "w-bad" / "index.html").write_text("<html><body>bad</body></html>")
        (widgets / "theme.css").write_text(":root { --tlcd-accent: #ff2e83; }")

        cls.token = dash.TokenStore(root / "data")
        cls.registry = dash.registry_mod.Registry(widgets)
        cls.store = layout_mod.LayoutStore(root / "data" / "layouts")
        cls.metrics = dash.metrics_mod.MetricsSampler(interval=0.05)
        cls.server = dash.DashboardServer(
            ("127.0.0.1", 0), cls.token, cls.registry, cls.store, cls.metrics, 0)
        cls.port = cls.server.server_address[1]
        cls.base = f"http://127.0.0.1:{cls.port}"
        cls.metrics.start()
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.metrics.stop()
        cls.server.shutdown()
        cls.tmp.cleanup()


class TestAuthMatrix(PlatformTestBase):
    def test_apis_401_without_token(self):
        status, _, _ = _http(f"{self.base}/api/metrics")
        self.assertEqual(status, 401)

    def test_root_serves_login_page_when_unauthenticated(self):
        # a human opening the printed URL gets a sign-in page, not JSON (D7/R2)
        status, _, body = _http(f"{self.base}/")
        self.assertEqual(status, 200)
        self.assertIn("SIGN IN", body)
        status, _, _ = _http(f"{self.base}/", token="nope")
        self.assertEqual(status, 200)          # wrong token: same login page
        status, _, body = _http(f"{self.base}/api/metrics", token="nope")
        self.assertEqual(status, 401)          # APIs stay strict

    def test_right_token_200(self):
        status, _, body = _http(f"{self.base}/api/metrics", token=self.token.token)
        self.assertEqual(status, 200)
        self.assertIn("refresh", body)

    def test_login_and_cookie(self):
        status, headers, _ = _http(f"{self.base}/api/login", method="POST",
                                   body={"token": "wrong"})
        self.assertEqual(status, 401)
        status, headers, _ = _http(f"{self.base}/api/login", method="POST",
                                   body={"token": self.token.token})
        self.assertEqual(status, 200)
        cookie = headers["Set-Cookie"].split(";")[0]
        status, _, body = _http(f"{self.base}/", cookie=cookie)
        self.assertEqual(status, 200)
        self.assertIn('class="grid"', body)  # page body renders

    def test_health_is_public(self):
        status, _, body = _http(f"{self.base}/api/health")
        self.assertEqual((status, body["ok"]), (200, True))

    def test_widget_statics_are_public(self):
        status, headers, _ = _http(f"{self.base}/widgets/theme.css")
        self.assertEqual(status, 200)

    def test_editor_route(self):
        status, _, body = _http(f"{self.base}/editor")
        self.assertEqual((status, "SIGN IN" in body), (200, True))  # gate first
        status, _, body = _http(f"{self.base}/editor", token=self.token.token)
        self.assertEqual(status, 200)
        self.assertIn("gridstack", body)  # editor page serves

    def test_assets_guarded_with_traversal(self):
        status, _, _ = _http(f"{self.base}/assets/vendor/gridstack.min.js")
        self.assertEqual(status, 401)
        # resolves to an existing file OUTSIDE assets/ — the guard must refuse
        status, _, _ = _http(f"{self.base}/assets/../dashboard.py",
                             token=self.token.token)
        self.assertEqual(status, 404)

    def test_token_file_0600(self):
        mode = oct(self.token.path.stat().st_mode & 0o777)
        self.assertEqual(mode, "0o600")


class TestRegistry(PlatformTestBase):
    def test_malformed_widget_skipped_with_reason(self):
        status, _, body = _http(f"{self.base}/api/widgets", token=self.token.token)
        self.assertEqual(status, 200)
        by_name = {w["name"]: w for w in body["widgets"]}
        self.assertNotIn("error", by_name["w-good"])
        self.assertIn("error", by_name["w-bad"])          # D25: listed, not dropped
        self.assertIn("size", by_name["w-bad"]["error"])

    def test_dashboard_renders_only_valid_widgets(self):
        # a layout slot only becomes a frame when its widget is valid (D25/D22)
        self.store.save("default", {"widgets": [
            {"id": "w-good", "x": 0, "y": 0, "w": 2, "h": 1},
            {"id": "w-bad", "x": 2, "y": 0, "w": 2, "h": 1}]})
        status, _, body = _http(f"{self.base}/", token=self.token.token)
        self.assertEqual(status, 200)
        self.assertEqual(body.count("<iframe"), 1)   # w-good's frame only
        self.assertNotIn("w-bad", body)              # malformed: scene shows through
        self.assertIn("srcdoc=", body)               # per-widget iframe (D21)


class TestLayoutStore(PlatformTestBase):
    def test_round_trip(self):
        layout = {"widgets": [{"id": "w-good", "x": 0, "y": 0, "w": 2, "h": 1}]}
        self.store.save("dev1", layout)
        self.assertEqual(self.store.load("dev1"), layout)

    def test_rejects_out_of_grid(self):
        bad = {"widgets": [{"id": "x", "x": 11, "y": 0, "w": 4, "h": 1}]}
        with self.assertRaises(ValueError):
            self.store.save("dev1", bad)

    def test_shuffle_never_touches_canonical(self):
        canonical = {"widgets": [{"id": "w-good", "x": 0, "y": 0, "w": 2, "h": 1}]}
        self.store.save("dev2", canonical)
        permuted = {"widgets": [{"id": "w-good", "x": 3, "y": 0, "w": 2, "h": 1}]}
        self.store.save("dev2", permuted, shuffled=True)
        self.assertEqual(self.store.load("dev2"), canonical)      # D28
        state = self.store.shuffle_state("dev2")
        self.assertTrue(state["shuffled"])
        self.assertEqual(state["widgets"], permuted["widgets"])

    def test_corrupt_file_falls_back_to_default(self):
        self.store.save("dev3", {"widgets": [
            {"id": "w-good", "x": 0, "y": 0, "w": 2, "h": 1}]})
        path = self.store.dir / "dev3.json"
        path.write_text("{not json")                       # simulate corruption
        loaded = self.store.load("dev3")
        self.assertEqual(loaded, layout_mod.DEFAULT_LAYOUT)
        self.assertTrue(list(self.store.dir.glob("dev3.corrupt-*")))  # moved aside


class TestLayoutAPI(PlatformTestBase):
    def test_put_then_get(self):
        layout = {"widgets": [{"id": "w-good", "x": 2, "y": 1, "w": 2, "h": 1}]}
        status, _, _ = _http(f"{self.base}/api/layout?device=api-dev", method="PUT",
                             token=self.token.token, body=layout)
        self.assertEqual(status, 200)
        status, _, doc = _http(f"{self.base}/api/layout?device=api-dev",
                               token=self.token.token)
        self.assertEqual(status, 200)
        self.assertEqual(doc["widgets"], layout["widgets"])

    def test_put_invalid_400(self):
        status, _, body = _http(f"{self.base}/api/layout", method="PUT",
                                token=self.token.token, body={"widgets": "nope"})
        self.assertEqual(status, 400)

    def test_get_includes_claim_state(self):
        # the dashboard's 1 s poll (D27) watches this doc: the claim state
        # riding along is what flips the on-panel watermark (D20)
        status, _, doc = _http(f"{self.base}/api/layout?device=c13",
                               token=self.token.token)
        self.assertEqual((status, doc["claimed"]), (200, False))


if __name__ == "__main__":
    unittest.main()
