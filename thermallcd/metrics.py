"""Per-tick system metrics sampler (PF1).

One background thread samples /proc and /sys at a fixed interval and keeps a
single snapshot; /api/metrics serves whatever the last tick produced. The
stale contract (D23) shapes the payload: every value carries the snapshot
timestamp, `refresh` tells widgets the tick interval, and a value with no
source yet is `null` — never a fabricated zero.
"""

import glob
import json
import os
import shutil
import threading
import time
import urllib.request

# T26: what the llm-tasks widget samples. The llama-swap endpoint provides
# the loaded-model line; the process scan catches LLM workers started
# outside it (a bare llama-server, ollama). Env-overridable for other hosts.
LLM_SWAP_URL = os.environ.get(
    "THERMALLCD_LLAMA_SWAP_URL", "http://127.0.0.1:8080/v1/models")
LLM_PROCESS_NAMES = ("llama-server", "ollama", "ollama-runner")


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


def _parse_models(doc):
    """Loaded model ids from a llama-swap /v1/models payload (T26)."""
    out = []
    for m in (doc or {}).get("data", []):
        if not isinstance(m, dict):
            continue
        if isinstance(m.get("status"), dict) and m["status"].get("value") == "loaded":
            out.append(m.get("id") or "")
    return [m for m in out if m]


def _loaded_models(url=None):
    """Models currently loaded in llama-swap. Best-effort: a down swap or
    a bad payload yields no model line, never a fake one (D23)."""
    try:
        with urllib.request.urlopen(url or LLM_SWAP_URL, timeout=1.0) as resp:
            return _parse_models(json.loads(resp.read().decode("utf-8")))
    except Exception:  # any failure: the source is simply not there
        return []


def _llm_processes(root="/proc"):
    """Running LLM workers (T26): basename match on argv[0], biggest RSS
    first. CPU% is left out on purpose — the GPU pane already shows whether
    inference is active; RSS is the "it is sitting in VRAM" number."""
    found = []
    for path in glob.glob(os.path.join(root, "[0-9]*/cmdline")):
        try:
            with open(path, "rb") as fh:
                argv = [a for a in fh.read().decode("utf-8", "replace").split("\0") if a]
            if not argv:
                continue  # zombie: empty cmdline
            name = os.path.basename(argv[0])
            if name not in LLM_PROCESS_NAMES:
                continue
            fields = {}
            with open(os.path.join(os.path.dirname(path), "status")) as fh:
                for line in fh:
                    key, _, val = line.partition(":")
                    fields[key.strip()] = val.strip()
            rss_kb = int(fields.get("VmRSS", "0").split()[0])
            threads = int(fields.get("Threads", "0"))
            found.append((rss_kb, os.path.dirname(path).rsplit("/", 1)[-1],
                          name, threads))
        except (OSError, ValueError, IndexError):
            continue  # raced exit or unreadable entry
    found.sort(key=lambda row: -row[0])
    return [{"id": pid, "name": name,
             "status": f"{rss_kb / 2**20:.1f}G" + (f" · {threads} thr" if threads else "")}
            for rss_kb, pid, name, threads in found]


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
        # T26: loaded models first, then LLM workers by RSS; the widget
        # renders name · status per entry
        data["llm_tasks"] = [{"id": m, "name": m, "status": "loaded"}
                             for m in _loaded_models()] + _llm_processes()
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
