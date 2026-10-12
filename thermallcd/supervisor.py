"""thermallcd supervisor (T2/D8): keep the panel pipeline alive.

Runs the dashboard server and the panel capture as supervised children
and restarts them when they die or go unhealthy:

  - capture self-exits (42) when its screencast is unrecoverably stuck —
    the panel-starving state that re-arming cannot fix;
  - the supervisor gates the dashboard on /api/health (3 consecutive
    failures -> restart) and restarts the capture whenever the dashboard
    restarts: a page stranded mid-outage never recovers cleanly;
  - the capture runs in its own process group, so a dead capture takes
    its chromium with it (an orphan would squat the CDP port);
  - restarts escalate through a backoff ladder, reset once a child has
    stayed up for `stable_s`;
  - SIGTERM/SIGINT stops both children cleanly.

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
import urllib.request
from pathlib import Path

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
                 dashboard_cmd=None, capture_cmd=None, health=None):
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
        url = f"http://127.0.0.1:{port}/?token={TokenStore(self.data_dir).token}"
        self._cmds = {
            "dashboard": dashboard_cmd or [
                sys.executable, "-m", "thermallcd.dashboard",
                "--bind", bind, "--port", str(port), "--data",
                str(self.data_dir)],
            "capture": capture_cmd or [
                sys.executable, "-m", "thermallcd.capture", url,
                "--port", str(cdp_port), "--fps", str(fps)],
        }
        self._health = health or self._http_health
        self.procs = {"dashboard": None, "capture": None}
        self.starts = {"dashboard": 0, "capture": 0}
        self._state = {name: {"fails": 0, "up_since": None}
                       for name in self.procs}
        self._health_fails = 0
        self._stop_event = threading.Event()

    # -- process lifecycle ---------------------------------------------------

    def run(self):
        self._install_signals()
        try:
            if not self._start_dashboard():
                return
            self._spawn("capture")
            while not self._stop_event.is_set():
                if self._stop_event.wait(self.check_interval):
                    break
                self._tick()
        finally:
            self._stop_proc("capture")
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
        # the capture gets its own process group: chromium is its child, and
        # the group dying together keeps an orphan from squatting the CDP port
        env = {**os.environ, "PYTHONUNBUFFERED": "1"}  # live stats, not 4 KB chunks
        proc = subprocess.Popen(self._cmds[name], env=env,
                                start_new_session=(name == "capture"))
        self.procs[name] = proc
        self._state[name]["up_since"] = time.time()
        self.starts[name] += 1
        print(f"supervisor: started {name} (pid {proc.pid})")
        return proc

    def _kill_group(self, proc, sig):
        # pgid == pid: the capture was started with start_new_session
        try:
            os.killpg(proc.pid, sig)
        except ProcessLookupError:
            pass  # group already gone

    def _stop_proc(self, name):
        proc, self.procs[name] = self.procs.get(name), None
        if not proc:
            return
        if proc.poll() is None:
            if name == "capture":
                self._kill_group(proc, signal.SIGTERM)
            else:
                proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                if name == "capture":
                    self._kill_group(proc, signal.SIGKILL)
                else:
                    proc.kill()
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    pass

    def _alive(self, name):
        proc = self.procs.get(name)
        return proc is not None and proc.poll() is None

    def _note_death(self, name):
        """Count one failed run; the ladder resets after a stable stretch."""
        st = self._state[name]
        if st["up_since"] and time.time() - st["up_since"] > self.stable_s:
            st["fails"] = 0
        st["fails"] += 1

    def _restart_delay(self, name):
        fails = max(1, self._state[name]["fails"])
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
                self._note_death("dashboard")
                print(f"supervisor: dashboard start failed — retry in "
                      f"{self._restart_delay('dashboard'):.0f}s")
                if self._stop_event.wait(self._restart_delay("dashboard")):
                    break
            self._spawn("dashboard")
            first = False
            if self._await_healthy():
                return True
            self._stop_proc("dashboard")
        return False

    def _restart_pipeline(self):
        # the capture follows the dashboard: its page never recovers a dead
        # server cleanly, so both start fresh together
        self._stop_proc("capture")
        self._stop_proc("dashboard")
        if self._start_dashboard():
            self._spawn("capture")

    def _tick(self):
        if not self._alive("dashboard"):
            self._note_death("dashboard")
            print("supervisor: dashboard died — restarting pipeline")
            self._restart_pipeline()
        elif not self._health():
            self._health_fails += 1
            print(f"supervisor: dashboard health fail "
                  f"{self._health_fails}/{self.health_fails_max}")
            if self._health_fails >= self.health_fails_max:
                self._health_fails = 0
                self._note_death("dashboard")
                print("supervisor: dashboard unhealthy — restarting pipeline")
                self._restart_pipeline()
        else:
            self._health_fails = 0
            if not self._alive("capture"):
                # reap the group first: a crashed capture may leave chromium
                # alive, and the orphan would hold the CDP port
                proc = self.procs.get("capture")
                if proc:
                    self._kill_group(proc, signal.SIGTERM)
                    try:
                        proc.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        pass
                self._note_death("capture")
                print(f"supervisor: capture exited — restart in "
                      f"{self._restart_delay('capture'):.0f}s")
                self.procs["capture"] = None
                if self._stop_event.wait(self._restart_delay("capture")):
                    return
                self._spawn("capture")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--bind", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8899,
                    help="dashboard port to supervise (default 8899)")
    ap.add_argument("--data", default=str(Path(__file__).parent.parent / "data"),
                    help="state dir (token); gitignored")
    ap.add_argument("--cdp-port", type=int, default=9222)
    ap.add_argument("--fps", type=float, default=FPS_DEFAULT)
    args = ap.parse_args(argv)
    Supervisor(args.data, port=args.port, bind=args.bind,
               cdp_port=args.cdp_port, fps=args.fps).run()


if __name__ == "__main__":
    main()
