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

# T13 flair: the background scene layer config, stored alongside the widgets.
# Synthwave is on from first boot (D29): a layout without a flair key renders
# DEFAULT_FLAIR; the editor saves the merged object from its first save on.
FLAIR_SCENES = {"sunset", "grid", "ridge", "city", "twin", "mesa"}
FLAIR_THEMES = {"synthwave", "vaporwave", "outrun", "miami", "cyber", "noir"}
FLAIR_MOTIONS = {"left", "right", "forward", "backward", "none"}
DEFAULT_FLAIR = {
    "scene": "sunset",
    "theme": "synthwave",
    "motion": "right",
    "glass": 0.82,
    "speed": 25,
    "spectrum": True,
    "stars": True,
    "road": False,
    "scanlines": True,
    "enabled": True,
}

# T14/D28 burn-in shuffle: minutes between widget permutations on the panel;
# 0 = off. Stored in the canonical doc (the editor sets it), consumed by the
# dashboard page's shuffle engine.
DEFAULT_SHUFFLE = {"interval_min": 0}


def _normalize_shuffle(shuffle):
    """Validated copy of the shuffle config, or an error string.

    Mirrors _normalize_flair: unknown keys dropped (forward-compatible),
    interval coerced to a non-negative int (a bad value should not 400 the
    whole layout save)."""
    if shuffle is None:
        return dict(DEFAULT_SHUFFLE), None
    if not isinstance(shuffle, dict):
        return None, "shuffle must be an object"
    out = dict(DEFAULT_SHUFFLE)
    for key, value in shuffle.items():
        if key == "interval_min":
            if (not isinstance(value, (int, float)) or isinstance(value, bool)
                    or value < 0):
                return None, "shuffle.interval_min must be a number >= 0"
            out[key] = int(value)
        # unknown keys: dropped silently
    return out, None


def _normalize_flair(flair):
    """Validated copy of the flair config, or an error string.

    Unknown keys are dropped (forward-compatible), glass is clamped to 0..1
    (a slider jitter should not 400), speed to 0..100, booleans are coerced."""
    if not isinstance(flair, dict):
        return None, "flair must be an object"
    out = dict(DEFAULT_FLAIR)
    for key, value in flair.items():
        if key in ("scene", "theme", "motion"):
            whitelist = {"scene": FLAIR_SCENES, "theme": FLAIR_THEMES,
                         "motion": FLAIR_MOTIONS}[key]
            if value not in whitelist:
                return None, f"flair.{key}: {value!r} is not one of {sorted(whitelist)}"
            out[key] = value
        elif key == "glass":
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                return None, "flair.glass must be a number"
            out[key] = round(max(0.0, min(1.0, float(value))), 3)
        elif key == "speed":
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                return None, "flair.speed must be a number"
            out[key] = round(max(0.0, min(100.0, float(value))), 1)
        elif key in ("spectrum", "stars", "road", "scanlines", "enabled"):
            out[key] = bool(value)
        # unknown keys: dropped silently
    return out, None


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
    if "flair" in layout and layout["flair"] is not None:
        _, error = _normalize_flair(layout["flair"])
        if error:
            return error
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
        if "options" in item:
            opts = item["options"]
            if not isinstance(opts, dict) or not all(
                    isinstance(k, str) and isinstance(v, str)
                    for k, v in opts.items()):
                return f"{item['id']}: options must be a dict of string values"
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
        """Canonical layout for a device; the shared default arrangement when
        the device has no file of its own (T15) or the file is corrupt."""
        device = self._safe_name(device)
        path = self._canonical(device)
        if device != "default" and not path.exists():
            # T15: a device without its own layout renders the shared default
            # (the single-panel install keeps working; the first editor save
            # forks it to layouts/<device>.json)
            device = "default"
            path = self._canonical(device)
        if path.exists():
            try:
                layout = json.loads(path.read_text(encoding="utf-8"))
                if _validate_layout(layout) is None:
                    if "shuffle" not in layout:
                        # pre-T14 docs: serve the off default so every consumer
                        # (editor settings, dashboard engine) sees the key
                        layout = {**layout, "shuffle": dict(DEFAULT_SHUFFLE)}
                    return layout
            except (json.JSONDecodeError, OSError):
                pass
            # corrupt: move aside, fall through to default (EC1 recovery)
            try:
                path.rename(self.dir / f"{device}.corrupt-{int(time.time())}.json")
            except OSError:
                pass
        out = json.loads(json.dumps(DEFAULT_LAYOUT))  # deep copy
        out["shuffle"] = dict(DEFAULT_SHUFFLE)
        return out

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
        # store the normalized flair (defaults filled, unknown keys dropped)
        # so the canonical file is always a complete document
        if layout.get("flair") is not None:
            flair, err = _normalize_flair(layout["flair"])
            if err:
                raise ValueError(err)
            layout = {**layout, "flair": flair}
        if "shuffle" in layout and layout["shuffle"] is not None:
            shuffle, err = _normalize_shuffle(layout["shuffle"])
            if err:
                raise ValueError(err)
            layout = {**layout, "shuffle": shuffle}
        _atomic_write(self._canonical(device), layout)
        # a canonical save may have moved widgets: the stored permutation is
        # now stale (wrong slots, possible overlaps) — drop it so the panel
        # reloads canonical and reshuffles from the new arrangement
        self.clear_shuffle(device)
        return layout

    def shuffle_state(self, device: str) -> dict | None:
        """Current permutation for a device, falling back to the shared
        default's state when the device renders the default layout (T15)."""
        device = self._safe_name(device)
        state = self._shuffle_state(device)
        if state is None and device != "default":
            state = self._shuffle_state("default")
        return state

    def _shuffle_state(self, device: str) -> dict | None:
        path = self._shuffle(device)
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
