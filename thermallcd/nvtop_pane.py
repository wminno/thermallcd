#!/usr/bin/env python3
"""Run nvtop inside a pty and render its terminal grid to a PIL image.

nvtop is a curses TUI, so we give it a fixed-size virtual terminal,
emulate it with pyte, then draw the resulting character grid with a
monospace font. This lets any TUI appear as pixels on the LCD.
"""

import fcntl
import io
import os
import pty
import select
import signal
import struct
import termios
import threading

import pyte
from PIL import Image, ImageDraw, ImageFont

FONT_REGULAR = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"
FONT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf"

# ANSI palette for pyte's 8/16 basic colors
ANSI = {
    "black": (40, 40, 46), "red": (222, 95, 95), "green": (98, 196, 98),
    "yellow": (235, 203, 88), "blue": (94, 148, 255), "magenta": (200, 121, 220),
    "cyan": (84, 200, 200), "white": (220, 220, 220),
    "bright_black": (110, 110, 120), "bright_red": (255, 120, 120),
    "bright_green": (130, 230, 130), "bright_yellow": (255, 230, 120),
    "bright_blue": (130, 170, 255), "bright_magenta": (230, 150, 255),
    "bright_cyan": (120, 230, 230), "bright_white": (245, 245, 245),
}
FG_DEFAULT = (208, 212, 220)
BG_DEFAULT = (10, 12, 16)

# ncurses draws lines via the VT100 alternate charset (ESC(0 ... ESC(B);
# pyte does not translate it, so we map the glyphs ourselves.
VT100_MAP = {
    0x6a: "┘", 0x6b: "┐", 0x6c: "┌", 0x6d: "└", 0x6e: "┼", 0x71: "─",
    0x74: "├", 0x75: "┤", 0x76: "┴", 0x77: "┬", 0x78: "│", 0x61: "▒",
    0x60: "◆", 0x66: "°", 0x67: "±", 0x6f: "⎺", 0x70: "⎯", 0x73: "⎽",
    0x7e: "·", 0x30: "▒",
}

NORMAL, ESC, SCS, CSI, OSC, OSC_ESC = range(6)


class CharsetFilter:
    """Stateful filter: rewrite ESC(0 regions to Unicode line-drawing."""

    def __init__(self):
        self.state = NORMAL
        self.in_graph = False

    def feed(self, data: bytes) -> bytes:
        out = bytearray()
        for byte in data:
            st = self.state
            if st == NORMAL:
                if byte == 0x1B:
                    self.state = ESC
                elif self.in_graph and byte in VT100_MAP:
                    out += VT100_MAP[byte].encode()
                else:
                    out.append(byte)
            elif st == ESC:
                if byte == ord("["):
                    out += b"\x1b["
                    self.state = CSI
                elif byte == ord("("):
                    self.state = SCS
                elif byte == ord("]"):
                    self.state = OSC
                elif byte == 0x1B:
                    pass  # ESC ESC - stay in ESC
                else:
                    self.state = NORMAL
                    out += b"\x1b" + bytes([byte])
            elif st == SCS:
                if byte == ord("0"):
                    self.in_graph = True
                elif byte == ord("B"):
                    self.in_graph = False
                self.state = NORMAL
            elif st == CSI:
                out.append(byte)  # control sequences pass through unmapped
                if 0x40 <= byte <= 0x7E:
                    self.state = NORMAL
            elif st == OSC:
                if byte == 0x07:
                    self.state = NORMAL
                elif byte == 0x1B:
                    self.state = OSC_ESC
            elif st == OSC_ESC:
                self.state = NORMAL  # consumed ESC \ terminator
        return bytes(out)


def _color(value, default):
    if value in (None, "default"):
        return default
    if value in ANSI:
        return ANSI[value]
    if isinstance(value, str) and value.startswith("#") and len(value) == 7:
        try:
            return tuple(int(value[i:i + 2], 16) for i in (1, 3, 5))
        except ValueError:
            return default
    return default


class NvtopPane:
    """Owns the nvtop child process + pyte screen; renders thread-safe."""

    def __init__(self, cols, rows, px=14, binary="/usr/bin/nvtop"):
        self.cols, self.rows, self.px = cols, rows, px
        self.binary = binary
        self.font_reg = ImageFont.truetype(FONT_REGULAR, px)
        self.font_bold = ImageFont.truetype(FONT_BOLD, px)
        bbox = self.font_reg.getbbox("M")
        self.cw = bbox[2] - bbox[0]
        self.ch = px

        self.screen = pyte.Screen(cols, rows)
        self.stream = pyte.ByteStream(self.screen)
        self.filter = CharsetFilter()
        self._lock = threading.Lock()

        self.master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))
        self.pid = os.fork()
        if self.pid == 0:
            os.setsid()
            fcntl.ioctl(slave, termios.TIOCSCTTY, 0)
            os.dup2(slave, 0); os.dup2(slave, 1); os.dup2(slave, 2)
            os.close(self.master); os.close(slave)
            env = os.environ.copy()
            env["TERM"] = "xterm-256color"
            env["LANG"] = "C.utf8"
            os.execve(binary, [binary], env)
            os._exit(127)
        os.close(slave)
        os.set_blocking(self.master, False)
        threading.Thread(target=self._pump, daemon=True).start()

    def _pump(self):
        while True:
            r, _, _ = select.select([self.master], [], [], 0.5)
            if not r:
                continue
            try:
                data = os.read(self.master, 65536)
            except (BlockingIOError, OSError):
                return  # child died / pty closed
            if not data:
                return
            with self._lock:
                self.stream.feed(self.filter.feed(data))

    def stop(self):
        try:
            os.kill(self.pid, signal.SIGKILL)
            os.waitpid(self.pid, 0)
        except (ProcessLookupError, ChildProcessError):
            pass
        try:
            os.close(self.master)
        except OSError:
            pass

    def force_repaint(self):
        """Ask nvtop for a full screen repaint (form feed -> ncurses
        endwin/reinit). Works around ncurses' incremental-update optimizer
        occasionally skipping graph cells, which leaves stale wave fragments
        on screen until the region is redrawn."""
        if not self._pane_dead():
            try:
                os.write(self.master, b"\x0c")
            except OSError:
                pass

    def _pane_dead(self) -> bool:
        try:
            os.kill(self.pid, 0)
            return False
        except ProcessLookupError:
            return True

    def _snapshot(self):
        with self._lock:
            return list(self.screen.display), self.screen.buffer

    def render(self, width, height) -> Image.Image:
        """Draw the terminal grid onto a width x height image."""
        lines, buf = self._snapshot()
        img = Image.new("RGB", (width, height), BG_DEFAULT)
        d = ImageDraw.Draw(img)
        cw, ch = self.cw, self.ch
        for y in range(min(self.rows, height // ch)):
            line = lines[y]
            row = buf[y]
            for x in range(min(self.cols, width // cw)):
                char = row[x]
                bg = _color(char.bg, BG_DEFAULT)
                if bg != BG_DEFAULT:
                    d.rectangle([x * cw, y * ch, (x + 1) * cw - 1, (y + 1) * ch - 1], fill=bg)
                if char.data and char.data != " ":
                    fg = _color(char.fg, FG_DEFAULT)
                    font = self.font_bold if char.bold else self.font_reg
                    d.text((x * cw, y * ch), char.data, font=font, fill=fg)
        return img

    @classmethod
    def for_area(cls, width, height, binary="/usr/bin/nvtop", target_rows=23):
        """Grid sized to fill width x height; ~target_rows keeps nvtop's layout sane."""
        px = max(8, int(height / target_rows))
        probe = ImageFont.truetype(FONT_REGULAR, px)
        bbox = probe.getbbox("M")
        cols = width // (bbox[2] - bbox[0])
        rows = height // px
        return cls(cols, rows, px=px, binary=binary)


class NvtopSource:
    """Server-side image source for the nvtop widget (T4/D19).

    Owns one NvtopPane sized to the widget's layout slot; a slot-size change
    recreates the pane, which re-renders nvtop at the new terminal size
    (resize: free). A 60 s force_repaint loop bounds ncurses artifact
    accumulation (R4). The pane is created lazily on first request and
    dropped when the layout has no nvtop slot.
    """

    CELL_W, CELL_H, GAP = 150, 138, 8  # dashboard grid geometry (D33)

    def __init__(self, layout, binary="/usr/bin/nvtop"):
        self.layout = layout          # LayoutStore — re-read per request
        self.binary = binary
        self._pane = None
        self._box = None              # (wpx, hpx) the pane was built for
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._repaints = threading.Thread(target=self._repaint_loop, daemon=True)
        self._repaints.start()

    @classmethod
    def pixels(cls, w, h):
        """Slot size in device pixels (cells 150x138, 8px gutter, D33)."""
        return (w * cls.CELL_W + (w - 1) * cls.GAP,
                h * cls.CELL_H + (h - 1) * cls.GAP)

    def _slot_size(self):
        """(w, h) of the first nvtop slot in the canonical layout, or None."""
        for item in self.layout.load("default")["widgets"]:
            if item["id"] == "nvtop":
                return item["w"], item["h"]
        return None

    def _repaint_loop(self):
        while not self._stop.wait(60):
            with self._lock:
                if self._pane:
                    self._pane.force_repaint()

    def jpeg(self, wpx=None, hpx=None):
        """JPEG of the pane; None when no slot/binary.

        (wpx, hpx) is the widget's measured content box — its slot minus the
        title strip and body padding — so the TUI renders 1:1 under the widget
        chrome. Rendering at the raw slot made the img overflow its iframe and
        clip the TUI's bottom rows. Requests without w/h (legacy tabs) are
        served at the pane's current size and never resize it; the pane never
        exceeds the slot either way."""
        slot = self._slot_size()
        if slot is None or not os.path.isfile(self.binary):
            self._drop()
            return None
        slot_px = self.pixels(*slot)
        with self._lock:
            if wpx is None or hpx is None:
                box = None    # legacy requester (no w/h) — see below
            else:
                try:
                    box = (max(60, min(int(wpx), slot_px[0])),
                           max(40, min(int(hpx), slot_px[1])))
                except (TypeError, ValueError):
                    box = None
            # only chrome-aware requests (w/h present) may move the pane.
            # Legacy polls — a tab running pre-contract widget JS, another
            # viewer's preview — are served at the pane's current size instead:
            # letting them resize too let two viewers with different boxes tug
            # the pty size back and forth every poll, which restarts nvtop
            # faster than it can draw and blanks the panel's widget.
            if box is not None and (box != self._box or self._pane is None):
                if self._pane:
                    self._pane.stop()   # recreate: resize = new pty size (D19)
                self._pane = NvtopPane.for_area(*box, binary=self.binary)
                self._box = box
            elif self._pane is None:
                # first requester is legacy: build at the raw slot (old shape)
                self._pane = NvtopPane.for_area(*slot_px, binary=self.binary)
                self._box = slot_px
            img = self._pane.render(*self._box)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=85)
        return buf.getvalue()

    def _drop(self):
        with self._lock:
            if self._pane:
                self._pane.stop()
            self._pane, self._box = None, None

    def stop(self):
        self._stop.set()
        self._drop()
