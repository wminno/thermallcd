"""Per-device layout store: canonical layouts + burn-in shuffle state.

Files live under data/layouts/: <device>.json is the canonical arrangement the
editor edits; <device>.shuffle.json carries the current shuffle permutation
(D28) — shuffle writes never overwrite the canonical file. All writes are
atomic (tempfile + os.replace). A corrupt canonical file is moved aside and
the default layout falls back (EC1) instead of a crash loop.
"""

import json
import os
import tempfile
import time
from pathlib import Path

# D18 default: hierarchy by size — tier-1 heroes left, nvtop as the wide
# center block, small meters stacked right, the wide LLM ticker along the
# bottom; the rest of row 3 stays empty so the scene shows through.
DEFAULT_LAYOUT = {
    "widgets": [
        {"id": "cpu-load", "x": 0, "y": 0, "w": 3, "h": 2},
        {"id": "gpu-load", "x": 3, "y": 0, "w": 3, "h": 2},
        {"id": "nvtop", "x": 6, "y": 0, "w": 4, "h": 2},
        {"id": "disk-usage", "x": 10, "y": 0, "w": 2, "h": 1},
        {"id": "load-avg", "x": 10, "y": 1, "w": 2, "h": 1},
        {"id": "llm-tasks", "x": 0, "y": 2, "w": 5, "h": 1},
    ]
}

GRID_COLS = 12
GRID_ROWS = 3


def _atomic_write(path: Path, payload: dict):
    body = json.dumps(payload, indent=2)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(body)
        os.replace(tmp_name, path)
    except OSError:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def _validate_layout(layout: dict) -> str | None:
    """Basic shape validation; returns an error string or None."""
    if not isinstance(layout, dict) or not isinstance(layout.get("widgets"), list):
        return "layout must be an object with a widgets array"
    for item in layout["widgets"]:
        if not isinstance(item, dict) or not isinstance(item.get("id"), str):
            return "each widget needs an id string"
        for axis in ("x", "y", "w", "h"):
            if not isinstance(item.get(axis), int):
                return f"{item.get('id')}: {axis} must be an integer"
        if item["w"] < 1 or item["h"] < 1:
            return f"{item['id']}: size must be >= 1"
        if not (0 <= item["x"] < GRID_COLS and 0 <= item["y"] < GRID_ROWS):
            return f"{item['id']}: origin outside the {GRID_COLS}x{GRID_ROWS} grid"
        if item["x"] + item["w"] > GRID_COLS or item["y"] + item["h"] > GRID_ROWS:
            return f"{item['id']}: extends past the {GRID_COLS}x{GRID_ROWS} grid"
    return None


class LayoutStore:
    def __init__(self, layouts_dir: Path):
        self.dir = Path(layouts_dir)
        self.dir.mkdir(parents=True, exist_ok=True)

    def _canonical(self, device: str) -> Path:
        return self.dir / f"{device}.json"

    def _shuffle(self, device: str) -> Path:
        return self.dir / f"{device}.shuffle.json"

    @staticmethod
    def _safe_name(device: str) -> str:
        return device.replace("/", "_").replace("\\", "_") or "default"

    def load(self, device: str) -> dict:
        """Canonical layout for a device; default on missing/corrupt file."""
        device = self._safe_name(device)
        path = self._canonical(device)
        if path.exists():
            try:
                layout = json.loads(path.read_text(encoding="utf-8"))
                if _validate_layout(layout) is None:
                    return layout
            except (json.JSONDecodeError, OSError):
                pass
            # corrupt: move aside, fall through to default (EC1 recovery)
            try:
                path.rename(self.dir / f"{device}.corrupt-{int(time.time())}.json")
            except OSError:
                pass
        return json.loads(json.dumps(DEFAULT_LAYOUT))  # deep copy

    def save(self, device: str, layout: dict, shuffled: bool = False) -> dict:
        """Write canonical or shuffle state. Returns the saved document."""
        device = self._safe_name(device)
        error = _validate_layout(layout)
        if error:
            raise ValueError(error)
        if shuffled:
            doc = {"widgets": layout["widgets"], "shuffled": True,
                   "shuffled_at": time.time()}
            _atomic_write(self._shuffle(device), doc)  # D28: never touches canonical
            return doc
        _atomic_write(self._canonical(device), layout)
        return layout

    def shuffle_state(self, device: str) -> dict | None:
        path = self._shuffle(self._safe_name(device))
        if not path.exists():
            return None
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
            if state.get("shuffled") and isinstance(state.get("widgets"), list):
                return state
        except (json.JSONDecodeError, OSError):
            pass
        return None

    def clear_shuffle(self, device: str):
        path = self._shuffle(self._safe_name(device))
        try:
            path.unlink()
        except FileNotFoundError:
            pass
