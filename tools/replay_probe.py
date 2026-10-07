#!/usr/bin/env python3
"""Artifact probe: record nvtop's raw byte stream while feeding pyte live.

After N seconds it sends Ctrl+L (full ncurses repaint), diffs pyte's screen
before vs after, and prints every cell ncurses considered stale — i.e. every
cell where our pyte state diverged from what a real terminal would show.

Usage: python3 replay_probe.py [seconds] [outdir]
"""

import os
import pty
import fcntl
import signal
import struct
import sys
import termios
import time

import pyte

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, "thermallcd"))
from nvtop_pane import CharsetFilter  # noqa: E402

COLS, ROWS = 80, 23


def cellgrid(screen):
    return list(screen.display)


def main():
    seconds = float(sys.argv[1]) if len(sys.argv) > 1 else 120.0
    outdir = sys.argv[2] if len(sys.argv) > 2 else "/tmp/lcdprobe"
    os.makedirs(outdir, exist_ok=True)

    screen = pyte.Screen(COLS, ROWS)
    stream = pyte.ByteStream(screen)
    filt = CharsetFilter()

    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", ROWS, COLS, 0, 0))
    pid = os.fork()
    if pid == 0:
        os.setsid()
        fcntl.ioctl(slave, termios.TIOCSCTTY, 0)
        os.dup2(slave, 0); os.dup2(slave, 1); os.dup2(slave, 2)
        os.close(master); os.close(slave)
        env = os.environ.copy()
        env["TERM"] = "xterm-256color"
        env["LANG"] = "C.utf8"
        os.execve("/usr/bin/nvtop", ["nvtop"], env)
        os._exit(127)
    os.close(slave)
    fcntl.fcntl(master, fcntl.F_SETFL, os.O_NONBLOCK)

    cap = open(os.path.join(outdir, "stream.bin"), "wb")
    events = open(os.path.join(outdir, "events.log"), "w")

    def pump_all(drain_seconds=0.0):
        """Read everything currently pending; returns bytes seen."""
        got = bytearray()
        deadline = time.time() + drain_seconds
        while True:
            try:
                data = os.read(master, 65536)
            except BlockingIOError:
                if time.time() >= deadline:
                    break
                time.sleep(0.02)
                continue
            except OSError:
                break
            if not data:
                break
            got += data
            cap.write(data)
            stream.feed(filt.feed(data))
        return bytes(got)

    events.write(f"start {time.time()}\n")
    start = time.time()
    pre_grid = None
    divergence_log = []

    while time.time() - start < seconds:
        pump_all(0.2)

    # quiesce, snapshot, force full repaint, snapshot again
    pump_all(1.0)
    pre_grid = cellgrid(screen)
    events.write(f"pre-repaint {time.time()} grid_rows={len(pre_grid)}\n")

    def force_repaint():
        """Trigger a full ncurses repaint; returns burst size (0 = no trigger)."""
        mark = cap.tell()
        os.write(master, b"\x0c")  # try Ctrl+L first
        got = pump_all(1.5)
        if len(got) > 1500:
            events.write(f"ctrlL repaint burst: {len(got)} bytes\n")
            return
        # fall back to same-size SIGWINCH (nvtop redraws on KEY_RESIZE)
        os.kill(pid, signal.SIGWINCH)
        got = pump_all(1.5)
        events.write(f"SIGWINCH burst: {len(got)} bytes\n")

    force_repaint()
    post_grid = cellgrid(screen)
    events.write(f"post-repaint {time.time()}\n")

    diffs = []
    for y, (a, b) in enumerate(zip(pre_grid, post_grid)):
        for x in range(min(len(a), len(b))):
            if a[x] != b[x]:
                diffs.append((y, x, a[x], b[x]))
    print(f"divergent cells after full repaint: {len(diffs)}")
    for y, x, old, new in diffs:
        print(f"  row {y:2d} col {x:2d}: pyte had {old!r} -> repaint wrote {new!r}")
    divergence_log.append(diffs)

    # second repaint check: should be a fixed point now
    mark = cap.tell()
    os.write(master, b"\x0c")
    got = pump_all(2.0)
    print(f"second repaint burst: {len(got)} bytes")
    post2 = cellgrid(screen)
    d2 = [(y, x, post_grid[y][x], post2[y][x])
          for y in range(len(post_grid)) for x in range(COLS)
          if post_grid[y][x] != post2[y][x]]
    print(f"cells changed by SECOND repaint: {len(d2)} (should be ~0 apart from live values)")
    for y, x, old, new in d2:
        print(f"  row {y:2d} col {x:2d}: {old!r} -> {new!r}")

    with open(os.path.join(outdir, "final_screen.txt"), "w") as f:
        for row in post2:
            f.write(row.rstrip() + "\n")

    os.kill(pid, signal.SIGKILL)
    os.waitpid(pid, 0)
    cap.close()
    events.close()
    print(f"capture: {os.path.getsize(os.path.join(outdir, 'stream.bin'))} bytes in {outdir}")


if __name__ == "__main__":
    main()
