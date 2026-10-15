"""External weather source for the weather widget (T20, Open-Meteo keyless).

One slow refresher thread fetches every weather placement's location and
publishes per-location entries that MetricsSampler copies into
data["weather"]. The invariant that shapes this module: the sampler's 1 s
tick thread only ever reads the published cache (a dict copy under the
lock) — every network call lives here. Locations come from the saved
layout (the tty source's precedent), so they are set only through the
token-gated editor and persist only in gitignored data/ — the repo ships
none. A failed fetch keeps the last good entry (stale-while-error) or
publishes an honest error entry; values are never fabricated (D23).
"""

import json
import os
import re
import threading
import time
import urllib.parse
import urllib.request

from thermallcd import layout as layout_mod

# Env-overridable for tests and air-gapped hosts (the llama-swap URL
# precedent, metrics.py). timezone=auto is required for daily variables.
GEOCODE_URL = os.environ.get(
    "THERMALLCD_GEOCODE_URL",
    "https://geocoding-api.open-meteo.com/v1/search?name={name}&count=1")
WEATHER_URL = os.environ.get(
    "THERMALLCD_WEATHER_URL",
    "https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}"
    "&current=temperature_2m,apparent_temperature,weather_code"
    "&daily=temperature_2m_max,temperature_2m_min,weather_code"
    "&timezone=auto&forecast_days=3")

FETCH_TIMEOUT_S = 8.0
TTL_S = 900.0        # 15 min: one Open-Meteo fetch per location per interval
RETRY_S = 300.0      # backoff after a failure (and for a geocode miss)
GEO_TTL_S = 86400.0  # a location's coordinates are stable: cache for a day
POLL_S = 10.0        # layout rescan cadence — how fast an edited location shows
WIDGET_ID = "weather"

_COORDS = re.compile(r"^\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*$")


def _parse_coords(location):
    """`lat,lon` passthrough (T20): coordinate text skips geocoding. None
    when the text is not a coordinate pair or is out of range."""
    m = _COORDS.match(location or "")
    if not m:
        return None
    lat, lon = float(m.group(1)), float(m.group(2))
    if abs(lat) > 90 or abs(lon) > 180:
        return None
    return lat, lon


def _fetch_json(url, timeout=FETCH_TIMEOUT_S):
    """GET + JSON-decode. Raises on any failure — the caller decides what
    the failure means (unreachable service, not a missing location)."""
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


class WeatherSource:
    """Per-location Open-Meteo cache behind the metrics snapshot."""

    def __init__(self, layout):
        self.layout = layout
        self._entries = {}   # location -> published entry (ok or error)
        self._expires = {}   # location -> monotonic refresh deadline
        self._geo = {}       # location -> (expires, (lat, lon, resolved)|None)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._refresh_loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def snapshot(self) -> dict:
        """The published entries, copied under the lock. This is the ONLY
        cross-thread surface: the sampler tick calls just this."""
        with self._lock:
            return dict(self._entries)

    def _locations(self):
        """Distinct trimmed location options of every placed weather widget
        (T28 pages: across all pages). No placement — no fetches."""
        found = []
        for page in layout_mod.pages_of(self.layout.load("default")):
            for item in page.get("widgets") or []:
                if item.get("id") != WIDGET_ID:
                    continue
                loc = str((item.get("options") or {}).get("location") or "").strip()
                if loc and loc not in found:
                    found.append(loc)
        return found

    def _resolve(self, location):
        """(lat, lon, resolved-name dict) for a location, or None on a
        definitive geocode miss. `lat,lon` text passes through ungeocoded.
        Geocoder failures raise (the caller keeps serving the last good
        entry); a miss is negatively cached for RETRY_S so a typo'd name
        doesn't re-query every cycle."""
        coords = _parse_coords(location)
        if coords:
            return coords[0], coords[1], {"name": location}
        now = time.monotonic()
        hit = self._geo.get(location)
        if hit and hit[0] > now:
            return hit[1]  # a day-fresh resolution — or a cached miss (None)
        results = _fetch_json(GEOCODE_URL.format(
            name=urllib.parse.quote(location))).get("results") or []
        if not results:
            self._geo[location] = (now + RETRY_S, None)
            return None
        first = results[0]
        out = (first["latitude"], first["longitude"],
               {k: first.get(k) for k in ("name", "admin1", "country")})
        self._geo[location] = (now + GEO_TTL_S, out)
        return out

    def _fetch_location(self, location):
        """The published entry for one location: ok with the Open-Meteo
        payload, or an error entry for a definitive geocode miss. Raises
        when the service is unreachable — the caller keeps the last good
        entry then. The entry's own ttl is its refresh deadline."""
        resolved = self._resolve(location)
        if resolved is None:
            return {"state": "error", "error": "location not found",
                    "ts": time.time(), "ttl": RETRY_S}
        lat, lon, where = resolved
        doc = _fetch_json(WEATHER_URL.format(lat=f"{lat:.4f}", lon=f"{lon:.4f}"))
        cur = doc.get("current") or {}
        daily = doc.get("daily") or {}
        return {
            "state": "ok", "ts": time.time(), "ttl": TTL_S,
            "resolved": where,
            "current": {"temperature_2m": cur.get("temperature_2m"),
                        "apparent_temperature": cur.get("apparent_temperature"),
                        "weather_code": cur.get("weather_code")},
            "daily": {"time": daily.get("time") or [],
                      "temperature_2m_max": daily.get("temperature_2m_max") or [],
                      "temperature_2m_min": daily.get("temperature_2m_min") or [],
                      "weather_code": daily.get("weather_code") or []},
        }

    def refresh_once(self):
        """One refresh cycle: re-read the layout's weather locations, evict
        removed ones, fetch every location past its deadline. Tests drive
        this directly; the thread just loops it. Per-location failures cost
        only that location; a whole-cycle failure never kills the loop."""
        locations = self._locations()
        with self._lock:
            for gone in [k for k in self._entries if k not in locations]:
                del self._entries[gone]
            for gone in [k for k in self._expires if k not in locations]:
                del self._expires[gone]
            for gone in [k for k in self._geo if k not in locations]:
                del self._geo[gone]
        now = time.monotonic()
        for loc in locations:
            if self._expires.get(loc, 0) > now:
                continue
            try:
                entry = self._fetch_location(loc)
            except Exception:
                entry = None
            with self._lock:
                if entry is None:
                    prev = self._entries.get(loc)
                    if not prev or prev.get("state") != "ok":
                        self._entries[loc] = {
                            "state": "error",
                            "error": "weather service unreachable",
                            "ts": time.time(), "ttl": RETRY_S}
                    # an ok entry stays untouched (stale-while-error): the
                    # widget's age check dims it once past its ttl
                else:
                    self._entries[loc] = entry
            self._expires[loc] = time.monotonic() + (
                entry["ttl"] if entry else RETRY_S)

    def _refresh_loop(self):
        # NvtopSource's repaint shape: wait FIRST, so construction never
        # races the first cycle (and tests never see a fetch on start).
        while not self._stop.wait(POLL_S):
            try:
                self.refresh_once()
            except Exception:  # a bad cycle never kills the refresher
                pass
