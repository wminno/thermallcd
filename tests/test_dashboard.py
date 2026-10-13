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
        (widgets / "w-image").mkdir()
        (widgets / "w-image" / "manifest.json").write_text(json.dumps({
            "name": "w-image", "title": "IMG", "size": {"w": 2, "h": 1},
            "source": "image"}))
        (widgets / "w-image" / "index.html").write_text("<html><body>img</body></html>")
        (widgets / "w-srcbad").mkdir()
        (widgets / "w-srcbad" / "manifest.json").write_text(json.dumps({
            "name": "w-srcbad", "size": {"w": 1, "h": 1}, "source": "bogus"}))
        (widgets / "w-srcbad" / "index.html").write_text("<html><body>src</body></html>")
        (widgets / "w-opts").mkdir()
        (widgets / "w-opts" / "manifest.json").write_text(json.dumps({
            "name": "w-opts", "title": "OPTS", "size": {"w": 2, "h": 1},
            "options": [{"key": "view", "label": "View", "type": "select",
                         "choices": [{"value": "bars", "label": "Bars"},
                                     {"value": "history", "label": "History"}]}]}))
        (widgets / "w-opts" / "index.html").write_text(
            "<!doctype html><html><head><title>o</title></head><body>ok</body></html>")
        (widgets / "w-optsbad").mkdir()
        (widgets / "w-optsbad" / "manifest.json").write_text(json.dumps({
            "name": "w-optsbad", "size": {"w": 1, "h": 1},
            "options": [{"key": "view", "type": "select"}]}))
        (widgets / "w-optsbad" / "index.html").write_text("<html><body>o</body></html>")
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

    def test_nvtop_endpoint_auth_then_404_without_slot(self):
        status, _, _ = _http(f"{self.base}/api/nvtop")
        self.assertEqual(status, 401)
        # a default layout without an nvtop slot: 404 and never a pane fork
        self.store.save("default", {"widgets": [
            {"id": "w-good", "x": 0, "y": 0, "w": 2, "h": 1}]})
        status, _, body = _http(f"{self.base}/api/nvtop", token=self.token.token)
        self.assertEqual(status, 404)

    def test_frame_endpoint_auth_payload_and_404(self):
        # live editor preview: one widget frame's doc (raw, token injected)
        status, _, _ = _http(f"{self.base}/api/frame?id=w-good")
        self.assertEqual(status, 401)
        status, _, body = _http(f"{self.base}/api/frame?id=w-good",
                                token=self.token.token)
        self.assertEqual(status, 200)
        self.assertEqual(body["title"], "GOOD")
        self.assertIn("window.TLCD_TOKEN", body["srcdoc"])
        # raw doc, not the srcdoc-escaped form — the preview assigns it as
        # a property (iframe.srcdoc = doc.srcdoc), so escaping would show tags
        self.assertNotIn("&lt;", body["srcdoc"])
        status, _, _ = _http(f"{self.base}/api/frame?id=nope",
                             token=self.token.token)
        self.assertEqual(status, 404)


class TestRegistry(PlatformTestBase):
    def test_malformed_widget_skipped_with_reason(self):
        status, _, body = _http(f"{self.base}/api/widgets", token=self.token.token)
        self.assertEqual(status, 200)
        by_name = {w["name"]: w for w in body["widgets"]}
        self.assertNotIn("error", by_name["w-good"])
        self.assertIn("error", by_name["w-bad"])          # D25: listed, not dropped
        self.assertIn("size", by_name["w-bad"]["error"])

    def test_source_mode_validated(self):
        # SC4: manifests declare their source; unknown modes fail with a reason
        status, _, body = _http(f"{self.base}/api/widgets", token=self.token.token)
        by_name = {w["name"]: w for w in body["widgets"]}
        self.assertEqual(by_name["w-image"]["source"], "image")
        self.assertEqual(by_name["w-good"]["source"], "html")     # default
        self.assertIn("source", by_name["w-srcbad"]["error"])

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
        self.assertIn('data-wid="w-good"', body)     # live-preview targeting
        self.assertIn("TLCD_WIDGETS", body)          # T14: effective arrangement
                                                     # for the shuffle engine

    def test_view_options_schema_and_frame_injection(self):
        # T17: manifest options validate; per-instance values reach the
        # widget doc as window.TLCD_OPTIONS and key the frame identity
        status, _, body = _http(f"{self.base}/api/widgets", token=self.token.token)
        by_name = {w["name"]: w for w in body["widgets"]}
        self.assertEqual(by_name["w-opts"]["options"][0]["key"], "view")
        self.assertIn("options", by_name["w-optsbad"]["error"])
        status, _, body = _http(
            f"{self.base}/api/frame?id=w-opts&opt_view=history",
            token=self.token.token)
        self.assertEqual(status, 200)
        self.assertIn('window.TLCD_OPTIONS={"view": "history"}', body["srcdoc"])
        # no opt_ params -> empty options object
        status, _, body = _http(f"{self.base}/api/frame?id=w-opts",
                                token=self.token.token)
        self.assertIn("window.TLCD_OPTIONS={}", body["srcdoc"])

    def test_wid_builds_frame_identity(self):
        # the wid string is a DOM lookup key shared by the server render and
        # the client: id alone, id@sorted-kv with options, escapes for the
        # characters that would break the client's querySelector
        self.assertEqual(dash._wid("cpu-load", None), "cpu-load")
        self.assertEqual(dash._wid("cpu-load", {}),
                         "cpu-load")
        self.assertEqual(dash._wid("cpu-load", {"view": "history"}),
                         "cpu-load@view=history")
        self.assertEqual(dash._wid("w", {"b": "1", "a": "2"}), "w@a=2&b=1")
        self.assertEqual(dash._wid("tty", {"cmd": 'watch "x"&y'}),
                         "tty@cmd=watch %22x%22%26y")


class TestLayoutStore(PlatformTestBase):
    def test_round_trip(self):
        layout = {"widgets": [{"id": "w-good", "x": 0, "y": 0, "w": 2, "h": 1}]}
        self.store.save("dev1", layout)
        # load fills the T14 shuffle default so every consumer sees the key
        self.assertEqual(self.store.load("dev1"),
                         {**layout, "shuffle": {"interval_min": 0}})

    def test_rejects_out_of_grid(self):
        bad = {"widgets": [{"id": "x", "x": 11, "y": 0, "w": 4, "h": 1}]}
        with self.assertRaises(ValueError):
            self.store.save("dev1", bad)

    def test_items_may_carry_per_instance_options(self):
        # T17: options ride the layout item, structure-validated only
        self.assertIsNone(layout_mod._validate_layout({"widgets": [
            {"id": "w", "x": 0, "y": 0, "w": 1, "h": 1,
             "options": {"view": "history"}}]}))
        self.assertIn("options", layout_mod._validate_layout({"widgets": [
            {"id": "w", "x": 0, "y": 0, "w": 1, "h": 1,
             "options": {"view": 5}}]}))

    def test_shuffle_never_touches_canonical(self):
        canonical = {"widgets": [{"id": "w-good", "x": 0, "y": 0, "w": 2, "h": 1}]}
        self.store.save("dev2", canonical)
        permuted = {"widgets": [{"id": "w-good", "x": 3, "y": 0, "w": 2, "h": 1}]}
        self.store.save("dev2", permuted, shuffled=True)
        # D28: the shuffle write never touched the canonical arrangement
        self.assertEqual(self.store.load("dev2"),
                         {**canonical, "shuffle": {"interval_min": 0}})
        state = self.store.shuffle_state("dev2")
        self.assertTrue(state["shuffled"])
        self.assertEqual(state["widgets"], permuted["widgets"])

    def test_canonical_save_clears_stale_shuffle(self):
        # T14: after a canonical save the stored permutation may reference
        # moved widgets — dropped so the panel reshuffles from canonical
        canonical = {"widgets": [{"id": "w-good", "x": 0, "y": 0, "w": 2, "h": 1}]}
        self.store.save("dev4", canonical)
        permuted = {"widgets": [{"id": "w-good", "x": 3, "y": 0, "w": 2, "h": 1}]}
        self.store.save("dev4", permuted, shuffled=True)
        self.assertIsNotNone(self.store.shuffle_state("dev4"))
        edited = {"widgets": [{"id": "w-good", "x": 1, "y": 0, "w": 2, "h": 1}]}
        self.store.save("dev4", edited)
        self.assertIsNone(self.store.shuffle_state("dev4"))

    def test_shuffle_config_validated(self):
        base = {"widgets": [{"id": "w-good", "x": 0, "y": 0, "w": 2, "h": 1}]}
        with self.assertRaises(ValueError):
            self.store.save("dev5", {**base, "shuffle": {"interval_min": -5}})
        self.store.save("dev5", {**base, "shuffle": {"interval_min": 30}})
        self.assertEqual(self.store.load("dev5")["shuffle"], {"interval_min": 30})

    def test_corrupt_file_falls_back_to_default(self):
        self.store.save("dev3", {"widgets": [
            {"id": "w-good", "x": 0, "y": 0, "w": 2, "h": 1}]})
        path = self.store.dir / "dev3.json"
        path.write_text("{not json")                       # simulate corruption
        loaded = self.store.load("dev3")
        self.assertEqual(loaded,
                         {**layout_mod.DEFAULT_LAYOUT, "shuffle": {"interval_min": 0}})
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


class TestFlairLayout(PlatformTestBase):
    """T13: the flair config rides the layout doc (validated, normalized)."""

    def test_put_then_get_roundtrip(self):
        layout = {"widgets": [{"id": "w-good", "x": 0, "y": 0, "w": 2, "h": 1}],
                  "flair": {"scene": "city", "theme": "noir", "motion": "forward",
                            "glass": 0.5, "spectrum": False, "stars": False}}
        status, _, _ = _http(f"{self.base}/api/layout?device=flair-dev", method="PUT",
                             token=self.token.token, body=layout)
        self.assertEqual(status, 200)
        status, _, doc = _http(f"{self.base}/api/layout?device=flair-dev",
                               token=self.token.token)
        self.assertEqual(status, 200)
        # defaults fill the keys the editor did not send (road, scanlines)
        self.assertEqual(doc["flair"], {
            "scene": "city", "theme": "noir", "motion": "forward",
            "glass": 0.5, "speed": 25, "spectrum": False, "stars": False,
            "road": False, "scanlines": True, "enabled": True})

    def test_glass_clamped_and_unknown_keys_dropped(self):
        layout = {"widgets": [], "flair": {"glass": 1.7, "scene": "grid",
                                           "nonsense": True}}
        status, _, _ = _http(f"{self.base}/api/layout?device=flair-dev", method="PUT",
                             token=self.token.token, body=layout)
        self.assertEqual(status, 200)
        _, _, doc = _http(f"{self.base}/api/layout?device=flair-dev",
                          token=self.token.token)
        self.assertEqual(doc["flair"]["glass"], 1.0)
        self.assertNotIn("nonsense", doc["flair"])

    def test_bad_flair_400(self):
        for flair in ({"scene": "bogus"}, {"theme": 7}, {"motion": "sideways"},
                      {"glass": "x"}, "yes"):
            layout = {"widgets": [], "flair": flair}
            status, _, _ = _http(f"{self.base}/api/layout?device=flair-dev",
                                 method="PUT", token=self.token.token, body=layout)
            self.assertEqual(status, 400, f"flair={flair!r}")

    def test_dashboard_renders_flair_defaults(self):
        # D29: synthwave on from first boot — a layout without a flair key
        # still injects the default config and the glass variable
        status, _, page = _http(f"{self.base}/", token=self.token.token)
        self.assertEqual(status, 200)
        self.assertIn("window.TLCD_FLAIR=", page)
        self.assertIn('"scene": "sunset"', page)
        self.assertIn("--tlcd-glass: 0.82", page)
        self.assertIn('id="flair-scene"', page)
        self.assertIn("/assets/flair.js", page)

    def test_dashboard_renders_saved_flair(self):
        layout = {"widgets": [], "flair": {"scene": "mesa", "theme": "cyber"}}
        # the dashboard serves device "default"
        _http(f"{self.base}/api/layout?device=default", method="PUT",
              token=self.token.token, body=layout)
        _, _, page = _http(f"{self.base}/", token=self.token.token)
        self.assertIn('"scene": "mesa"', page)
        self.assertIn('"theme": "cyber"', page)
        # cleanup: restore the stock default layout for other tests
        _http(f"{self.base}/api/layout?device=default", method="PUT",
              token=self.token.token, body=layout_mod.DEFAULT_LAYOUT)


class TestMultiPanel(PlatformTestBase):
    """T15: per-device layouts with the shared default as fallback."""

    def test_device_without_own_layout_falls_back_to_default(self):
        loaded = self.store.load("panel-nofile")
        self.assertEqual(loaded, {**layout_mod.DEFAULT_LAYOUT,
                                  "shuffle": {"interval_min": 0}})

    def test_first_save_forks_the_device_layout(self):
        layout = {"widgets": [{"id": "w-good", "x": 4, "y": 0, "w": 2, "h": 1}]}
        self.store.save("fork-dev", layout)
        self.assertEqual(self.store.load("fork-dev"),
                         {**layout, "shuffle": {"interval_min": 0}})
        # the shared default file is untouched by the fork
        self.assertNotIn(
            {"id": "w-good", "x": 4, "y": 0, "w": 2, "h": 1},
            self.store.load("default")["widgets"])

    def test_shuffle_state_falls_back_to_default(self):
        permuted = {"widgets": [{"id": "w-good", "x": 5, "y": 0, "w": 2, "h": 1}]}
        self.store.save("default", permuted, shuffled=True)
        try:
            # a panel rendering the default arrangement inherits its state
            state = self.store.shuffle_state("panel-x")
            self.assertTrue(state["shuffled"])
            # an own state shadows the fallback
            own = {"widgets": [{"id": "w-good", "x": 6, "y": 0, "w": 2, "h": 1}]}
            self.store.save("panel-x", own, shuffled=True)
            self.assertEqual(self.store.shuffle_state("panel-x")["widgets"],
                             own["widgets"])
        finally:
            self.store.clear_shuffle("default")
            self.store.clear_shuffle("panel-x")

    def test_dashboard_page_carries_device_and_profile_tokens(self):
        layout = {"widgets": [{"id": "w-good", "x": 0, "y": 0, "w": 2, "h": 1}]}
        self.store.save("scope-dev", layout)
        status, _, page = _http(f"{self.base}/?device=scope-dev",
                                token=self.token.token)
        self.assertEqual(status, 200)
        self.assertIn('window.TLCD_DEVICE="scope-dev"', page)
        self.assertIn('data-wid="w-good"', page)
        # absent profile: the 9.16" default sizes the canvas
        self.assertIn("width: 1920px", page)

    def test_render_dashboard_uses_profile_dims(self):
        page = dash._render_dashboard(
            self.server, [], [], "tok", {}, "d", {"w": 1280, "h": 480})
        self.assertIn("width: 1280px", page)
        self.assertIn("height: 480px", page)
        self.assertIn('width="1280"', page)
        self.assertIn('window.TLCD_DEVICE="d"', page)

    def test_render_dashboard_tokenizes_flair_src(self):
        # flair.js is a guarded subresource: subresources never inherit the
        # page's ?token=, so the src must carry the token itself
        page = dash._render_dashboard(self.server, [], [], "tok", {}, "d")
        self.assertIn('src="/assets/flair.js?token=tok"', page)
        self.assertNotIn("__TOKEN_URL__", page)

    def test_devices_endpoint_empty_without_profiles(self):
        # the shared test server has no data dir: no devices reported
        status, _, body = _http(f"{self.base}/api/devices",
                                token=self.token.token)
        self.assertEqual((status, body["devices"]), (200, []))
        status, _, _ = _http(f"{self.base}/api/devices")
        self.assertEqual(status, 401)          # token-gated like the rest


class TestProfiles(unittest.TestCase):
    """T15: the capture's profile state files — /api/devices reads them."""

    def test_load_profiles_reads_and_heartbeats(self):
        import os
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        profiles = Path(tmp.name) / "profiles"
        profiles.mkdir()
        (profiles / "p1.json").write_text(json.dumps(
            {"id": "p1", "serial": "S", "w": 1920, "h": 462, "pm": 65, "sub": 3}))
        doc = dash.load_profiles(tmp.name)
        self.assertTrue(doc["p1"]["online"])           # just written
        os.utime(profiles / "p1.json", (0, 0))         # stale heartbeat
        self.assertFalse(dash.load_profiles(tmp.name)["p1"]["online"])
        # malformed state is skipped, not fatal
        (profiles / "bad.json").write_text("{nope")
        (profiles / "empty.json").write_text('{"id": "x"}')  # missing w/h
        self.assertEqual(set(dash.load_profiles(tmp.name)), {"p1"})


if __name__ == "__main__":
    unittest.main()
