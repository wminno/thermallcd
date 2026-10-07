#!/usr/bin/env python3
"""Freeze-probe: find truly stale cells without live-data confounding.

1. run nvtop in a pty fed to pyte (same pipeline as the service)
2. after a settle period, SIGSTOP nvtop (data frozen), drain pty
3. snapshot pyte screen (pre)
4. SIGCONT + Ctrl+L -> nvtop does a full repaint of identical data
5. snapshot (post); any changed cell = a cell ncurses believed stale,
   i.e. our screen really was wrong there (or ncurses is)
"""

import fcntl
import os
import pty
import signal
import struct
import sys
import termios
import time

import pyte

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), os.pardir, "thermallcd"))
from nvtop_pane import CharsetFilter  # noqa: E402

COLS, ROWS = 80, 23


def main():
    rounds = int(sys.argv[1]) if len(sys.argv) > 1 else 4
    settle = float(sys.argv[2]) if len(sys.argv) > 2 else 45.0
    outdir = sys.argv[3] if len(sys.argv) > 3 else None

    cap = open(outdir + "/stream.bin", "wb") if outdir else None

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

    def pump(dur):
        end = time.time() + dur
        while time.time() < end:
            try:
                data = os.read(master, 65536)
            except (BlockingIOError, OSError):
                time.sleep(0.02)
                continue
            if data:
                stream.feed(filt.feed(data))

    print(f"settling {settle}s ...")
    pump(settle)

    total_stale = 0
    for r in range(rounds):
        # freeze
        os.kill(pid, signal.SIGSTOP)
        pump(0.5)
        pre = list(screen.display)
        # repaint with frozen data
        os.kill(pid, signal.SIGCONT)
        os.write(master, b"\x0c")
        pump(2.5)
        post = list(screen.display)
        diffs = [(y, x, pre[y][x], post[y][x])
                 for y in range(ROWS) for x in range(COLS)
                 if pre[y][x] != post[y][x]]
        print(f"\nround {r}: {len(diffs)} stale cells")
        for y, x, old, new in diffs[:40]:
            print(f"  row {y:2d} col {x:2d}: {old!r} -> {new!r}")
        total_stale += len(diffs)
        # leave it running between rounds
        pump(3.0)

    print(f"\ntotal stale cells across {rounds} rounds: {total_stale}")
    os.kill(pid, signal.SIGKILL)
    os.waitpid(pid, 0)


if __name__ == "__main__":
    main()
