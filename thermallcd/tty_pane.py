"""Server-side image source for the tty widget (T18).

One pty pane per tty placement, running an arbitrary command under
sh -c (pipes and arguments work). The command is per-instance config from
T17: the layout's tty item carries {"options": {"cmd": ...}}, and this
source derives everything from that item — a viewer sends nothing but its
token, and /api/tty accepts no command input (D19: viewers never nominate
the pane's size or command). The pane is recreated when the command or the
slot size changes, killing the old process group.
"""

import io
import threading

from thermallcd import layout as layout_mod
from thermallcd.nvtop_pane import NvtopPane, NvtopSource


class TtySource(NvtopSource):
    """NvtopSource machinery (slot-pixel box, 60 s repaint loop, drop on
    removal) keyed on the tty item's command. The tty widget mirrors the
    nvtop widget's fixed chrome, so the slot-minus-chrome box math carries
    over unchanged."""

    def __init__(self, layout):
        super().__init__(layout)
        self._cmd = None

    def _tty_item(self):
        """The tty item in the canonical layout, or None. T28 pages: first
        match across pages in document order wins (the server cannot know
        which page a cycling panel shows), so a tty with a different cmd per
        page serves the first page's command."""
        return layout_mod.find_item(self.layout.load("default"), "tty")

    def image(self):
        """PNG of the command's pty pane, background keyed transparent;
        None when the widget isn't placed or no command is set — the widget
        dims and says so, never fabricating a pane."""
        item = self._tty_item()
        if item is None:
            self._drop()
            return None
        cmd = str((item.get("options") or {}).get("cmd") or "").strip()
        if not cmd:
            self._drop()   # placed but unconfigured: nothing to spawn
            return None
        slot_px = self.pixels(item["w"], item["h"])
        box = (max(60, slot_px[0] - self.CHROME_W),
               max(40, slot_px[1] - self.CHROME_H))
        with self._lock:
            if box != self._box or cmd != self._cmd or self._pane is None:
                if self._pane:
                    self._pane.stop()   # command or size changed: fresh pty
                self._pane = NvtopPane.for_area(
                    *box, argv=["/bin/sh", "-c", cmd])
                self._box = box
                self._cmd = cmd
            img = self._pane.render(*self._box)
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()
