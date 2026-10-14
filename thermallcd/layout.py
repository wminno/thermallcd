"""Per-device layout store: canonical arrangements the editor edits.

Files live under data/layouts/: <device>.json is the canonical arrangement.
All writes are atomic (tempfile + os.replace). A corrupt canonical file is
moved aside and the default layout falls back (EC1) instead of a crash loop.

The T14/D28 burn-in shuffle (client-side widget permutation with a persisted
state file) was removed 2026-10-14 — hunting displaced widgets annoyed the
user, and T28 widget pages on a timer supersedes it with user-definable
layouts. Stale <device>.shuffle.json files are dead data; nothing reads them.
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
    # T27 scene rotation (anti-burn-in): minutes between random switches,
    # 0 = off; what the switch randomizes. Client-side only — the pick is
    # never written back, so the saved config stays the user's default.
    "rotate_min": 0,
    "rotate_what": "scene",
}
# T27: what the rotation timer may randomize ("all" = scene+theme+motion)
FLAIR_ROTATE_WHAT = {"scene", "theme", "motion", "all"}


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
        elif key == "rotate_min":
            if not isinstance(value, (int, float)) or isinstance(value, bool) \
                    or value < 0:
                return None, "flair.rotate_min must be a number >= 0"
            out[key] = int(value)
        elif key == "rotate_what":
            if value not in FLAIR_ROTATE_WHAT:
                return None, f"flair.rotate_what: {value!r} is not one of {sorted(FLAIR_ROTATE_WHAT)}"
            out[key] = value
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


def _validate_widget_items(widgets: list) -> str | None:
    """Shape validation shared by flat docs and each page of a pages doc."""
    for item in widgets:
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
        # null options are the shape the panel render itself produces for
        # optionless widgets (T29): treat them like an absent key
        opts = item.get("options")
        if opts is not None and (not isinstance(opts, dict) or not all(
                isinstance(k, str) and isinstance(v, str)
                for k, v in opts.items())):
            return f"{item['id']}: options must be a dict of string values"
    return None


# T28 widget pages: the canonical doc is {cycle_min, pages: [{name, widgets}],
# ...} and cycles between the pages on a timer. Flat docs ({widgets}) stay
# valid so pre-pages files keep loading; save() normalizes everything to the
# pages shape, like the removed shuffle key's dead-key convergence.
DEFAULT_PAGE_NAME = "Main"
MAX_PAGE_NAME = 40


def _validate_layout(layout: dict) -> str | None:
    """Basic shape validation; returns an error string or None.

    Accepts both the flat shape and the pages shape so pre-pages files keep
    loading. A doc carrying both keys validates as pages; save() drops the
    dead widgets key."""
    if not isinstance(layout, dict):
        return "layout must be an object with a widgets array"
    cycle_min = layout.get("cycle_min")
    if cycle_min is not None:
        if not isinstance(cycle_min, (int, float)) or isinstance(cycle_min, bool) \
                or cycle_min < 0:
            return "cycle_min must be a number >= 0"
    pages = layout.get("pages")
    if pages is not None or "cycle_min" in layout:
        if not isinstance(pages, list) or not pages:
            return "pages must be a non-empty array"
        seen = set()
        for page in pages:
            if not isinstance(page, dict):
                return "each page must be an object"
            name = page.get("name")
            if not isinstance(name, str) or not name.strip():
                return "each page needs a name string"
            if len(name) > MAX_PAGE_NAME:
                return f"page name {name!r} is longer than {MAX_PAGE_NAME} characters"
            if name in seen:
                return f"page name {name!r} is used twice"
            seen.add(name)
            widgets = page.get("widgets")
            if not isinstance(widgets, list):
                return f"page {name!r} needs a widgets array"
            error = _validate_widget_items(widgets)
            if error:
                return f"page {name!r}: {error}"
    else:
        # flat shape: the pre-T28 document (DEFAULT_LAYOUT, existing files)
        if not isinstance(layout.get("widgets"), list):
            return "layout must be an object with a widgets array"
        error = _validate_widget_items(layout["widgets"])
        if error:
            return error
    if "flair" in layout and layout["flair"] is not None:
        _, error = _normalize_flair(layout["flair"])
        if error:
            return error
    return None


def pages_of(layout: dict) -> list[dict]:
    """A layout document's pages: pages-shape docs pass through, flat docs
    wrap as one page. The single dual-shape helper — the render bakes page
    0's frames, the pane scanners match across pages in document order."""
    pages = layout.get("pages") if isinstance(layout, dict) else None
    if isinstance(pages, list) and pages:
        return pages
    widgets = layout.get("widgets") if isinstance(layout, dict) else None
    return [{"name": DEFAULT_PAGE_NAME,
             "widgets": widgets if isinstance(widgets, list) else []}]


def find_item(layout: dict, item_id: str) -> dict | None:
    """First widget item with this id across all pages, in document order.
    Image-source panes key on it: the server cannot know which page a
    cycling panel is showing, so the first placement wins."""
    for page in pages_of(layout):
        for item in page["widgets"]:
            if item.get("id") == item_id:
                return item
    return None


class LayoutStore:
    def __init__(self, layouts_dir: Path):
        self.dir = Path(layouts_dir)
        self.dir.mkdir(parents=True, exist_ok=True)

    def _canonical(self, device: str) -> Path:
        return self.dir / f"{device}.json"

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
                    return layout
            except (json.JSONDecodeError, OSError):
                pass
            # corrupt: move aside, fall through to default (EC1 recovery)
            try:
                path.rename(self.dir / f"{device}.corrupt-{int(time.time())}.json")
            except OSError:
                pass
        return json.loads(json.dumps(DEFAULT_LAYOUT))  # deep copy

    def save(self, device: str, layout: dict) -> dict:
        """Write the canonical arrangement. Returns the saved document.

        Everything normalizes to the T28 pages shape: flat input becomes one
        page named DEFAULT_PAGE_NAME, cycle_min is always written, and the
        removed shuffle key is dropped — canonical files converge on the
        current shape, like the flair normalization before it."""
        device = self._safe_name(device)
        error = _validate_layout(layout)
        if error:
            raise ValueError(error)
        # store the normalized flair (defaults filled, unknown keys dropped)
        # so the canonical file is always a complete document
        if layout.get("flair") is not None:
            flair, err = _normalize_flair(layout["flair"])
            if err:
                raise ValueError(err)
            layout = {**layout, "flair": flair}
        pages = layout.get("pages")
        if isinstance(pages, list):
            norm_pages = [{"name": page["name"],
                           "widgets": [dict(item) for item in page["widgets"]]}
                          for page in pages]
        else:
            norm_pages = [{"name": DEFAULT_PAGE_NAME,
                           "widgets": [dict(item) for item in layout.get("widgets", [])]}]
        doc = {key: value for key, value in layout.items()
               if key not in ("widgets", "pages", "cycle_min", "shuffle")}
        doc["cycle_min"] = int(layout.get("cycle_min") or 0)
        doc["pages"] = norm_pages
        _atomic_write(self._canonical(device), doc)
        return doc
