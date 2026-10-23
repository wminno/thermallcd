"""capture.py unit tests: the /api/devices heartbeat (T15) and the
per-panel profile dir."""

import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from thermallcd import capture


class HeartbeatTest(unittest.TestCase):
    def test_touches_existing_profile(self):
        # regression: the first cut called the nonexistent Path.utime —
        # AttributeError killed the capture at its first stats print
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "panel.json"
            p.write_text("{}")
            old = time.time() - 120
            os.utime(p, (old, old))
            capture.heartbeat(p)
            self.assertGreater(p.stat().st_mtime, time.time() - 30)

    def test_missing_profile_is_not_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            capture.heartbeat(Path(tmp) / "absent.json")


class ProfileDirTest(unittest.TestCase):
    def test_snap_chromium_uses_snap_common(self):
        # regression: snap chromium silently ignores --user-data-dir under
        # ~/.cache (hidden dirs are outside strict confinement) — every panel
        # fell back to the shared default profile and fought over the
        # SingletonLock
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            (home / "snap" / "chromium" / "common").mkdir(parents=True)
            with mock.patch.dict(os.environ, {"HOME": str(home)}):
                self.assertEqual(
                    capture.user_profile_dir("/snap/bin/chromium", "PANEL1"),
                    str(home / "snap" / "chromium" / "common"
                        / "thermallcd-capture-PANEL1"))

    def test_non_snap_chromium_keeps_cache_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp) / "home"
            (home / ".cache").mkdir(parents=True)
            with mock.patch.dict(os.environ, {"HOME": str(home)}):
                self.assertEqual(
                    capture.user_profile_dir("/usr/bin/chromium", "PANEL1"),
                    str(home / ".cache" / "thermallcd-capture-PANEL1"))

    def test_snap_detection_via_usr_bin_snap(self):
        # on snap systems /usr/bin/chromium-browser symlinks to the snapd
        # launcher — realpath it
        with mock.patch("os.path.realpath", return_value="/usr/bin/snap"):
            self.assertTrue(capture._is_snap_chromium("/x/chromium-browser"))
        with mock.patch("os.path.realpath", return_value="/usr/bin/chromium"):
            self.assertFalse(capture._is_snap_chromium("/x/chromium-browser"))


if __name__ == "__main__":
    unittest.main()
