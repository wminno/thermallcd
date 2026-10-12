"""Supervisor watchdog tests (T2/D8): restart on death, health gating,
backoff escalation — plus the T15 per-device pipeline model (spawn on
hot-plug, stop on unplug, independent ladders) — all with fake child
commands and a fake device enumeration; no real dashboard or panel."""

import threading
import time
import unittest

from thermallcd.supervisor import Supervisor


def _sleep_cmd():
    return ["/bin/sh", "-c", "sleep 60"]


class SupervisorTest(unittest.TestCase):
    def setUp(self):
        self.devices = ["panel-a"]
        self.healthy = True
        self.sup = Supervisor(
            data_dir="/tmp/thermallcd-sup-test", port=8999,
            check_interval=0.1, start_timeout=0.5, stable_s=60,
            backoff=(0.05, 0.1, 0.2), health_fails_max=3,
            dashboard_cmd=_sleep_cmd(), capture_cmd=_sleep_cmd(),
            health=lambda: self.healthy,
            enumerate_devices=lambda: [{"id": d} for d in self.devices])
        self.thread = threading.Thread(target=self.sup.run, daemon=True)

    def tearDown(self):
        self.sup.stop()
        try:
            self.thread.join(timeout=5)
        except RuntimeError:
            return  # never started (pure unit tests) — nothing to stop
        self.assertFalse(self.thread.is_alive(), "supervisor did not stop")

    # -- pipeline shape -------------------------------------------------------

    def test_starts_dashboard_and_device_capture(self):
        self.thread.start()
        time.sleep(0.8)
        self.assertTrue(self.sup._alive("dashboard"))
        entry = self.sup.devices.get("panel-a")
        self.assertIsNotNone(entry)
        self.assertIsNotNone(entry["proc"])
        self.assertIsNone(entry["proc"].poll())
        self.assertLessEqual(self.sup.starts["dashboard"], 2)  # no churn

    def test_each_device_gets_its_own_pipeline(self):
        self.devices = ["panel-a", "panel-b"]
        self.thread.start()
        time.sleep(0.8)
        pa = self.sup.devices["panel-a"]["proc"]
        pb = self.sup.devices["panel-b"]["proc"]
        self.assertTrue(pa.poll() is None and pb.poll() is None)
        self.assertNotEqual(pa.pid, pb.pid)

    def test_capture_cmd_scopes_device_and_cdp_port(self):
        # no injected capture_cmd: the built command carries the device and
        # a fresh CDP port per pipeline (two chromiums cannot share one)
        self.sup._cmds["capture"] = None
        cmd_a = self.sup._capture_cmd("panel-a")
        cmd_b = self.sup._capture_cmd("panel-b")
        self.assertIn("--device", cmd_a)
        self.assertIn("panel-a", cmd_a)
        self.assertIn("device=panel-a", cmd_a[3])   # the dashboard URL
        # the CDP port differs per pipeline
        port = lambda cmd: cmd[cmd.index("--port") + 1]
        self.assertNotEqual(port(cmd_a), port(cmd_b))

    # -- hot-plug -------------------------------------------------------------

    def test_unplugged_device_stops_its_pipeline(self):
        self.devices = ["panel-a", "panel-b"]
        self.thread.start()
        time.sleep(0.8)
        pb = self.sup.devices["panel-b"]["proc"]
        self.devices = ["panel-a"]
        time.sleep(0.8)
        self.assertNotIn("panel-b", self.sup.devices)
        self.assertIsNotNone(pb.poll())          # its process is gone
        self.assertIsNone(self.sup.devices["panel-a"]["proc"].poll())

    def test_plugging_device_in_spawns_later(self):
        self.devices = []
        self.thread.start()
        time.sleep(0.6)
        self.assertEqual(self.sup.devices, {})
        self.devices = ["panel-b"]
        time.sleep(0.6)
        self.assertIn("panel-b", self.sup.devices)
        self.assertIsNone(self.sup.devices["panel-b"]["proc"].poll())

    def test_enumeration_error_keeps_current_pipelines(self):
        # a transient USB hiccup must not tear down working pipelines
        self.thread.start()
        time.sleep(0.6)
        pa = self.sup.devices["panel-a"]["proc"]

        def boom():
            raise RuntimeError("usb hiccup")
        self.sup._enumerate = boom
        time.sleep(0.6)
        self.assertIsNone(pa.poll())             # untouched through the errors
        self.sup._enumerate = lambda: [{"id": "panel-a"}]

    # -- watchdog -------------------------------------------------------------

    def test_capture_restarted_when_it_exits(self):
        self.sup._cmds["capture"] = ["/bin/sh", "-c", "exit 42"]
        self.thread.start()
        time.sleep(1.6)
        self.assertGreaterEqual(self.sup.starts["panel-a"], 3)
        self.assertTrue(self.sup._alive("dashboard"))

    def test_dashboard_health_failure_restarts_pipeline(self):
        self.thread.start()
        time.sleep(0.6)
        self.assertIsNone(self.sup.devices["panel-a"]["proc"].poll())
        d1 = self.sup.procs["dashboard"].pid
        c1 = self.sup.devices["panel-a"]["proc"].pid
        self.healthy = False
        time.sleep(1.0)             # 3 consecutive fails at 0.1s intervals
        self.healthy = True
        time.sleep(0.8)             # pipeline comes back up
        self.assertGreater(self.sup.starts["dashboard"], 1)
        self.assertGreater(self.sup.starts["panel-a"], 1)
        self.assertNotEqual(self.sup.procs["dashboard"].pid, d1)
        self.assertNotEqual(self.sup.devices["panel-a"]["proc"].pid, c1)
        self.assertIsNone(self.sup.devices["panel-a"]["proc"].poll())

    def test_stop_terminates_children(self):
        self.thread.start()
        time.sleep(0.6)
        pa = self.sup.devices["panel-a"]["proc"]
        self.sup.stop()
        self.thread.join(timeout=5)
        self.assertFalse(self.sup._alive("dashboard"))
        self.assertIsNotNone(pa.poll())   # _stop_capture nulls the entry slot

    def test_backoff_escalates_and_resets(self):
        sup = self.sup
        st = {"fails": 0, "up_since": time.time()}
        # fresh death each time: the ladder climbs
        for i, want in enumerate(sup.backoff):
            st["up_since"] = time.time()
            st["fails"] = i
            sup._note_death(st)
            self.assertAlmostEqual(sup._restart_delay(st), want)
        # a stable stretch resets the ladder
        st["up_since"] = time.time() - sup.stable_s - 1
        sup._note_death(st)
        self.assertAlmostEqual(sup._restart_delay(st), sup.backoff[0])


if __name__ == "__main__":
    unittest.main()
