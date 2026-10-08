"""Capture spike: resident headless Chromium + CDP Page.startScreencast -> lcd.py.

Plan step 2 (docs/designs/dashboard-platform.md). One Chromium instance per
panel; the browser owns the frame clock, Python is transport:

  1. spawn chromium --headless=new --remote-debugging-port=PORT --window-size=1920,462
  2. open the dashboard URL (spike: a file:// test page)
  3. CDP Page.startScreencast (JPEG); frames arrive on repaint, ack each one
  4. throttle to ~6 FPS (firmware rate), re-encode via lcd.encode_jpeg
     (guarantees baseline 4:2:0 + the <450 KB frame contract), send via lcd.py

Run: python3 -m thermallcd.capture <url> [--port 9222] [--fps 6] [--seconds N]

This is the spike: no watchdog (T2), no device enumeration (T15), no auth.
Ctrl+C stops Chromium and releases the panel.
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

import websocket  # websocket-client

from . import lcd

CDP_PORT_DEFAULT = 9222
FPS_DEFAULT = 6.0
STALE_FRAME_S = 30.0  # spike: warn only (watchdog lands with T2/D8)

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
                 height: int = lcd.HEIGHT):
        self.url = url
        self.port = port
        self.width = width
        self.height = height
        self.proc = None

    def start(self):
        binpath = find_chromium()
        self.proc = subprocess.Popen(
            [
                binpath,
                "--headless=new",
                f"--remote-debugging-port={self.port}",
                f"--window-size={self.width},{self.height}",
                "--disable-gpu",
                # newer chromium rejects CDP websockets without an allow-list;
                # the port binds 127.0.0.1, so this stays local
                "--remote-allow-origins=*",
                "--no-first-run",
                "--no-default-browser-check",
                "--hide-scrollbars",
                # snap chromium (strict confinement) can only write under $HOME
                "--user-data-dir=" + os.path.expanduser(
                    "~/.cache/thermallcd-capture-profile"
                ),
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

    def __init__(self, ws_url: str, on_frame, fps: float = FPS_DEFAULT):
        self.ws_url = ws_url
        self.on_frame = on_frame
        self.min_interval = 1.0 / fps
        self.stale_s = STALE_FRAME_S
        self._last_send = 0.0
        self._last_frame = time.time()
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

    def run(self):
        ws = websocket.create_connection(self.ws_url, timeout=5, enable_multithread=True)
        ws.send(json.dumps({"id": 1, "method": "Page.enable"}))
        self._msg_id = self._start(ws, self._msg_id)
        self._last_frame = time.time()
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
                    now = time.time()
                    if now - self._last_send >= self.min_interval:
                        self._last_send = now
                        self.sent += 1
                        jpeg = base64.b64decode(params.get("data", ""))
                        self.on_frame(jpeg)
                    else:
                        self.dropped += 1
                self._stale_check()
        finally:
            try:
                ws.close()
            except Exception:
                pass

    def _stale_check(self, force_reset: bool = False) -> bool:
        """True exactly when the stale threshold fires (once per window)."""
        if force_reset:
            self._last_frame = time.time()
            return False
        if time.time() - self._last_frame > self.stale_s:
            print(f"capture: no frame for >{self.stale_s:.0f}s — re-arming "
                  "screencast", file=sys.stderr)
            self._last_frame = time.time()
            return True
        return False

    def stop(self):
        self._stop.set()


def to_panel_frame(jpeg: bytes, max_bytes: int = lcd.MAX_FRAME_BYTES) -> bytes:
    """Re-encode a screencast JPEG into the firmware contract.

    lcd.encode_jpeg guarantees baseline 4:2:0 at 1920x462; quality walks down
    until the frame fits under max_bytes.
    """
    from PIL import Image

    image = Image.open(io.BytesIO(jpeg))
    for quality in (85, 80, 75, 70, 60, 50, 40, 30):
        out = lcd.encode_jpeg(image, quality=quality)
        if len(out) < max_bytes:
            return out
    raise lcd.TrofeoError("frame cannot fit under the firmware size cap")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("url", help="dashboard URL (file:// works for the spike)")
    ap.add_argument("--port", type=int, default=CDP_PORT_DEFAULT, help="CDP port")
    ap.add_argument("--fps", type=float, default=FPS_DEFAULT)
    ap.add_argument("--seconds", type=float, default=0, help="stop after N s (0 = run)")
    ap.add_argument("--dry-run", action="store_true", help="skip the panel (no USB)")
    args = ap.parse_args(argv)

    panel = lcd.TrofeoLCD(debug=True)
    chromium = Chromium(args.url, port=args.port)
    frames_out = {"n": 0}

    def on_frame(jpeg: bytes):
        frame = to_panel_frame(jpeg)
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
    print(f"capture: chromium up on :{args.port} -> {args.url}")
    try:
        if not args.dry_run:
            panel.open()
            info = panel.init()
            print(f"capture: panel opened (pm={info['pm']} sub={info['sub']})")
        cast = Screencast(chromium.page_ws_url(), on_frame, fps=args.fps)
        thread = threading.Thread(target=cast.run, daemon=True)
        thread.start()

        sig = {"stop": False}
        def _sig(*_):
            sig["stop"] = True
        signal.signal(signal.SIGINT, _sig)

        t0 = time.time()
        last = 0
        while not sig["stop"] and tick():
            n = frames_out["n"]
            if n and n % (args.fps * 10) == 0 and n != last:
                dt = time.time() - t0
                print(f"capture: {n} frames to panel in {dt:.0f}s "
                      f"({n / dt:.1f} FPS, {cast.dropped} dropped)")
                last = n
            time.sleep(0.25)
    finally:
        if cast:
            cast.stop()
        chromium.stop()
        if not args.dry_run:
            panel.close()
        print(f"capture: done — {frames_out['n']} frames sent")


if __name__ == "__main__":
    main()
