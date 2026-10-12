"""thermallcd supervisor (T2/D8): keep the panel pipeline alive.

Runs the dashboard server and the panel captures as supervised children
and restarts them when they die or go unhealthy:

  - one capture pipeline per connected panel (T15): devices are enumerated
    from USB each tick, so plugging a panel in spawns its pipeline and
    unplugging stops it — each supervised independently;
  - a capture self-exits (42) when its screencast is unrecoverably stuck —
    the panel-starving state that re-arming cannot fix;
  - the supervisor gates the dashboard on /api/health (3 consecutive
    failures -> restart) and restarts the captures whenever the dashboard
    restarts: a page stranded mid-outage never recovers cleanly;
  - each capture runs in its own process group, so a dead capture takes
    its chromium with it (an orphan would squat the CDP port);
  - restarts escalate through a backoff ladder, reset once a child has
    stayed up for `stable_s`; capture restarts are SCHEDULED (a due time
    per device) so one dead panel never blocks the others' tick;
  - SIGTERM/SIGINT stops everything cleanly.

Run: python3 -m thermallcd.supervisor [--port 8899] [--data DIR]
systemd is the outer watchdog: the unit's Restart=always covers this
process itself dying.
"""

import argparse
import os
import signal
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

from . import lcd
from .capture import FPS_DEFAULT
from .dashboard import TokenStore

BACKOFF_S = (2.0, 5.0, 10.0, 20.0, 30.0)
STABLE_S = 120.0
CHECK_INTERVAL_S = 5.0
START_TIMEOUT_S = 15.0
HEALTH_FAILS_MAX = 3


class Supervisor:
    def __init__(self, data_dir, port=8899, bind="0.0.0.0", fps=15.0,
                 cdp_port=9222, check_interval=CHECK_INTERVAL_S,
                 start_timeout=START_TIMEOUT_S, stable_s=STABLE_S,
                 backoff=BACKOFF_S, health_fails_max=HEALTH_FAILS_MAX,
                 dashboard_cmd=None, capture_cmd=None, health=None,
                 enumerate_devices=None):
        self.data_dir = Path(data_dir)
        self.port = port
        self.bind = bind
        self.fps = fps
        self.cdp_port = cdp_port
        self.check_interval = check_interval
        self.start_timeout = start_timeout
        self.stable_s = stable_s
        self.backoff = tuple(backoff)
        self.health_fails_max = health_fails_max
        self._cmds = {
            "dashboard": dashboard_cmd or [
                sys.executable, "-m", "thermallcd.dashboard",
                "--bind", bind, "--port", str(port), "--data",
                str(self.data_dir)],
            # per-device capture commands are BUILT in _capture_cmd; an
            # injected capture_cmd (tests) applies to every device verbatim
            "capture": capture_cmd,
        }
        self._health = health or self._http_health
        self._enumerate = enumerate_devices or lcd.enumerate_devices
        self.procs = {"dashboard": None}
        self.starts = {"dashboard": 0}
        self._state = {"dashboard": {"fails": 0, "up_since": None}}
        self.devices: dict = {}   # id -> {proc, state, restart_at}
        self._cdp_next = cdp_port
        self._health_fails = 0
        self._stop_event = threading.Event()

    # -- process lifecycle ---------------------------------------------------

    def run(self):
        self._install_signals()
        try:
            if not self._start_dashboard():
                return
            self._sync_devices()
            while not self._stop_event.is_set():
                if self._stop_event.wait(self.check_interval):
                    break
                self._tick()
        finally:
            for dev_id in list(self.devices):
                self._stop_capture(dev_id)
            self._stop_proc("dashboard")
            print("supervisor: stopped")

    def stop(self):
        self._stop_event.set()

    def _install_signals(self):
        def _sig(*_):
            self._stop_event.set()
        try:
            signal.signal(signal.SIGINT, _sig)
            signal.signal(signal.SIGTERM, _sig)
        except ValueError:
            pass  # not the main thread (tests) — stop() drives shutdown

    def _spawn(self, name):
        env = {**os.environ, "PYTHONUNBUFFERED": "1"}  # live stats, not 4 KB chunks
        proc = subprocess.Popen(self._cmds[name], env=env)
        self.procs[name] = proc
        self._state[name]["up_since"] = time.time()
        self.starts[name] += 1
        print(f"supervisor: started {name} (pid {proc.pid})")
        return proc

    def _capture_cmd(self, dev_id: str):
        if self._cmds["capture"]:
            return self._cmds["capture"]
        url = (f"http://127.0.0.1:{self.port}/?"
               f"token={TokenStore(self.data_dir).token}"
               f"&device={urllib.parse.quote(dev_id)}")
        return [sys.executable, "-m", "thermallcd.capture", url,
                "--device", dev_id,
                "--port", str(self._next_cdp_port()),
                "--fps", str(self.fps), "--data", str(self.data_dir)]

    def _next_cdp_port(self) -> int:
        port = self._cdp_next
        self._cdp_next += 1
        return port

    def _kill_group(self, proc, sig):
        # pgid == pid: the capture was started with start_new_session
        try:
            os.killpg(proc.pid, sig)
        except ProcessLookupError:
            pass  # group already gone

    def _spawn_capture(self, dev_id: str):
        # the capture gets its own process group: chromium is its child, and
        # the group dying together keeps an orphan from squatting a CDP port
        env = {**os.environ, "PYTHONUNBUFFERED": "1"}
        proc = subprocess.Popen(self._capture_cmd(dev_id), env=env,
                                start_new_session=True)
        entry = self.devices.setdefault(
            dev_id, {"proc": None, "state": {"fails": 0, "up_since": None},
                     "restart_at": None})
        entry["proc"] = proc
        entry["state"]["up_since"] = time.time()
        entry["restart_at"] = None
        self.starts[dev_id] = self.starts.get(dev_id, 0) + 1
        print(f"supervisor: started capture {dev_id} (pid {proc.pid})")
        return proc

    def _stop_capture(self, dev_id: str):
        entry = self.devices.get(dev_id)
        if not entry or entry.get("proc") is None:
            return
        proc = entry["proc"]
        entry["proc"] = None
        if proc.poll() is None:
            self._kill_group(proc, signal.SIGTERM)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._kill_group(proc, signal.SIGKILL)
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    pass

    def _stop_proc(self, name):
        proc, self.procs[name] = self.procs.get(name), None
        if not proc:
            return
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    pass

    def _alive(self, name):
        proc = self.procs.get(name)
        return proc is not None and proc.poll() is None

    def _note_death(self, st):
        """Count one failed run; the ladder resets after a stable stretch."""
        if st.get("up_since") and time.time() - st["up_since"] > self.stable_s:
            st["fails"] = 0
        st["fails"] = st.get("fails", 0) + 1

    def _restart_delay(self, st) -> float:
        fails = max(1, st["fails"])
        return self.backoff[min(fails - 1, len(self.backoff) - 1)]

    # -- health --------------------------------------------------------------

    def _http_health(self):
        try:
            with urllib.request.urlopen(
                    f"http://127.0.0.1:{self.port}/api/health", timeout=2) as r:
                return r.status == 200
        except Exception:
            return False

    def _await_healthy(self):
        deadline = time.time() + self.start_timeout
        while time.time() < deadline:
            if self._stop_event.is_set():
                return False
            if self._health():
                return True
            time.sleep(0.25)
        return False

    # -- supervision ---------------------------------------------------------

    def _start_dashboard(self):
        """Spawn + health-gate the dashboard; retry with backoff until it
        comes up (a slow boot or a taken port must not kill the watchdog)."""
        first = True
        while not self._stop_event.is_set():
            if not first:
                st = self._state["dashboard"]
                self._note_death(st)
                print(f"supervisor: dashboard start failed — retry in "
                      f"{self._restart_delay(st):.0f}s")
                if self._stop_event.wait(self._restart_delay(st)):
                    break
            self._spawn("dashboard")
            first = False
            if self._await_healthy():
                return True
            self._stop_proc("dashboard")
        return False

    def _sync_devices(self):
        """One capture per connected panel (T15): enumerate, spawn new
        panels' pipelines, stop vanished ones, restart dead ones on their
        backoff schedule. An enumeration error keeps the current set — a
        transient USB hiccup must not tear down working pipelines."""
        try:
            ids = [d["id"] for d in self._enumerate()]
        except Exception as e:
            print(f"supervisor: device enumeration failed ({e}) — keeping "
                  f"{len(self.devices)} pipeline(s)")
            ids = None
        if ids is not None:
            for dev_id in [d for d in self.devices if d not in ids]:
                print(f"supervisor: device {dev_id} gone — stopping its capture")
                self._stop_capture(dev_id)
                del self.devices[dev_id]
            for dev_id in ids:
                if dev_id not in self.devices:
                    self.devices[dev_id] = {
                        "proc": None, "state": {"fails": 0, "up_since": None},
                        "restart_at": None}
                    self._spawn_capture(dev_id)
        # dead captures of still-present devices: schedule the restart
        # (non-blocking — one dead panel must not stall the others' tick)
        now = time.time()
        for dev_id, entry in list(self.devices.items()):
            proc = entry.get("proc")
            if proc is not None and proc.poll() is None:
                continue
            st = entry["state"]
            if entry.get("restart_at") is None:
                # reap the group first: a crashed capture may leave chromium
                # alive, and the orphan would hold the CDP port
                if proc is not None:
                    self._kill_group(proc, signal.SIGTERM)
                    try:
                        proc.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        self._kill_group(proc, signal.SIGKILL)
                self._note_death(st)
                delay = self._restart_delay(st)
                entry["restart_at"] = now + delay
                print(f"supervisor: capture {dev_id} exited — restart in "
                      f"{delay:.0f}s")
            elif entry["restart_at"] <= now:
                entry["restart_at"] = None
                self._spawn_capture(dev_id)

    def _restart_pipeline(self):
        # the captures follow the dashboard: their pages never recover a dead
        # server cleanly, so everything starts fresh together
        for dev_id in list(self.devices):
            self._stop_capture(dev_id)
            self.devices[dev_id]["restart_at"] = None
            self.devices[dev_id]["state"]["up_since"] = None
        self._stop_proc("dashboard")
        if self._start_dashboard():
            self._sync_devices()

    def _tick(self):
        if not self._alive("dashboard"):
            self._note_death(self._state["dashboard"])
            print("supervisor: dashboard died — restarting pipeline")
            self._restart_pipeline()
        elif not self._health():
            self._health_fails += 1
            print(f"supervisor: dashboard health fail "
                  f"{self._health_fails}/{self.health_fails_max}")
            if self._health_fails >= self.health_fails_max:
                self._health_fails = 0
                self._note_death(self._state["dashboard"])
                print("supervisor: dashboard unhealthy — restarting pipeline")
                self._restart_pipeline()
        else:
            self._health_fails = 0
            self._sync_devices()


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--bind", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8899,
                    help="dashboard port to supervise (default 8899)")
    ap.add_argument("--data", default=str(Path(__file__).parent.parent / "data"),
                    help="state dir (token, layouts, profiles); gitignored")
    ap.add_argument("--cdp-port", type=int, default=9222)
    ap.add_argument("--fps", type=float, default=FPS_DEFAULT)
    args = ap.parse_args(argv)
    Supervisor(args.data, port=args.port, bind=args.bind,
               cdp_port=args.cdp_port, fps=args.fps).run()


if __name__ == "__main__":
    main()
