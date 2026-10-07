# thermallcd

Monitoring display for the **Thermalright Trofeo Vision 9.16" LCD** panel
(USB `0416:5408`, Winbond "USBDISPLAY", 1920x462) on Linux.

Current layout — nvtop fills the left half, right half is a placeholder for
the upcoming system/CPU/GPU stats widgets:

```
[ nvtop: GPU utilization TUI, 960x462 ] [ right panel: placeholder ]
```

## How it works

- `thermallcd/lcd.py` — minimal driver. Protocol ("LY" wire, read from the
  vendor DLL `USBLCDNEW` 2.1.6; authoritative sources in
  `reference/trcc-linux/`, background in `reference/Open-Trofeo-LCD/PROTOCOL_FULL.md`):
  1. Handshake (cmd `0x02`): one 2048-byte bulk OUT packet -> 512-byte
     response (`resp[0]=3`, `resp[8]=1` on success; panel reports PM=65,
     SUB=3, width 1920). Drain the IN endpoint first — a stale reply from a
     previous crashed session otherwise poisons the handshake.
  2. Frames: baseline JPEG (quality 85, 4:2:0), sliced into **512-byte
     chunks** (16-byte header + 496 data; `[8]=0x01`, `[9..10]`=chunk count
     LE16, `[11..12]`=chunk index LE16), chunk count padded to a multiple
     of 4 with zero chunks, written as 4096-byte bulk writes with a final
     2048-byte write, then **one** 512-byte ACK per frame. JPEG must stay
     under ~450 KB. Measured throughput: ~44 FPS.
- `thermallcd/nvtop_pane.py` — runs `/usr/bin/nvtop` in a fixed-size pty,
  emulates it with `pyte`, renders the character grid with DejaVu Sans
  Mono. Includes a VT100-charset filter because ncurses draws lines via
  `ESC(0` and pyte doesn't map it.
- `thermallcd/monitor.py` — composes the canvas, re-encodes and streams it
  to the panel at ~6 FPS (vendor rate). Reconnects when the panel is
  unplugged/replugged.
- `deploy/` — udev rule (permissions + service hook on ANY USB port) and a
  systemd service.

## Install

```
pip install --user --break-system-packages pyusb   # PIL + pyte already present
deploy/install.sh                                  # udev rule + systemd service
```

Test the panel directly (no service needed):

```
python3 thermallcd/test_card.py          # loops a test card until Ctrl+C
```

## Files

| path | purpose |
|---|---|
| `thermallcd/lcd.py` | USB driver (find/init/send JPEG) |
| `thermallcd/nvtop_pane.py` | nvtop in pty -> pyte -> PIL bitmap |
| `thermallcd/monitor.py` | compositor + streaming loop (service entry) |
| `thermallcd/test_card.py` | static test card |
| `tools/` | artifact investigation: stream capture / reference-emulator diff / freeze-probe |
| `deploy/99-thermallcd.rules` | udev: `MODE=0666`, starts service on plug |
| `deploy/thermallcd.service.template` | systemd unit template (install.sh fills in user + repo path) |
| `reference/Open-Trofeo-LCD` | upstream project + capture notes (GPL-3.0) |
| `reference/trcc-linux/` | vendor-DLL protocol doc + LY adapter (authoritative) |

## Known issue: stale graph fragments (fixed by mitigation)

nvtop's graph occasionally left stale wave fragments that never cleared
(seen once in practice, 2026-10-07). Root cause: **ncurses' incremental
update optimizer**, not our pipeline. Verified by:

- `tools/reference_diff.py` — a minimal xterm-accurate reference emulator
  run against pyte on a 160 KB captured nvtop stream: **zero divergences**.
  pyte (and the VT100 charset filter) process every sequence — CUP/CHA/VPA,
  autowrap toggles at the right margin, backspace, ACS — exactly like a real
  terminal.
- `tools/freeze_probe.py` — SIGSTOP nvtop (data frozen), snapshot pyte,
  Ctrl+L full repaint, snapshot again: dozens of cells differ every round,
  in both directions (leftover old glyphs *and* missing new ones). With
  frozen data those diffs are pure divergence between what ncurses believes
  is on screen and what a terminal actually shows.

So ncurses' `curscr` bookkeeping drifts from the terminal state (its
optimizer skips cells it believes unchanged, likely around right-margin /
pending-wrap handling while nvtop draws the graph into the last column).
Most drift self-heals within a few ticks; it becomes visible only when
stale glyphs sit in an otherwise quiet graph.

Mitigation: `NvtopPane.force_repaint()` sends form feed (`\x0c`), which
makes nvtop do a full endwin/reinit repaint; the monitor does this every
`REPAINT_INTERVAL` (60 s), bounding any artifact's lifetime to under a
minute. The repaint is invisible in practice (the clear and redraw land in
the same burst).

## Status / next steps

- [x] panel addressed (handshake + JPEG frames verified live)
- [x] nvtop on left 50%
- [x] service installed, enabled, starts on plug into any USB port (2026-10-06)
- [ ] right half: system / CPU / GPU stats widgets

### Next session: right-half widgets

The right half is a placeholder — replace `build_right_panel()` in
`thermallcd/monitor.py` (area: x 960..1920, 960x462 px). Widget ideas:
CPU load/per-core, RAM/swap, GPU busy + VRAM + temps (amdgpu:
`/sys/class/drm/card*/gpu_busy_percent`, `mem_info_vram_used`),
network, disk, top LLM process. Data sources: /proc, /sys — no nvidia-smi
on this box. Keep the composed frame under ~450 KB JPEG (firmware limit).

Service management: `sudo systemctl restart thermallcd.service`,
logs: `journalctl -u thermallcd.service -f`.
