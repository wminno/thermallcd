"""Platform-core tests: auth matrix, registry failure reasons, layout store.

Covers the verify obligations from the plan (T1/T5/T6): 401/401/200 auth,
registry skip + failure-reason payload (D25), layout round-trip + corrupt
recovery (EC1).
"""

import json
import tempfile
import threading
import time
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

from thermallcd import dashboard as dash
from thermallcd import layout as layout_mod
from thermallcd import weather as weather_mod


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

    def test_tty_endpoint_auth_then_404_without_slot(self):
        # tty widget (T18): same posture as nvtop — unauthenticated 401;
        # unplaced or command-less placement answers 404 and never forks
        status, _, _ = _http(f"{self.base}/api/tty")
        self.assertEqual(status, 401)
        self.store.save("default", {"widgets": [
            {"id": "w-good", "x": 0, "y": 0, "w": 2, "h": 1}]})
        status, _, body = _http(f"{self.base}/api/tty", token=self.token.token)
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
        # malformed: scene shows through — no frame, though the T28 pages
        # payload legitimately lists the full arrangement for the cycler
        self.assertNotIn('data-wid="w-bad"', body)
        self.assertIn("srcdoc=", body)               # per-widget iframe (D21)
        self.assertIn('data-wid="w-good"', body)     # live-preview targeting
        self.assertIn("TLCD_WIDGETS", body)          # the placed arrangement

    def test_pages_doc_renders_page_zero_and_ships_the_cycle(self):
        # T28: only page 0 bakes boot frames; the full page list + interval
        # ride the __PAGES__/__CYCLE_MIN__ tokens for the client's cycler
        self.store.save("default", {
            "cycle_min": 3,
            "pages": [
                {"name": "Main", "widgets": [
                    {"id": "w-good", "x": 0, "y": 0, "w": 2, "h": 1}]},
                {"name": "Alt", "widgets": [
                    {"id": "w-good", "x": 2, "y": 0, "w": 2, "h": 1}]}]})
        status, _, body = _http(f"{self.base}/", token=self.token.token)
        self.assertEqual(status, 200)
        self.assertEqual(body.count("<iframe"), 1)   # page 0 only
        self.assertIn('data-wid="w-good"', body)
        self.assertIn("window.TLCD_PAGES=", body)
        self.assertIn("window.TLCD_CYCLE_MIN=3", body)
        self.assertIn('"name": "Alt"', body)

    def test_page_names_cannot_close_the_script_tag(self):
        # page names are user text baked into an inline <script>; json.dumps
        # does not escape /, so "</" is escaped to "<\/" (valid JSON, parses
        # identically) or a page name could inject script content
        self.store.save("default", {
            "pages": [{"name": "</script>x", "widgets": [
                {"id": "w-good", "x": 0, "y": 0, "w": 2, "h": 1}]}]})
        status, _, body = _http(f"{self.base}/", token=self.token.token)
        self.assertEqual(status, 200)
        self.assertNotIn("</script>x", body)
        self.assertIn("<\\/script>x", body)

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
        # T28: save normalizes to the pages shape — flat input becomes one
        # page, so canonical files converge on the current shape
        layout = {"widgets": [{"id": "w-good", "x": 0, "y": 0, "w": 2, "h": 1}]}
        saved = self.store.save("dev1", layout)
        self.assertEqual(self.store.load("dev1"), saved)
        self.assertEqual(saved["pages"], [
            {"name": "Main",
             "widgets": [{"id": "w-good", "x": 0, "y": 0, "w": 2, "h": 1}]}])
        self.assertEqual(saved["cycle_min"], 0)

    def test_rejects_out_of_grid(self):
        bad = {"widgets": [{"id": "x", "x": 11, "y": 0, "w": 4, "h": 1}]}
        with self.assertRaises(ValueError):
            self.store.save("dev1", bad)

    def test_items_may_carry_per_instance_options(self):
        # T17: options ride the layout item, structure-validated only
        self.assertIsNone(layout_mod._validate_layout({"widgets": [
            {"id": "w", "x": 0, "y": 0, "w": 1, "h": 1,
             "options": {"view": "history"}}]}))

    def test_null_options_pass_validation(self):
        # T29: the panel render bakes "options": null for optionless
        # widgets; a null-valued key must validate like an absent key
        self.assertIsNone(layout_mod._validate_layout({"widgets": [
            {"id": "w", "x": 0, "y": 0, "w": 1, "h": 1, "options": None}]}))

    def test_devices_enumerates_saves_and_excludes_dead_files(self):
        # A1: every device the editor has saved (default included) is its
        # own canonical file; shuffle sidecars and corrupt-renames are dead
        # data that must not enumerate as devices
        self.store.save("2025ACFC9E03", {"widgets": []})
        self.store.save("dev2", {"widgets": []})
        (self.store.dir / "dev1.shuffle.json").write_text("{}")
        (self.store.dir / "dev2.corrupt-9.json").write_text("{}")
        (self.store.dir / "dev2.json.abc.tmp").write_text("{}")  # atomic temp
        self.assertEqual(self.store.devices(), ["2025ACFC9E03", "dev2"])


class TestPagesLayout(PlatformTestBase):
    """T28: widget pages cycling on a timer. The canonical doc is
    {cycle_min, pages: [{name, widgets}]}; flat docs stay valid so
    pre-pages files keep loading, and save() normalizes everything."""

    def test_pages_doc_round_trips_with_cycle_min(self):
        pages = {"cycle_min": 7.4,
                 "pages": [
                     {"name": "Main", "widgets": [
                         {"id": "w-good", "x": 0, "y": 0, "w": 2, "h": 1}]},
                     {"name": "GPU", "widgets": [
                         {"id": "w-good", "x": 2, "y": 0, "w": 2, "h": 1}]}]}
        saved = self.store.save("pages-dev", pages)
        self.assertEqual(saved["cycle_min"], 7)   # rotate_min precedent: int()
        self.assertEqual(self.store.load("pages-dev"), saved)

    def test_migration_pin_flat_doc_with_stale_shuffle_loads_verbatim(self):
        # the live pre-T28 file shape: flat widgets, full flair, and the
        # removed shuffle key riding along. The dual-shape validator must
        # keep accepting it or the restart trips the EC1 corrupt-rename and
        # the user's arrangement is lost. First save converges the shape.
        legacy = {
            "widgets": [{"id": "nvtop", "x": 0, "y": 0, "w": 6, "h": 3}],
            "flair": dict(layout_mod.DEFAULT_FLAIR, scene="city"),
            "shuffle": {"interval_min": 2}}
        (self.store.dir / "legacy.json").write_text(json.dumps(legacy))
        self.assertEqual(self.store.load("legacy"), legacy)
        saved = self.store.save("legacy", legacy)
        self.assertNotIn("shuffle", saved)
        self.assertEqual(saved["pages"], [
            {"name": "Main", "widgets": legacy["widgets"]}])

    def test_rejects_bad_pages(self):
        good = {"id": "w-good", "x": 0, "y": 0, "w": 2, "h": 1}
        cases = [
            ({"pages": [], "cycle_min": 0}, "non-empty array"),
            ({"pages": "nope"}, "non-empty array"),
            ({"pages": [{"name": "", "widgets": [good]}]}, "needs a name"),
            ({"pages": [{"name": "x" * 41, "widgets": [good]}]}, "longer than 40"),
            ({"pages": [{"name": "A", "widgets": [good]},
                        {"name": "A", "widgets": []}]}, "used twice"),
            ({"pages": [{"name": "A"}]}, "needs a widgets array"),
            ({"pages": [{"name": "A", "widgets": [good]}], "cycle_min": -1},
             "cycle_min"),
            ({"pages": [{"name": "A", "widgets": [good]}], "cycle_min": "soon"},
             "cycle_min"),
            ({"pages": [{"name": "A", "widgets": [good]}], "cycle_min": True},
             "cycle_min"),
            ({"pages": [{"name": "A", "widgets": [
                {"id": "x", "x": 11, "y": 0, "w": 4, "h": 1}]}]},
             "page 'A': "),   # item errors are page-scoped
        ]
        for bad, fragment in cases:
            with self.subTest(bad=bad):
                message = layout_mod._validate_layout(bad)
                self.assertIn(fragment, message)

    def test_pages_of_wraps_flat_and_find_item_scans_pages(self):
        flat = {"widgets": [{"id": "a", "x": 0, "y": 0, "w": 1, "h": 1}]}
        self.assertEqual(layout_mod.pages_of(flat), [
            {"name": "Main", "widgets": flat["widgets"]}])
        paged = {"pages": [
            {"name": "P1", "widgets": [{"id": "nvtop", "x": 0, "y": 0,
                                        "w": 4, "h": 2}]},
            {"name": "P2", "widgets": [{"id": "tty", "x": 0, "y": 0,
                                        "w": 2, "h": 1}]}]}
        self.assertIsNone(layout_mod.find_item(flat, "nvtop"))
        self.assertEqual(layout_mod.find_item(paged, "tty")["w"], 2)


class TestTtySource(PlatformTestBase):
    # T18: the pane derives entirely from the layout's tty item — the
    # command is per-instance options (T17), never request input
    def _tty_store(self):
        store = layout_mod.LayoutStore(Path(self.tmp.name) / "layouts")
        return store

    def test_image_none_without_slot_or_command(self):
        from thermallcd import tty_pane
        src = tty_pane.TtySource(self._tty_store())
        try:
            self.assertIsNone(src.image())            # not placed
            self._tty_store().save("default", {"widgets": [
                {"id": "tty", "x": 0, "y": 0, "w": 3, "h": 2}]})
            self.assertIsNone(src.image())            # placed, no cmd
            self._tty_store().save("default", {"widgets": [
                {"id": "tty", "x": 0, "y": 0, "w": 3, "h": 2,
                 "options": {"cmd": "   "}}]})
            self.assertIsNone(src.image())            # blank cmd
        finally:
            src.stop()

    def test_image_png_when_command_set(self):
        from thermallcd import tty_pane
        store = self._tty_store()
        store.save("default", {"widgets": [
            {"id": "tty", "x": 0, "y": 0, "w": 3, "h": 2,
             "options": {"cmd": "printf t18-ok"}}]})
        src = tty_pane.TtySource(store)
        try:
            data = src.image()
            self.assertTrue(data.startswith(b"\x89PNG"))
            # same command + box reuses the pane; a new command respawns
            self.assertEqual(src._cmd, "printf t18-ok")
            pane = src._pane
            self.assertIs(src.image() and pane, pane)
            store.save("default", {"widgets": [
                {"id": "tty", "x": 0, "y": 0, "w": 3, "h": 2,
                 "options": {"cmd": "printf t18-v2"}}]})
            deadline = threading.Event()
            deadline.wait(0.2)
            src.image()
            self.assertIsNot(src._pane, pane)
            self.assertEqual(src._cmd, "printf t18-v2")
        finally:
            src.stop()
        self.assertIn("options", layout_mod._validate_layout({"widgets": [
            {"id": "w", "x": 0, "y": 0, "w": 1, "h": 1,
             "options": {"view": 5}}]}))

    def test_pages_layout_first_page_wins(self):
        # T28: the pane scans every page in document order; the server
        # cannot know which page a cycling panel shows, so the first match
        # wins — including a tty with a different cmd per page
        from thermallcd import tty_pane
        store = self._tty_store()
        store.save("default", {
            "pages": [
                {"name": "P1", "widgets": [
                    {"id": "tty", "x": 0, "y": 0, "w": 3, "h": 2,
                     "options": {"cmd": "printf p1"}}]},
                {"name": "P2", "widgets": [
                    {"id": "tty", "x": 0, "y": 0, "w": 2, "h": 1,
                     "options": {"cmd": "printf p2"}}]}]})
        src = tty_pane.TtySource(store)
        try:
            self.assertEqual(src._tty_item()["options"]["cmd"], "printf p1")
            self.assertTrue(src.image().startswith(b"\x89PNG"))
        finally:
            src.stop()

    def test_slot_size_on_pages_layout(self):
        # nvtop placed only on page 2: the scanner finds it there
        from thermallcd import nvtop_pane
        store = self._tty_store()
        store.save("default", {
            "pages": [
                {"name": "P1", "widgets": []},
                {"name": "P2", "widgets": [
                    {"id": "nvtop", "x": 0, "y": 0, "w": 4, "h": 2}]}]})
        src = nvtop_pane.NvtopSource(store)
        try:
            self.assertEqual(src._slot_size(), (4, 2))
        finally:
            src.stop()

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
        # T28: the stored doc echoes the normalized pages shape
        self.assertEqual(doc["pages"],
                         [{"name": "Main", "widgets": layout["widgets"]}])

    def test_put_pages_shape_round_trips(self):
        pages = {"cycle_min": 1,
                 "pages": [{"name": "Main", "widgets": [
                     {"id": "w-good", "x": 2, "y": 1, "w": 2, "h": 1}]}]}
        status, _, _ = _http(f"{self.base}/api/layout?device=pages-dev",
                             method="PUT", token=self.token.token, body=pages)
        self.assertEqual(status, 200)
        status, _, doc = _http(f"{self.base}/api/layout?device=pages-dev",
                               token=self.token.token)
        self.assertEqual(status, 200)
        self.assertEqual(doc["pages"], pages["pages"])
        self.assertEqual(doc["cycle_min"], 1)

    def test_put_bad_pages_400(self):
        for body in ({"pages": []},
                     {"pages": [{"name": "", "widgets": []}]},
                     {"cycle_min": 2, "pages": "nope"}):
            with self.subTest(body=body):
                status, _, _ = _http(
                    f"{self.base}/api/layout?device=pages-bad", method="PUT",
                    token=self.token.token, body=body)
                self.assertEqual(status, 400)

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
        # defaults fill the keys the editor did not send (road, scanlines,
        # rotation)
        self.assertEqual(doc["flair"], {
            "scene": "city", "theme": "noir", "motion": "forward",
            "glass": 0.5, "speed": 25, "spectrum": False, "stars": False,
            "road": False, "scanlines": True, "enabled": True,
            "rotate_min": 0, "rotate_what": "scene"})

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

    def test_rotation_fields_roundtrip(self):
        # T27: rotate_min coerces to a non-negative int, rotate_what is
        # whitelisted, and both ride the doc the dashboard page bakes in
        layout = {"widgets": [], "flair": {"scene": "ridge",
                                           "rotate_min": 7.4,
                                           "rotate_what": "all"}}
        status, _, _ = _http(f"{self.base}/api/layout?device=flair-dev", method="PUT",
                             token=self.token.token, body=layout)
        self.assertEqual(status, 200)
        _, _, doc = _http(f"{self.base}/api/layout?device=flair-dev",
                          token=self.token.token)
        self.assertEqual(doc["flair"]["rotate_min"], 7)
        self.assertEqual(doc["flair"]["rotate_what"], "all")

    def test_bad_rotation_fields_400(self):
        for flair in ({"rotate_min": -1}, {"rotate_min": "soon"},
                      {"rotate_what": "everything"}):
            layout = {"widgets": [], "flair": flair}
            status, _, _ = _http(f"{self.base}/api/layout?device=flair-dev",
                                 method="PUT", token=self.token.token, body=layout)
            self.assertEqual(status, 400, f"flair={flair!r}")

    def test_dashboard_renders_rotation_config(self):
        layout = {"widgets": [], "flair": {"rotate_min": 5, "rotate_what": "all"}}
        _http(f"{self.base}/api/layout?device=default", method="PUT",
              token=self.token.token, body=layout)
        _, _, page = _http(f"{self.base}/", token=self.token.token)
        self.assertIn('"rotate_min": 5', page)
        self.assertIn('"rotate_what": "all"', page)
        # cleanup: restore the stock default layout for other tests
        _http(f"{self.base}/api/layout?device=default", method="PUT",
              token=self.token.token, body=layout_mod.DEFAULT_LAYOUT)

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
        self.assertEqual(loaded, layout_mod.DEFAULT_LAYOUT)

    def test_first_save_forks_the_device_layout(self):
        layout = {"widgets": [{"id": "w-good", "x": 4, "y": 0, "w": 2, "h": 1}]}
        saved = self.store.save("fork-dev", layout)
        self.assertEqual(self.store.load("fork-dev"), saved)
        # the shared default file is untouched by the fork
        self.assertNotIn(
            {"id": "w-good", "x": 4, "y": 0, "w": 2, "h": 1},
            self.store.load("default")["widgets"])

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


class TestMemSwap(unittest.TestCase):
    """T25: the mem block carries used_kb and a swap block for the widget."""

    def _sample(self, meminfo):
        sampler = dash.metrics_mod.MetricsSampler(interval=0.05)
        with mock.patch.object(dash.metrics_mod, "_read_meminfo",
                               return_value=dict(meminfo)):
            return sampler._sample_once()

    def test_used_kb_and_swap_fields(self):
        data = self._sample({"MemTotal": 8000000, "MemAvailable": 3000000,
                             "SwapTotal": 2000000, "SwapFree": 1500000})
        self.assertEqual(data["mem"]["used_kb"], 5000000)
        self.assertEqual(data["mem"]["swap"]["total_kb"], 2000000)
        self.assertEqual(data["mem"]["swap"]["used_kb"], 500000)
        self.assertEqual(data["mem"]["swap"]["used_pct"], 25.0)

    def test_no_swap_reports_zero_total_and_none_pct(self):
        # SwapTotal present but 0: the widget shows "no swap configured"
        data = self._sample({"MemTotal": 8000000, "MemAvailable": 3000000,
                             "SwapTotal": 0, "SwapFree": 0})
        self.assertEqual(data["mem"]["swap"]["total_kb"], 0)
        self.assertIsNone(data["mem"]["swap"]["used_pct"])

    def test_missing_swaptotal_omits_block(self):
        # a kernel without swap support lists no SwapTotal at all
        data = self._sample({"MemTotal": 8000000, "MemAvailable": 3000000})
        self.assertNotIn("swap", data["mem"])


class TestLlmTasks(unittest.TestCase):
    """T26: the llm-tasks source — loaded models plus LLM workers."""

    def test_parse_models_keeps_only_loaded(self):
        doc = {"data": [
            {"id": "qwen-38b-unc-flash", "status": {"value": "loaded"}},
            {"id": "gemma-4", "status": {"value": "unloaded"}},
            {"id": "no-status"},
            {"id": "weird-status", "status": "loaded"},   # not a dict: skip
            {"status": {"value": "loaded"}},               # no id: skip
            "garbage",
        ]}
        self.assertEqual(dash.metrics_mod._parse_models(doc),
                         ["qwen-38b-unc-flash"])
        self.assertEqual(dash.metrics_mod._parse_models(None), [])
        self.assertEqual(dash.metrics_mod._parse_models({}), [])

    def test_loaded_models_down_swap_yields_empty(self):
        # closed port: the source is simply not there, never faked (D23)
        self.assertEqual(
            dash.metrics_mod._loaded_models("http://127.0.0.1:1/v1/models"),
            [])

    def test_llm_processes_scan_fake_proc(self):
        with tempfile.TemporaryDirectory() as tmp:
            proc = Path(tmp)
            for pid, argv, rss, threads in (
                ("2000", b"/opt/swap/llama-server\0--port 11012\0--model x.gguf\0",
                 "21159600 kB", "38"),
                ("2001", b"bash\0-c\0llama-server --port 1\0", "0", "1"),
                ("2002", b"", "0", "0"),  # zombie: empty cmdline, skip
            ):
                d = proc / pid
                d.mkdir()
                (d / "cmdline").write_bytes(argv)
                (d / "status").write_bytes(
                    f"VmRSS:\t{rss}\nThreads:\t{threads}\n".encode())
            tasks = dash.metrics_mod._llm_processes(root=str(proc))
        self.assertEqual(len(tasks), 1)  # bash's argv[0] is not a worker
        self.assertEqual(tasks[0]["id"], "2000")
        self.assertEqual(tasks[0]["name"], "llama-server")
        self.assertEqual(tasks[0]["status"], "20.2G · 38 thr")

    def test_sample_wires_llm_tasks(self):
        sampler = dash.metrics_mod.MetricsSampler(interval=0.05)
        with mock.patch.object(dash.metrics_mod, "_loaded_models",
                               return_value=["qwen-38b-unc-flash"]), \
             mock.patch.object(dash.metrics_mod, "_llm_processes",
                               return_value=[{"id": "77", "name": "llama-server",
                                              "status": "20.2G · 38 thr"}]):
            data = sampler._sample_once()
        self.assertEqual(data["llm_tasks"], [
            {"id": "qwen-38b-unc-flash", "name": "qwen-38b-unc-flash",
             "status": "loaded"},
            {"id": "77", "name": "llama-server", "status": "20.2G · 38 thr"},
        ])


class TestWeather(unittest.TestCase):
    """T20: the Open-Meteo source — coords passthrough, geocode cache,
    TTL + stale-while-error, and the snapshot publish."""

    LOC = "52.09,5.12"
    _GEOCODE = {"results": [{"name": "Utrecht", "latitude": 52.09,
                             "longitude": 5.12, "admin1": "Utrecht",
                             "country": "Netherlands"}]}
    _FORECAST = {"current": {"temperature_2m": 18.3,
                             "apparent_temperature": 16.8, "weather_code": 3},
                 "daily": {"time": ["2026-10-15", "2026-10-16", "2026-10-17"],
                           "temperature_2m_max": [21.1, 19.8, 20.0],
                           "temperature_2m_min": [9.4, 10.2, 8.9],
                           "weather_code": [3, 61, 1]}}

    def _source(self, root):
        store = layout_mod.LayoutStore(Path(root) / "layouts")
        return weather_mod.WeatherSource(store), store

    def _save(self, store, placed):
        widgets = [{"id": "weather", "x": 0, "y": 0, "w": 3, "h": 2,
                    "options": {"location": self.LOC}}] if placed else [
            {"id": "clock", "x": 0, "y": 0, "w": 2, "h": 2}]
        store.save("default", {"widgets": widgets})

    def test_parse_coords_passthrough_and_rejects(self):
        self.assertEqual(weather_mod._parse_coords("52.09,5.12"), (52.09, 5.12))
        self.assertEqual(weather_mod._parse_coords(" 52.09, 5.12 "), (52.09, 5.12))
        self.assertIsNone(weather_mod._parse_coords("Utrecht"))
        self.assertIsNone(weather_mod._parse_coords("52.09"))
        self.assertIsNone(weather_mod._parse_coords("95,200"))  # lon out of range
        self.assertIsNone(weather_mod._parse_coords(""))

    def test_geocode_caches_and_misses(self):
        with tempfile.TemporaryDirectory() as tmp:
            src, _ = self._source(tmp)
            with mock.patch.object(weather_mod, "_fetch_json",
                                   return_value=self._GEOCODE) as fe:
                first = src._resolve("Utrecht")
                self.assertEqual(first, (52.09, 5.12,
                                         {"name": "Utrecht", "admin1": "Utrecht",
                                          "country": "Netherlands"}))
                self.assertEqual(fe.call_count, 1)
                self.assertEqual(src._resolve("Utrecht"), first)
                self.assertEqual(fe.call_count, 1)  # day cache: no second fetch
            with mock.patch.object(weather_mod, "_fetch_json",
                                   return_value={"results": []}):
                self.assertIsNone(src._resolve("Nowhere"))
            with mock.patch.object(weather_mod, "_fetch_json",
                                   return_value={"error": True, "reason": "x"}):
                self.assertIsNone(src._resolve("Elsewhere"))
            src.stop()

    def test_fetch_location_coords_shape(self):
        with tempfile.TemporaryDirectory() as tmp:
            src, _ = self._source(tmp)
            with mock.patch.object(weather_mod, "_fetch_json",
                                   return_value=self._FORECAST) as fe:
                entry = src._fetch_location(self.LOC)
            self.assertEqual(fe.call_count, 1)  # coords: no geocoding fetch
            self.assertEqual(entry["state"], "ok")
            self.assertEqual(entry["ttl"], weather_mod.TTL_S)
            self.assertEqual(entry["resolved"], {"name": self.LOC})
            self.assertEqual(entry["current"]["temperature_2m"], 18.3)
            self.assertEqual(len(entry["daily"]["weather_code"]), 3)
            src.stop()

    def test_fetch_location_geocode_miss_is_an_error_entry(self):
        with tempfile.TemporaryDirectory() as tmp:
            src, _ = self._source(tmp)
            with mock.patch.object(weather_mod, "_fetch_json",
                                   return_value={"results": []}):
                entry = src._fetch_location("Xyzzyville")
            self.assertEqual(entry["state"], "error")
            self.assertEqual(entry["error"], "location not found")
            self.assertEqual(entry["ttl"], weather_mod.RETRY_S)
            src.stop()

    def test_fetch_json_down_url_raises(self):
        # closed port: the service is simply not there; the caller keeps the
        # last good entry or publishes an honest error, never fake data (D23)
        with self.assertRaises(Exception):
            weather_mod._fetch_json("http://127.0.0.1:1/v1/forecast")

    def test_refresh_once_ttls_then_refetches(self):
        with tempfile.TemporaryDirectory() as tmp:
            src, store = self._source(tmp)
            self._save(store, placed=True)
            with mock.patch.object(weather_mod, "_fetch_json",
                                   return_value=self._FORECAST) as fe:
                src.refresh_once()
                self.assertEqual(src.snapshot()[self.LOC]["state"], "ok")
                n = fe.call_count
                src.refresh_once()  # still fresh: no refetch
                self.assertEqual(fe.call_count, n)
                src._expires[self.LOC] = 0  # force the deadline
                src.refresh_once()
                self.assertEqual(fe.call_count, n + 1)
            src.stop()

    def test_refresh_once_keeps_last_good_on_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            src, store = self._source(tmp)
            self._save(store, placed=True)
            with mock.patch.object(weather_mod, "_fetch_json",
                                   return_value=self._FORECAST):
                src.refresh_once()
            good = src.snapshot()[self.LOC]
            self.assertEqual(good["state"], "ok")
            src._expires[self.LOC] = 0  # past the deadline: a real refetch
            with mock.patch.object(weather_mod, "_fetch_json",
                                   side_effect=OSError("down")):
                src.refresh_once()
            self.assertEqual(src.snapshot()[self.LOC], good)  # same entry, same ts
            self.assertAlmostEqual(src._expires[self.LOC] - time.monotonic(),
                                   weather_mod.RETRY_S, delta=5.0)
            src.stop()

    def test_refresh_once_error_entry_when_never_fetched(self):
        with tempfile.TemporaryDirectory() as tmp:
            src, store = self._source(tmp)
            self._save(store, placed=True)
            with mock.patch.object(weather_mod, "_fetch_json",
                                   side_effect=OSError("down")):
                src.refresh_once()
            entry = src.snapshot()[self.LOC]
            self.assertEqual(entry["state"], "error")
            self.assertEqual(entry["error"], "weather service unreachable")
            self.assertNotIn("current", entry)  # no fabricated values
            src.stop()

    def test_refresh_once_evicts_removed_locations(self):
        with tempfile.TemporaryDirectory() as tmp:
            src, store = self._source(tmp)
            self._save(store, placed=True)
            with mock.patch.object(weather_mod, "_fetch_json",
                                   return_value=self._FORECAST):
                src.refresh_once()
            self.assertEqual(list(src.snapshot()), [self.LOC])
            self._save(store, placed=False)  # widget removed from the layout
            src.refresh_once()
            self.assertEqual(src.snapshot(), {})
            src.stop()

    def test_refresh_once_idles_without_weather_widget(self):
        with tempfile.TemporaryDirectory() as tmp:
            src, _ = self._source(tmp)  # fresh store: default layout, no weather
            with mock.patch.object(weather_mod, "_fetch_json") as fe:
                src.refresh_once()
            self.assertEqual(fe.call_count, 0)
            self.assertEqual(src.snapshot(), {})
            src.stop()

    def test_locations_union_across_device_layouts(self):
        # A1: the editor saves to the device's own layout file (T15), so a
        # weather widget placed there must be picked up even with no
        # weather in the shared default — the old default-only read held
        # stale locations from files the editor no longer writes
        with tempfile.TemporaryDirectory() as tmp:
            src, store = self._source(tmp)
            store.save("2025ACFC9E03", {"widgets": [
                {"id": "weather", "x": 0, "y": 0, "w": 3, "h": 2,
                 "options": {"location": "  Amersfoort, NL "}}]})
            store.save("dev2", {"widgets": [
                {"id": "weather", "x": 0, "y": 0, "w": 3, "h": 2,
                 "options": {"location": "Utrecht"}},
                {"id": "weather", "x": 3, "y": 0, "w": 3, "h": 2,
                 "options": {"location": "Utrecht"}}]})  # deduped
            store.save("dev3", {"widgets": [
                {"id": "clock", "x": 0, "y": 0, "w": 2, "h": 2}]})
            self.assertEqual(src._locations(), ["Amersfoort, NL", "Utrecht"])
            src.stop()

    def test_sample_publishes_weather_block(self):
        loc = self.LOC

        class FakeSource:
            def snapshot(self):
                return {loc: {"state": "ok", "ts": 1.0, "ttl": 900.0}}

        sampler = dash.metrics_mod.MetricsSampler(interval=0.05,
                                                  weather=FakeSource())
        data = sampler._sample_once()
        self.assertEqual(data["weather"][loc]["ts"], 1.0)
        bare = dash.metrics_mod.MetricsSampler(interval=0.05)
        self.assertNotIn("weather", bare._sample_once())  # default: key absent


    def test_suggest_shapes_and_guards(self):
        """The editor autocomplete's server half: shaped rows, short/failed
        queries degrade to [] and never fetch (D23)."""
        with mock.patch.object(weather_mod, "_fetch_json") as fe:
            fe.return_value = {"results": [
                {"name": "Utrecht", "admin1": "Utrecht",
                 "country": "Netherlands"},
                {"latitude": 1.0},  # no name: skipped, not a crash
                {"name": "Utrecht", "admin1": "Eastern Cape",
                 "country": "South Africa"}]}
            self.assertEqual(weather_mod.suggest("Utre"), [
                {"label": "Utrecht", "sub": "Utrecht, Netherlands",
                 "value": "Utrecht"},
                {"label": "Utrecht", "sub": "Eastern Cape, South Africa",
                 "value": "Utrecht"}])
            self.assertEqual(fe.call_count, 1)
        with mock.patch.object(weather_mod, "_fetch_json") as fe:
            self.assertEqual(weather_mod.suggest("u"), [])
            self.assertEqual(weather_mod.suggest("   "), [])
            self.assertEqual(weather_mod.suggest(None), [])
            self.assertEqual(fe.call_count, 0)
        with mock.patch.object(weather_mod, "_fetch_json",
                               side_effect=OSError("down")):
            self.assertEqual(weather_mod.suggest("Utrecht"), [])
        with mock.patch.object(weather_mod, "_fetch_json",
                               return_value={"results": None}):
            self.assertEqual(weather_mod.suggest("Utrecht"), [])


class TestWeatherWidget(unittest.TestCase):
    """T20: the bundled weather widget — manifest validity and option baking."""

    def test_manifest_options_and_frame_injection(self):
        registry = dash.registry_mod.Registry(
            Path(dash.__file__).parent.parent / "widgets")
        entry = registry.get("weather")
        self.assertIsNotNone(entry)
        self.assertNotIn("error", entry)
        self.assertEqual([o["key"] for o in entry["options"]],
                         ["view", "location", "units", "iconstyle"])
        self.assertEqual([c["value"] for c in entry["options"][0]["choices"]],
                         ["current", "today", "forecast"])
        # autocomplete wiring (T20): the option spec survives normalization
        self.assertEqual(entry["options"][1]["suggest"], "geocode")
        self.assertEqual(entry["options"][1]["placeholder"], "place or lat,lon")
        srcdoc = dash._widget_doc(registry, "weather", "tok",
                                  {"location": "Utrecht", "units": "f"})
        self.assertIn('window.TLCD_OPTIONS={"location": "Utrecht", "units": "f"}',
                      srcdoc)
        self.assertIn("window.TLCD_TOKEN=", srcdoc)
        plain = dash._widget_doc(registry, "weather", "tok", None)
        self.assertIn("window.TLCD_OPTIONS={}", plain)
        html = (Path(dash.__file__).parent.parent
                / "widgets" / "weather" / "index.html").read_text()
        self.assertIn("data.weather", html)
        self.assertIn("tlcd-stale", html)


class TestGeocodeAPI(PlatformTestBase):
    """T20: /api/geocode — the editor's token-guarded suggestion proxy."""

    def test_route_token_guarded_and_proxies(self):
        status, _, _ = _http(f"{self.base}/api/geocode?q=Utre")
        self.assertEqual(status, 401)
        with mock.patch.object(weather_mod, "suggest", return_value=[
                {"label": "Utrecht", "sub": "Utrecht, Netherlands",
                 "value": "Utrecht"}]) as sug:
            status, _, body = _http(f"{self.base}/api/geocode?q=Utre",
                                    token=self.token.token)
        self.assertEqual(status, 200)
        self.assertEqual(body["suggestions"], [
            {"label": "Utrecht", "sub": "Utrecht, Netherlands",
             "value": "Utrecht"}])
        sug.assert_called_once_with("Utre")


if __name__ == "__main__":
    unittest.main()
