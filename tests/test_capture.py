"""capture.py unit tests: the /api/devices heartbeat (T15)."""

import os
import tempfile
import time
import unittest
from pathlib import Path

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


if __name__ == "__main__":
    unittest.main()
