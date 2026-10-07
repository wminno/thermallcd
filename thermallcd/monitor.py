#!/usr/bin/env python3
"""thermallcd monitor: nvtop on the left half of the LCD, info panel on the right.

Initial test layout (1920x462):
  [ nvtop (GPU utilization TUI), 50% width ] [ right-side panel (placeholder) ]

The panel image is re-encoded and streamed to the LCD continuously (~6 FPS),
like the vendor software does.
"""

import os
import signal
import socket
import sys
import time

from PIL import Image, ImageDraw, ImageFont

from lcd import HEIGHT, WIDTH, TrofeoError, TrofeoLCD, encode_jpeg
from nvtop_pane import BG_DEFAULT, NvtopPane

NVTOP_WIDTH = WIDTH // 2          # left half of the panel
RIGHT_WIDTH = WIDTH - NVTOP_WIDTH

FONT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
FONT_MONO = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"

TARGET_FPS = 6.0                  # vendor software streams ~6.3 FPS
PANE_REFRESH = 0.5                # seconds between nvtop screen captures
RECONNECT_DELAY = 3.0             # seconds between reconnect attempts
REPAINT_INTERVAL = 60.0           # force full nvtop repaint (clears stale
#                                   graph fragments ncurses forgot to update)

ACCENT = (70, 220, 160)
FG = (200, 205, 215)
DIM = (130, 138, 150)


def build_right_panel() -> Image.Image:
    """Placeholder right side; real stats widgets slot in here next."""
    img = Image.new("RGB", (RIGHT_WIDTH, HEIGHT), BG_DEFAULT)
    d = ImageDraw.Draw(img)
    big = ImageFont.truetype(FONT_BOLD, 52)
    med = ImageFont.truetype(FONT_BOLD, 30)
    mono = ImageFont.truetype(FONT_MONO, 26)

    d.text((40, 50), "thermallcd", font=big, fill=ACCENT)
    d.text((42, 130), socket.gethostname(), font=med, fill=FG)
    d.text((42, 175), time.strftime("%a %d %b  %H:%M:%S"), font=mono, fill=DIM)

    try:
        with open("/proc/uptime") as f:
            up = int(float(f.read().split()[0]))
        uptime = f"up {up // 86400}d {up % 86400 // 3600}h {up % 3600 // 60}m"
        d.text((42, 215), uptime, font=mono, fill=DIM)
    except OSError:
        pass

    d.line([(40, 270), (RIGHT_WIDTH - 40, 270)], fill=(60, 66, 76), width=2)
    d.text((42, 290), "system / cpu / gpu stats", font=med, fill=FG)
    d.text((42, 330), "widget goes here", font=mono, fill=DIM)
    return img


class Monitor:
    def __init__(self, nvtop_binary="/usr/bin/nvtop"):
        self.nvtop_binary = nvtop_binary
        self.lcd = None
        self.pane = None
        self.right = build_right_panel()
        self._running = True

    # -- nvtop pane lifecycle ----------------------------------------------

    def _pane_dead(self) -> bool:
        if self.pane is None:
            return True
        try:
            os.kill(self.pane.pid, 0)
            return False
        except ProcessLookupError:
            return True

    def _ensure_pane(self):
        if self._pane_dead():
            if self.pane is not None:
                self.pane.stop()
                print("nvtop exited; restarting in 5 s", file=sys.stderr)
                time.sleep(5)
            self.pane = NvtopPane.for_area(NVTOP_WIDTH, HEIGHT, binary=self.nvtop_binary)
            print(f"nvtop started: {self.pane.cols}x{self.pane.rows} cells "
                  f"({self.pane.cols * self.pane.cw}x{self.pane.rows * self.pane.ch}px)")

    # -- LCD lifecycle -------------------------------------------------------

    def _ensure_lcd(self):
        if self.lcd is not None:
            return
        self.lcd = TrofeoLCD()
        self.lcd.open()
        self.lcd.init()
        print("LCD connected and initialized")

    def _drop_lcd(self):
        if self.lcd is not None:
            self.lcd.close()
            self.lcd = None
            print("LCD lost; will retry", file=sys.stderr)

    # -- main loop -----------------------------------------------------------

    def run(self):
        pane_img = None
        last_capture = 0.0
        last_repaint = time.monotonic()
        frame_interval = 1.0 / TARGET_FPS
        while self._running:
            t_frame = time.monotonic()

            if t_frame - last_capture >= PANE_REFRESH or pane_img is None:
                try:
                    self._ensure_pane()
                    if t_frame - last_repaint >= REPAINT_INTERVAL:
                        self.pane.force_repaint()
                        last_repaint = t_frame
                    pane_img = self.pane.render(NVTOP_WIDTH, HEIGHT)
                except OSError as e:
                    print(f"nvtop spawn failed: {e}", file=sys.stderr)
                    pane_img = Image.new("RGB", (NVTOP_WIDTH, HEIGHT), BG_DEFAULT)
                last_capture = t_frame

            canvas = Image.new("RGB", (WIDTH, HEIGHT), BG_DEFAULT)
            canvas.paste(pane_img, (0, 0))
            canvas.paste(self.right, (NVTOP_WIDTH, 0))

            try:
                self._ensure_lcd()
                self.lcd.send_jpeg(encode_jpeg(canvas))
            except TrofeoError as e:
                print(f"LCD: {e}", file=sys.stderr)
                self._drop_lcd()
                if not self._sleep(RECONNECT_DELAY):
                    break
                continue
            except OSError as e:  # USB transfer failure on unplug
                print(f"USB error: {e}", file=sys.stderr)
                self._drop_lcd()
                if not self._sleep(RECONNECT_DELAY):
                    break
                continue

            self._sleep(max(0.0, frame_interval - (time.monotonic() - t_frame)))

    def _sleep(self, seconds) -> bool:
        """Interruptible sleep; False if shutting down."""
        end = time.monotonic() + seconds
        while self._running and time.monotonic() < end:
            time.sleep(min(0.1, end - time.monotonic()))
        return self._running

    def stop(self, *_):
        self._running = False


def main() -> int:
    mon = Monitor()
    signal.signal(signal.SIGTERM, mon.stop)
    signal.signal(signal.SIGINT, mon.stop)
    print("thermallcd monitor running (Ctrl+C to stop)")
    try:
        mon.run()
    finally:
        if mon.pane:
            mon.pane.stop()
        if mon.lcd:
            mon.lcd.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
