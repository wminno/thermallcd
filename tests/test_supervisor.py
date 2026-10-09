"""Supervisor watchdog tests (T2/D8): restart on death, health gating,
backoff escalation — all with fake child commands, no real dashboard."""

import threading
import time
import unittest

from thermallcd.supervisor import Supervisor


def _sleep_cmd():
    return ["/bin/sh", "-c", "sleep 60"]


class SupervisorTest(unittest.TestCase):
    def setUp(self):
        self.healthy = True
        self.sup = Supervisor(
            data_dir="/tmp/thermallcd-sup-test", port=8999,
            check_interval=0.1, start_timeout=0.5, stable_s=60,
            backoff=(0.05, 0.1, 0.2), health_fails_max=3,
            dashboard_cmd=_sleep_cmd(), capture_cmd=_sleep_cmd(),
            health=lambda: self.healthy)
        self.thread = threading.Thread(target=self.sup.run, daemon=True)

    def tearDown(self):
        self.sup.stop()
        try:
            self.thread.join(timeout=5)
        except RuntimeError:
            return  # never started (pure unit tests) — nothing to stop
        self.assertFalse(self.thread.is_alive(), "supervisor did not stop")

    def test_starts_both_children(self):
        self.thread.start()
        time.sleep(0.8)
        self.assertTrue(self.sup._alive("dashboard"))
        self.assertTrue(self.sup._alive("capture"))
        self.assertLessEqual(self.sup.starts["dashboard"], 2)  # no churn

    def test_capture_restarted_when_it_exits(self):
        self.sup._cmds["capture"] = ["/bin/sh", "-c", "exit 42"]
        self.thread.start()
        time.sleep(1.2)
        self.assertGreaterEqual(self.sup.starts["capture"], 3)
        self.assertTrue(self.sup._alive("dashboard"))

    def test_dashboard_health_failure_restarts_pipeline(self):
        self.thread.start()
        time.sleep(0.6)
        self.assertTrue(self.sup._alive("capture"))
        d1 = self.sup.procs["dashboard"].pid
        c1 = self.sup.procs["capture"].pid
        self.healthy = False
        time.sleep(1.0)             # 3 consecutive fails at 0.1s intervals
        self.healthy = True
        time.sleep(0.8)             # pipeline comes back up
        self.assertGreater(self.sup.starts["dashboard"], 1)
        self.assertGreater(self.sup.starts["capture"], 1)
        self.assertNotEqual(self.sup.procs["dashboard"].pid, d1)
        self.assertNotEqual(self.sup.procs["capture"].pid, c1)
        self.assertTrue(self.sup._alive("capture"))

    def test_stop_terminates_children(self):
        self.thread.start()
        time.sleep(0.6)
        self.sup.stop()
        self.thread.join(timeout=5)
        self.assertFalse(self.sup._alive("dashboard"))
        self.assertFalse(self.sup._alive("capture"))

    def test_backoff_escalates_and_resets(self):
        sup = self.sup
        # fresh death each time: the ladder climbs
        for i, want in enumerate(sup.backoff):
            sup._state["capture"]["up_since"] = time.time()
            sup._state["capture"]["fails"] = i
            sup._note_death("capture")
            self.assertAlmostEqual(sup._restart_delay("capture"), want)
        # a stable stretch resets the ladder
        sup._state["capture"]["up_since"] = time.time() - sup.stable_s - 1
        sup._note_death("capture")
        self.assertAlmostEqual(sup._restart_delay("capture"), sup.backoff[0])


if __name__ == "__main__":
    unittest.main()
