"""Widget registry: discover widgets/ drop-ins and validate their manifests.

A widget is a directory under widgets/ containing manifest.json (+ index.html
and any assets). Malformed widgets are skipped but surfaced with a failure
reason (D25) — never silently dropped.
"""

import json
from pathlib import Path

GRID_COLS = 12
GRID_ROWS = 3
RESIZE_MODES = ("free", "fixed")
SOURCE_MODES = ("html", "image")
OPTION_TYPES = ("select", "text")


def _validate_options(raw, errors):
    """Per-instance options (T17): fields the editor offers when the widget
    is selected; values ride the layout item and reach the widget as
    window.TLCD_OPTIONS. Structural validation only — the editor constrains
    values to the manifest's choices."""
    if not isinstance(raw, list):
        errors.append("manifest.options must be a list")
        return None
    out, seen = [], set()
    for spec in raw:
        if not isinstance(spec, dict) or not isinstance(spec.get("key"), str) \
                or not spec["key"]:
            errors.append("each manifest.options entry needs a non-empty key string")
            return None
        key = spec["key"]
        if key in seen:
            errors.append(f"manifest.options key {key!r} appears twice")
            return None
        seen.add(key)
        typ = spec.get("type", "select")
        if typ not in OPTION_TYPES:
            errors.append(f"options.{key}: type {typ!r} unknown "
                          f"(expected one of {OPTION_TYPES})")
            return None
        entry = {"key": key, "type": typ, "label": spec.get("label", key)}
        if typ == "select":
            choices = spec.get("choices")
            if not isinstance(choices, list) or not choices:
                errors.append(f"options.{key}: select needs a non-empty choices list")
                return None
            norm, vals = [], set()
            for ch in choices:
                if not isinstance(ch, dict) or not isinstance(ch.get("value"), str) \
                        or not ch["value"]:
                    errors.append(f"options.{key}: each choice needs a value string")
                    return None
                if ch["value"] in vals:
                    errors.append(f"options.{key}: choice {ch['value']!r} appears twice")
                    return None
                vals.add(ch["value"])
                norm.append({"value": ch["value"],
                             "label": ch.get("label", ch["value"])})
            entry["choices"] = norm
        out.append(entry)
    return out


def _validate_size(raw, what, errors):
    if not isinstance(raw, dict):
        errors.append(f"{what} must be an object")
        return None
    out = {}
    for axis in ("w", "h"):
        v = raw.get(axis)
        if not isinstance(v, int) or v < 1:
            errors.append(f"{what}.{axis} must be an integer >= 1")
            return None
        out[axis] = v
    return out


def validate_manifest(directory: Path, manifest: dict) -> dict:
    """Validate one parsed manifest; returns the registry entry (with `error`)."""
    errors = []
    name = manifest.get("name")
    if not isinstance(name, str) or not name:
        errors.append("manifest.name must be a non-empty string")
    elif name != directory.name:
        errors.append(f"manifest.name {name!r} must match directory {directory.name!r}")

    size = _validate_size(manifest.get("size"), "manifest.size", errors)
    if size is not None:
        if size["w"] > GRID_COLS:
            errors.append(f"size.w {size['w']} exceeds the {GRID_COLS}-column grid")
        if size["h"] > GRID_ROWS:
            errors.append(f"size.h {size['h']} exceeds the {GRID_ROWS}-row grid")

    resize = manifest.get("resize", "free")
    if resize not in RESIZE_MODES:
        errors.append(f"resize mode {resize!r} unknown (expected one of {RESIZE_MODES})")

    # SC4: the schema covers non-HTML sources — "image" widgets are served
    # pixels by the platform (nvtop), "html" is the default iframe widget
    source = manifest.get("source", "html")
    if source not in SOURCE_MODES:
        errors.append(f"source mode {source!r} unknown (expected one of {SOURCE_MODES})")

    min_size = None
    if "min" in manifest:
        min_size = _validate_size(manifest["min"], "manifest.min", errors)

    options = None
    if "options" in manifest:
        options = _validate_options(manifest["options"], errors)

    entry = {
        "name": name if isinstance(name, str) else directory.name,
        "title": manifest.get("title", name),
        "description": manifest.get("description", ""),
        "size": size,
        "resize": resize,
        "min": min_size,
        "source": source,
        "options": options,
    }
    if errors:
        entry["error"] = "; ".join(errors)
    return entry


class Registry:
    def __init__(self, widgets_dir: Path):
        self.widgets_dir = Path(widgets_dir)
        self._cache = {}
        self._mtime = None

    def scan(self) -> list[dict]:
        """All entries — good ones with size/resize, malformed with `error` (D25)."""
        if not self.widgets_dir.is_dir():
            return []
        entries = []
        for directory in sorted(self.widgets_dir.iterdir()):
            if not directory.is_dir() or directory.name.startswith("."):
                continue
            manifest_path = directory / "manifest.json"
            if not manifest_path.is_file():
                continue  # not a widget (assets dir, theme.css, boot.js live above)
            entry = self._load_one(directory, manifest_path)
            entries.append(entry)
        return entries

    def _load_one(self, directory: Path, manifest_path: Path) -> dict:
        try:
            mtime = manifest_path.stat().st_mtime
            cached = self._cache.get(directory.name)
            if cached and cached[0] == mtime:
                return cached[1]
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if not isinstance(manifest, dict):
                entry = {"name": directory.name, "error": "manifest must be a JSON object"}
            else:
                entry = validate_manifest(directory, manifest)
        except json.JSONDecodeError as e:
            entry = {"name": directory.name, "error": f"manifest parse error: {e}"}
        except OSError as e:
            entry = {"name": directory.name, "error": f"manifest unreadable: {e}"}
        self._cache[directory.name] = (manifest_path.stat().st_mtime if manifest_path.exists() else 0, entry)
        return entry

    def get(self, name: str) -> dict | None:
        """One entry by name; None if the widget is absent or malformed."""
        for entry in self.scan():
            if entry["name"] == name and "error" not in entry:
                return entry
        return None

    def widget_file(self, name: str, relpath: str) -> Path | None:
        """Resolved path for a widget asset, or None (traversal-guarded)."""
        base = (self.widgets_dir / name).resolve()
        target = (base / relpath).resolve()
        if target.is_file() and base in target.parents:
            return target
        return None
