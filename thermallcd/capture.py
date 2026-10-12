"""Capture spike: resident headless Chromium + CDP Page.startScreencast -> lcd.py.

Plan step 2 (docs/designs/dashboard-platform.md). One Chromium instance per
panel; the browser owns the frame clock, Python is transport:

  1. spawn chromium --headless=new --remote-debugging-port=PORT --window-size=1920,462
  2. open the dashboard URL (spike: a file:// test page)
  3. CDP Page.startScreencast (JPEG); frames arrive on repaint, ack each one
  4. throttle to the target rate (15 FPS default — raised from 6 when the
     flair scene needed smooth motion; the vendor sources pin no cap and
     the panel kept up), re-encode via lcd.encode_jpeg
     (guarantees baseline 4:2:0 + the <450 KB frame contract), send via lcd.py

Run: python3 -m thermallcd.capture <url> [--device ID] [--port 9222] [--fps 6]
     [--seconds N]

Watchdog split (T2/D8): the capture self-exits with code 42 when the
screencast is unrecoverably stuck (no frames at all despite re-arms — a
broken tab context re-arming cannot fix, or a dead chromium); the
supervisor (thermallcd.supervisor) restarts it. T15: --device picks the
panel (id from lcd.enumerate_devices — one capture per panel, each
supervised independently); the handshake resolves the profile before
chromium is sized, and the profile is written to <data>/profiles/<id>.json
(its mtime is the heartbeat /api/devices reads). Ctrl+C stops Chromium
and releases the panel.
"""

import argparse
import base64
import io
import json
import os
import signal
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

import websocket  # websocket-client

from . import lcd

CDP_PORT_DEFAULT = 9222
FPS_DEFAULT = 15.0
STALE_FRAME_S = 30.0
# exit code the supervisor reads as "screencast stuck, restart me"
EXIT_STUCK = 42


class StuckScreencastError(RuntimeError):
    """No frames arrived despite repeated re-arms — restart the pipeline."""

CHROMIUM_BIN_CANDIDATES = (
    "chromium",
    "chromium-browser",
    "google-chrome",
    "/usr/bin/chromium",
)


def find_chromium() -> str:
    import shutil

    for name in CHROMIUM_BIN_CANDIDATES:
        path = shutil.which(name) or (name if name.startswith("/") and _is_exe(name) else None)
        if path:
            return path
    raise RuntimeError(
        "chromium not found — install it (sudo apt install chromium) "
        "or set CHROMIUM_BIN"
    )


def _is_exe(path: str) -> bool:
    import os

    return os.path.isfile(path) and os.access(path, os.X_OK)


class Chromium:
    """Resident headless chromium exposing one CDP endpoint."""

    def __init__(self, url: str, port: int = CDP_PORT_DEFAULT, width: int = lcd.WIDTH,
                 height: int = lcd.HEIGHT, user_data_dir: str | None = None):
        self.url = url
        self.port = port
        self.width = width
        self.height = height
        self.user_data_dir = user_data_dir or os.path.expanduser(
            "~/.cache/thermallcd-capture-profile")
        self.proc = None

    def start(self):
        binpath = find_chromium()
        self.proc = subprocess.Popen(
            [
                binpath,
                "--headless=new",
                f"--remote-debugging-port={self.port}",
                f"--window-size={self.width},{self.height}",
                # GPU compositing: headless picks SwiftShader (software) by
                # default even without --disable-gpu; ANGLE Vulkan hits the
                # real GPU (RADV), so the flair canvas + per-widget backdrop
                # blur composite at speed. If a driver update makes the
                # screencast unstable, dropping these is the fallback.
                "--use-angle=vulkan",
                # newer chromium rejects CDP websockets without an allow-list;
                # the port binds 127.0.0.1, so this stays local
                "--remote-allow-origins=*",
                "--no-first-run",
                "--no-default-browser-check",
                "--hide-scrollbars",
                # snap chromium (strict confinement) can only write under $HOME;
                # per-panel dirs (T15) — two headless instances sharing one
                # profile dir would fight over the SingletonLock
                "--user-data-dir=" + self.user_data_dir,
                self.url,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        deadline = time.time() + 15
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{self.port}/json/version", timeout=1
                ) as r:
                    json.loads(r.read().decode())
                    return
            except Exception:
                time.sleep(0.2)
        raise RuntimeError(f"chromium CDP endpoint did not come up on port {self.port}")

    def page_ws_url(self) -> str:
        """webSocketDebuggerUrl of the (first) page target."""
        with urllib.request.urlopen(
            f"http://127.0.0.1:{self.port}/json/list", timeout=5
        ) as r:
            targets = json.loads(r.read().decode())
        for t in targets:
            if t.get("type") == "page" and t.get("webSocketDebuggerUrl"):
                return t["webSocketDebuggerUrl"]
        raise RuntimeError("no page target found on the CDP endpoint")

    def stop(self):
        proc, self.proc = self.proc, None
        if proc:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()


class Screencast:
    """CDP screencast -> callback(jpeg_bytes), throttled, acked."""

    def __init__(self, ws_url: str, on_frame, fps: float = FPS_DEFAULT,
                 width: int = lcd.WIDTH, height: int = lcd.HEIGHT):
        self.ws_url = ws_url
        self.on_frame = on_frame
        self.min_interval = 1.0 / fps
        self.width = width
        self.height = height
        self.stale_s = STALE_FRAME_S
        self.max_stale_rearms = 3  # 3 x stale_s with zero frames = stuck
        self._stale_streak = 0
        self._last_frame = time.time()
        self._latest = None          # newest un-sent frame (jpeg bytes)
        self._latest_lock = threading.Lock()
        self._msg_id = 10
        self._stop = threading.Event()
        self.dropped = 0
        self.sent = 0

    def _start(self, ws, msg_id: int) -> int:
        """(Re-)arm the screencast; chromium restarts the session each call."""
        ws.send(json.dumps({
            "id": msg_id,
            "method": "Page.startScreencast",
            "params": {"format": "jpeg", "quality": 85, "everyFrame": False},
        }))
        return msg_id + 1

    def _send_loop(self):
        """Steady send clock: ship the newest frame every min_interval.

        The screencast produces on page damage in bursts; gating arrivals
        directly sent ~5 of 15 produced FPS (an arrival right after a send
        was dropped, then the next clump member was stale). Holding the
        newest frame and sending on a fixed clock keeps the panel cadence
        even at the full target rate. The tick grid is absolute — waiting
        min_interval after each encode let the ~20 ms encode cost stretch
        the period to ~85 ms (11.5 FPS) and jitter it per frame."""
        while not self._stop.is_set():
            next_tick = time.time() + self.min_interval
            with self._latest_lock:
                jpeg, self._latest = self._latest, None
            if jpeg is not None:
                self.sent += 1
                self.on_frame(jpeg)
            delay = next_tick - time.time()
            if delay > 0:
                self._stop.wait(delay)

    def run(self):
        ws = websocket.create_connection(self.ws_url, timeout=5, enable_multithread=True)
        ws.send(json.dumps({"id": 1, "method": "Page.enable"}))
        # Pin the viewport to the panel geometry. headless=new windows carry
        # real browser UI — an omnibox popup was seen eating 87px off the
        # viewport — so --window-size alone does not guarantee the page lays
        # out at panel size, and to_panel_frame stretches whatever it gets to
        # the firmware contract: a short viewport means a soft, compressed
        # panel image. Emulation pins the layout viewport to exactly WxH.
        ws.send(json.dumps({
            "id": 2, "method": "Emulation.setDeviceMetricsOverride",
            "params": {"width": self.width, "height": self.height,
                       "deviceScaleFactor": 1, "mobile": False},
        }))
        self._msg_id = self._start(ws, self._msg_id)
        self._last_frame = time.time()
        sender = threading.Thread(target=self._send_loop, daemon=True)
        sender.start()
        try:
            while not self._stop.is_set():
                try:
                    raw = ws.recv()
                except websocket.WebSocketTimeoutException:
                    if self._stale_check():
                        # session died (or never attached): re-arm instead of
                        # warning forever — the panel stays black otherwise
                        self._msg_id = self._start(ws, self._msg_id)
                    continue
                if not raw:
                    break
                msg = json.loads(raw)
                if msg.get("method") == "Page.screencastFrame":
                    self._stale_check(force_reset=True)
                    params = msg.get("params", {})
                    session = params.get("sessionId")
                    # always ack so chromium keeps producing frames
                    self._msg_id += 1
                    ws.send(json.dumps({
                        "id": self._msg_id,
                        "method": "Page.screencastFrameAck",
                        "params": {"sessionId": session},
                    }))
                    jpeg = base64.b64decode(params.get("data", ""))
                    with self._latest_lock:
                        if self._latest is not None:
                            self.dropped += 1   # superseded before it was sent
                        self._latest = jpeg
                self._stale_check()
        finally:
            try:
                ws.close()
            except Exception:
                pass

    def _stale_check(self, force_reset: bool = False) -> bool:
        """True exactly when the stale threshold fires (once per window).

        Every firing counts toward the stuck streak: a healthy session
        produces at least the re-arm's initial frame, so consecutive
        firings with zero frames mean the tab context is gone — raising
        beats re-arming forever while the panel starves."""
        if force_reset:
            self._last_frame = time.time()
            self._stale_streak = 0
            return False
        if time.time() - self._last_frame > self.stale_s:
            self._stale_streak += 1
            if self._stale_streak >= self.max_stale_rearms:
                raise StuckScreencastError(
                    f"no frames for >{self.max_stale_rearms * self.stale_s:.0f}s "
                    "despite re-arms — screencast session is unrecoverable")
            print(f"capture: no frame for >{self.stale_s:.0f}s — re-arming "
                  "screencast", file=sys.stderr)
            self._last_frame = time.time()
            return True
        return False

    def stop(self):
        self._stop.set()


def to_panel_frame(jpeg: bytes, size=(lcd.WIDTH, lcd.HEIGHT),
                   max_bytes: int = lcd.MAX_FRAME_BYTES) -> bytes:
    """Re-encode a screencast JPEG into the firmware contract.

    lcd.encode_jpeg guarantees baseline 4:2:0 at the panel's profile (T15 —
    1920x462 on the 9.16"); quality walks down until the frame fits under
    max_bytes.
    """
    from PIL import Image

    image = Image.open(io.BytesIO(jpeg))
    for quality in (85, 80, 75, 70, 60, 50, 40, 30):
        out = lcd.encode_jpeg(image, quality=quality, size=size)
        if len(out) < max_bytes:
            return out
    raise lcd.TrofeoError("frame cannot fit under the firmware size cap")


def _write_profile(data_dir, device_id: str, device: dict, info: dict):
    """Panel profile state (T15): the dashboard and editor read these — the
    file's mtime doubles as the heartbeat /api/devices reports."""
    profiles = Path(data_dir) / "profiles"
    profiles.mkdir(parents=True, exist_ok=True)
    w, h = info["profile"]
    doc = {
        "id": device_id,
        "serial": device.get("serial"),
        "w": w, "h": h,
        "pm": info["pm"], "sub": info["sub"],
    }
    (profiles / f"{device_id}.json").write_text(
        json.dumps(doc), encoding="utf-8")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("url", help="dashboard URL (file:// works for the spike)")
    ap.add_argument("--device", default="default",
                    help="panel id from lcd.enumerate_devices (T15)")
    ap.add_argument("--port", type=int, default=CDP_PORT_DEFAULT, help="CDP port")
    ap.add_argument("--fps", type=float, default=FPS_DEFAULT)
    ap.add_argument("--seconds", type=float, default=0, help="stop after N s (0 = run)")
    ap.add_argument("--data", default=str(Path(__file__).parent.parent / "data"),
                    help="state dir (profiles/); gitignored")
    ap.add_argument("--dry-run", action="store_true", help="skip the panel (no USB)")
    args = ap.parse_args(argv)

    panel = lcd.TrofeoLCD(debug=True)
    frames_out = {"n": 0}
    size = (lcd.WIDTH, lcd.HEIGHT)

    device = None
    if not args.dry_run:
        # T15: open exactly the panel this pipeline owns. A missing device is
        # not a stuck screencast — exit 1 and let the supervisor's resync
        # (hot-plug poll) decide when to try again.
        device = next(
            (d for d in lcd.enumerate_devices() if d["id"] == args.device), None)
        if device is None:
            print(f"capture: device {args.device!r} not present", file=sys.stderr)
            sys.exit(1)
        panel.open(device)
        info = panel.init()
        print(f"capture: panel {args.device} opened "
              f"(pm={info['pm']} sub={info['sub']})")
        size = info["profile"]
        _write_profile(args.data, args.device, device, info)

    chromium = Chromium(args.url, port=args.port, width=size[0], height=size[1],
                        user_data_dir=os.path.expanduser(
                            "~/.cache/thermallcd-capture-" + args.device))
    profile_path = Path(args.data) / "profiles" / f"{args.device}.json"

    def on_frame(jpeg: bytes):
        frame = to_panel_frame(jpeg, size=size)
        if args.dry_run:
            frames_out["n"] += 1
            return
        panel.send_jpeg(frame)
        frames_out["n"] += 1

    cast = None
    stop_at = time.time() + args.seconds if args.seconds else None

    def tick():
        if stop_at and time.time() > stop_at:
            return False
        return True

    chromium.start()
    print(f"capture: chromium up on :{args.port} ({size[0]}x{size[1]}) -> {args.url}")
    try:
        cast = Screencast(chromium.page_ws_url(), on_frame, fps=args.fps,
                          width=chromium.width, height=chromium.height)
        thread = threading.Thread(target=cast.run, daemon=True)
        thread.start()

        sig = {"stop": False}
        exit_code = 0
        def _sig(*_):
            sig["stop"] = True
        signal.signal(signal.SIGINT, _sig)

        t0 = time.time()
        last = 0
        while not sig["stop"] and tick():
            if not thread.is_alive():
                # stuck screencast (raises in-thread) or dead chromium: the
                # supervisor restarts us — the panel must not starve silently
                print("capture: screencast ended — exiting for restart",
                      file=sys.stderr)
                exit_code = EXIT_STUCK
                break
            n = frames_out["n"]
            if n and n % (args.fps * 10) == 0 and n != last:
                dt = time.time() - t0
                print(f"capture: {n} frames to panel in {dt:.0f}s "
                      f"({n / dt:.1f} FPS, {cast.dropped} dropped)")
                last = n
                if not args.dry_run and profile_path.exists():
                    profile_path.utime(None)  # heartbeat for /api/devices
            time.sleep(0.25)
    finally:
        if cast:
            cast.stop()
        chromium.stop()
        if not args.dry_run:
            panel.close()
        print(f"capture: done — {frames_out['n']} frames sent")
    if exit_code:
        sys.exit(exit_code)


if __name__ == "__main__":
    main()
