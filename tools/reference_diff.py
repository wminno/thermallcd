#!/usr/bin/env python3
"""Diff pyte against a minimal xterm-accurate reference emulator on a capture.

Reference implements exactly the primitives nvtop uses:
CUP(H) CHA(G) VPA(d) SGR(m) ED(J) EL(K) DECSET/DECRST(private h/l, incl 7)
IRM(4l), ESC(0/ESC(B charset via the same CharsetFilter, CR, LF, and
printable characters with xterm's pending-wrap semantics.

Feeds the capture in small chunks to both, reports the first chunk where
their grids diverge, and the cells.
"""

import os
import sys

import pyte

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, "thermallcd"))
from nvtop_pane import CharsetFilter  # noqa: E402

COLS, ROWS = 80, 23


class Reference:
    """Tiny xterm-accurate terminal for nvtop's primitive set."""

    def __init__(self):
        self.grid = [[" "] * COLS for _ in range(ROWS)]
        self.x = 0
        self.y = 0
        self.pending = False  # wrap pending at last column
        self.autowrap = True
        self.buf = b""  # carry-over for sequences split across chunks

    # -- helpers -----------------------------------------------------------
    def _put(self, ch):
        if self.pending:
            self.pending = False
            self.x = 0
            self.y += 1
            if self.y >= ROWS:
                # scroll: drop line 0
                self.grid.pop(0)
                self.grid.append([" "] * COLS)
                self.y = ROWS - 1
        if self.x >= COLS:  # shouldn't happen
            self.x = COLS - 1
        self.grid[self.y][self.x] = ch
        if self.x == COLS - 1:
            if self.autowrap:
                self.pending = True  # deferred wrap
            # autowrap off: stay, overprint next time
        else:
            self.x += 1

    def feed(self, data: bytes):
        self.buf += data
        i = 0
        n = len(self.buf)
        while i < n:
            b = self.buf[i]
            if b == 0x1B:
                used = self._escape(self.buf[i:])
                if used < 0:
                    break  # incomplete sequence; keep for next chunk
                i += used
                continue
            if b == 0x0D:
                self.x = 0
                self.pending = False
                i += 1
                continue
            if b == 0x08:
                self.x = max(0, self.x - 1)
                self.pending = False
                i += 1
                continue
            if b == 0x0A:
                self.pending = False
                self.y += 1
                if self.y >= ROWS:
                    self.grid.pop(0)
                    self.grid.append([" "] * COLS)
                    self.y = ROWS - 1
                i += 1
                continue
            if b < 0x20:
                i += 1
                continue
            # UTF-8 decode
            if b < 0x80:
                self._put(chr(b))
                i += 1
                continue
            ln = 2 if b >= 0xC0 and b < 0xE0 else 3 if b < 0xF0 else 4
            if i + ln > n:
                break  # partial UTF-8; keep for next chunk
            try:
                ch = self.buf[i:i + ln].decode("utf-8")
            except UnicodeDecodeError:
                ch = "?"
            self._put(ch)
            i += ln
        self.buf = self.buf[i:]

    def _escape(self, data):
        """Handle ESC sequence at data[0]; return consumed length, or -1 if
        the sequence is incomplete (needs more bytes)."""
        if len(data) < 2:
            return -1
        if data[1:2] == b"(":
            if len(data) < 3:
                return -1
            return 3  # charset switch (filter already handled mapping)
        if data[1:2] == b"]":
            # OSC until BEL or ESC-\
            j = 2
            while j < len(data):
                if data[j] == 0x07:
                    return j + 1
                if data[j] == 0x1B and data[j + 1:j + 2] == b"\\":
                    return j + 2
                j += 1
            return -1  # unterminated; wait for more
        if data[1:2] != b"[":
            return 2  # ESC= ESC> etc.
        # CSI
        j = 2
        while j < len(data) and 0x20 <= data[j] <= 0x3f:
            j += 1
        if j >= len(data):
            return -1  # incomplete; wait for more
        if not (0x40 <= data[j] <= 0x7e):
            return 1  # malformed; drop ESC
        final = chr(data[j])
        params = data[2:j].decode("latin1")
        priv = params.startswith("?")
        nums = [int(x) if x else 0 for x in params.lstrip("?").split(";")] \
            if params.lstrip("?") else []
        self._csi(final, priv, nums)
        return j + 1

    def _csi(self, final, priv, nums):
        g1 = lambda k: nums[k] if len(nums) > k and nums[k] else 1
        if final == "H":
            self.y = min(max(0, g1(0) - 1), ROWS - 1)
            self.x = min(max(0, g1(1) - 1), COLS - 1)
            self.pending = False
        elif final == "G":
            self.x = min(max(0, g1(0) - 1), COLS - 1)
            self.pending = False
        elif final == "d":
            self.y = min(max(0, g1(0) - 1), ROWS - 1)
            self.pending = False
        elif final == "J":
            how = nums[0] if nums else 0
            if how == 2:
                for row in self.grid:
                    for xx in range(COLS):
                        row[xx] = " "
        elif final == "K":
            how = nums[0] if nums else 0
            if how == 0:
                rng = range(self.x, COLS)
            elif how == 1:
                rng = range(self.x + 1)
            else:
                rng = range(COLS)
            for xx in rng:
                self.grid[self.y][xx] = " "
        elif final in "hl" and priv:
            if 7 in nums:
                self.autowrap = final == "h"


def main():
    data = open(sys.argv[1] if len(sys.argv) > 1 else "/tmp/lcdprobe/stream.bin", "rb").read()
    ref = Reference()
    screen = pyte.Screen(COLS, ROWS)
    stream = pyte.ByteStream(screen)
    filt = CharsetFilter()

    CH = 64
    first_div = None
    div_count = 0
    for off in range(0, len(data), CH):
        chunk = data[off:off + CH]
        filtered = filt.feed(chunk)
        stream.feed(filtered)
        ref.feed(filtered)
        # compare
        d = screen.display
        diffs = []
        for y in range(ROWS):
            for x in range(COLS):
                if d[y][x] != ref.grid[y][x]:
                    diffs.append((y, x, d[y][x], ref.grid[y][x]))
        if diffs:
            div_count += 1
            if first_div is None:
                first_div = off
                print(f"FIRST divergence at byte offset {off}:")
                for y, x, p, r in diffs[:20]:
                    print(f"  row {y} col {x}: pyte={p!r} reference={r!r}")
                lo = max(0, off - 120)
                print("context:", repr(data[lo:off + CH]))
    print(f"\nchunks with divergence: {div_count}")
    print(f"first divergence offset: {first_div}")


if __name__ == "__main__":
    main()
