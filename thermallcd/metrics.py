"""Per-tick system metrics sampler (PF1).

One background thread samples /proc and /sys at a fixed interval and keeps a
single snapshot; /api/metrics serves whatever the last tick produced. The
stale contract (D23) shapes the payload: every value carries the snapshot
timestamp, `refresh` tells widgets the tick interval, and a value with no
source yet is `null` — never a fabricated zero.
"""

import glob
import os
import shutil
import threading
import time


def _read_proc_stat():
    """cpu + per-core jiffies from /proc/stat."""
    cpus = {}
    with open("/proc/stat", "rb") as fh:
        for line in fh:
            if line.startswith(b"cpu") and line[3:4] != b" ":
                parts = line.split()
                fields = [int(x) for x in parts[1:8]]  # user nice system idle iowait irq softirq
                idle = fields[3] + fields[4]
                total = sum(fields)
                cpus[parts[0].decode()] = (idle, total)
    return cpus


def _read_meminfo():
    info = {}
    with open("/proc/meminfo", "rb") as fh:
        for line in fh:
            key, rest = line.split(b":", 1)
            info[key.decode()] = int(rest.split()[0])  # kB
    return info


def _gpu_paths():
    """Best-effort amdgpu sysfs paths (card with gpu_busy_percent)."""
    for card in sorted(glob.glob("/sys/class/drm/card*/device")):
        if os.path.isfile(os.path.join(card, "gpu_busy_percent")):
            return card
    return None


def _gpu_sample(card):
    out = {"busy": None, "vram_used": None, "vram_total": None, "temp": None}
    if not card:
        return out
    try:
        with open(os.path.join(card, "gpu_busy_percent")) as fh:
            out["busy"] = int(fh.read().strip())
    except (OSError, ValueError):
        pass
    for key, fname in (("vram_used", "mem_info_vram_used"),
                       ("vram_total", "mem_info_vram_total")):
        try:
            with open(os.path.join(card, fname)) as fh:
                out[key] = int(fh.read().strip())  # bytes
        except (OSError, ValueError):
            pass
    for hwmon in sorted(glob.glob(os.path.join(card, "hwmon", "hwmon*"))):
        try:
            with open(os.path.join(hwmon, "temp1_input")) as fh:
                out["temp"] = int(fh.read().strip()) / 1000.0
                break
        except (OSError, ValueError):
            continue
    return out


class MetricsSampler:
    def __init__(self, interval: float = 1.0):
        self.interval = interval
        self.refresh = interval
        self._snap = {"ts": None, "refresh": interval, "data": {}}
        self._lock = threading.Lock()
        self._prev_cpu = None
        self._thread = None
        self._stop = threading.Event()
        self._gpu_card = None

    def snapshot(self) -> dict:
        with self._lock:
            return dict(self._snap)

    def _sample_once(self) -> dict:
        data = {"ts": time.time()}
        try:
            cpu = _read_proc_stat()
            if self._prev_cpu:
                per_core = []
                overall = None
                idle_d = cpu["cpu0"][0] - self._prev_cpu["cpu0"][0]
                total_d = cpu["cpu0"][1] - self._prev_cpu["cpu0"][1]
                if total_d > 0:
                    overall = round(100.0 * (1 - idle_d / total_d), 1)
                data["cpu"] = overall
                for key in sorted(cpu):
                    if key == "cpu0":
                        continue
                    idle_d = cpu[key][0] - self._prev_cpu[key][0]
                    total_d = cpu[key][1] - self._prev_cpu[key][1]
                    per_core.append(
                        round(100.0 * (1 - idle_d / total_d), 1) if total_d > 0 else None)
                data["cpu_cores"] = per_core
            self._prev_cpu = cpu
        except OSError:
            pass
        try:
            mem = _read_meminfo()
            total = mem.get("MemTotal")
            avail = mem.get("MemAvailable")
            if total and avail is not None:
                data["mem"] = {"used_pct": round(100.0 * (total - avail) / total, 1),
                               "total_kb": total, "avail_kb": avail,
                               "used_kb": total - avail}
                swap_total = mem.get("SwapTotal")  # swap for the memory widget (T25)
                if swap_total is not None:
                    used = swap_total - mem.get("SwapFree", 0)
                    data["mem"]["swap"] = {
                        "total_kb": swap_total, "used_kb": used,
                        "used_pct": round(100.0 * used / swap_total, 1) if swap_total else None}
        except OSError:
            pass
        try:
            usage = shutil.disk_usage("/")
            data["disk"] = {"used_pct": round(100.0 * usage.used / usage.total, 1),
                            "total_gb": round(usage.total / 2**30, 1),
                            "free_gb": round(usage.free / 2**30, 1)}
        except OSError:
            pass
        try:
            l1, l5, l15 = os.getloadavg()
            data["load"] = [round(l1, 2), round(l5, 2), round(l15, 2)]
        except OSError:
            pass
        data["gpu"] = _gpu_sample(self._gpu_card)
        data["llm_tasks"] = []  # no source wired yet (widget lands with T5+)
        return data

    def _run(self):
        while not self._stop.is_set():
            try:
                sample = self._sample_once()
                with self._lock:
                    self._snap = {"ts": sample["ts"], "refresh": self.refresh,
                                  "data": {k: v for k, v in sample.items() if k != "ts"}}
            except Exception:  # a bad tick never kills the sampler
                pass
            self._stop.wait(self.interval)

    def start(self):
        self._stop = threading.Event()
        self._gpu_card = _gpu_paths()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        if self._stop:
            self._stop.set()
