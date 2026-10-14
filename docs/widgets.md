# Widget authoring contract (D30)

Everything a widget author needs: what a widget is, what the platform
guarantees, and what it expects in return. One worked example at the bottom.
The upstream design decisions (D-numbers) live in
`docs/designs/dashboard-platform.md`.

## What a widget is

A directory under `widgets/` containing `manifest.json` plus an `index.html`
(and any assets). Drop the directory in place — the registry picks it up on
the next scan; the editor palette lists it on the next page load. Two
shared files live one level above your directory and are linked, not
duplicated: `widgets/theme.css` (tokens + base styles) and `widgets/boot.js`
(metrics helper).

    widgets/
      theme.css          <- shared token stylesheet (link it)
      boot.js            <- shared metrics helper (script it)
      cpu-load/
        manifest.json
        index.html

## manifest.json

```json
{
  "name": "cpu-load",
  "title": "CPU-LOAD",
  "description": "Overall and per-core CPU usage.",
  "size": { "w": 3, "h": 2 },
  "resize": "free",
  "min": { "w": 2, "h": 1 }
}
```

| field         | meaning                                                                 |
|---------------|-------------------------------------------------------------------------|
| `name`        | must equal the directory name (validated)                               |
| `title`       | editor palette label; defaults to `name`                                |
| `description` | one line for the palette                                                |
| `size`        | default placement in grid units, `w` ≤ 12, `h` ≤ 3                      |
| `resize`      | `"free"` (default) or `"fixed"` — the editor offers resizing or not     |
| `min`         | optional minimum when `resize: "free"`                                  |
| `source`      | `"html"` (default, an iframe) or `"image"` (platform-served pixels)     |

A malformed manifest never silently disappears: the widget shows up in the
editor palette with the parse/validate error (`registry.py` collects them,
D25). The dashboard itself skips malformed widgets — the scene shows
through the empty slot (D22).

## Geometry: grid units and real pixels

The panel is a 12×3 grid over a 1920×462 canvas (per-device profiles rescale
the cell — grid units are what you author in). 8 px gutters, 16 px page
inset. A slot at `w × h` measures:

    width  = 150·w + 8·(w−1)
    height = 138·h + 8·(h−1)

Your iframe fills its slot exactly (`minmax(0,1fr)` tracks, D33) — size
content against those numbers. `1×1` = 150×138, `6×3` = 940×430. Content
that overflows is clipped (`overflow: hidden`), never scrolls.

## Isolation and the token

Each widget renders in its own iframe via `srcdoc` (D21): a crash or bad
script in your widget cannot take down the page or other widgets. The
platform injects the auth token into your `<head>` as
`window.TLCD_TOKEN` — use it for any fetch of an `/api/*` route, and read
it at call time (it is injected after your `<script>` tags on srcdoc
reloads). The `boot.js` helper already handles this for metrics.

## View options (per-instance, T17)

A widget can declare options the editor offers when a placed instance is
selected — view variants, a command, anything the instance needs:

```json
"options": [
  { "key": "view", "label": "View", "type": "select",
    "choices": [
      { "value": "bars", "label": "Per-core bars" },
      { "value": "history", "label": "History graph" }
    ] }
]
```

Two field types: `"select"` (a fixed choice list) and `"text"` (free text,
for a command or a location). Values are per-instance: two placements of
the same widget can differ. The platform injects the instance's chosen
values into your iframe as `window.TLCD_OPTIONS` (a plain object, `{}`
when unset) — read them at boot: they are baked into the frame and never
change while it runs (changing an option in the editor swaps the frame).
An unset value means your widget's own default.

## Theme tokens (D32)

Link `<link rel="stylesheet" href="/widgets/theme.css">` and consume only
the custom properties — never hardcode color or font:

| token                 | role                                             |
|-----------------------|--------------------------------------------------|
| `--tlcd-surface`      | card background                                  |
| `--tlcd-surface-2`    | raised surface                                   |
| `--tlcd-field`        | wells, meter tracks                              |
| `--tlcd-line`         | hairlines, borders                               |
| `--tlcd-text`         | primary text (≥ 13:1 over glass, worst case)     |
| `--tlcd-dim`          | secondary text (≥ 4.5:1 over glass, worst case — D35) |
| `--tlcd-accent`       | accent (bars, highlights)                        |
| `--tlcd-warn`         | warnings, stale badge                            |
| `--tlcd-font-display` | display face (titles)                            |
| `--tlcd-font-mono`    | body/numbers                                     |

Ready-made classes: `.tlcd-title` (14px letterspaced card title with
hairline rule), `.tlcd-big` (40px tier-1 number), `.tlcd-mid` (24px),
`.tlcd-dim-text`, `.tlcd-accent-text`, `.tlcd-bar` (+ `i` fill). The type
tiers are D18's glance hierarchy: one 40px number per card, supporting
detail smaller and dimmer. Body background stays transparent — your card
IS the glass panel (D18); never paint an opaque card background.

## Metrics and the stale contract (D23)

`boot.js` exposes `window.tlcd`:

```js
window.tlcd.onMetrics(function (state, data) {
  // state: "live" | "stale"
  // data:  the /api/metrics snapshot body (PF1)
});
```

The helper polls `/api/metrics` for you at the snapshot's own refresh rate,
deduplicates ticks, and enforces the stale contract platform-side: on
`"stale"` it adds `.tlcd-stale` to `body` (dims to 40% and badges "stale")
and keeps your last values on screen. Your obligations:

- On `state === "stale"`, return early — keep the last real values, never
  clear the card.
- Missing data renders as `—`, never as zero. A fabricated zero is a lie
  the user can't distinguish from a dead sensor.
- Don't run your own fetch loop for metrics; one poll per tick serves every
  widget on the panel.

## Resize behavior

`resize: "free"` widgets reflow to any slot ≥ their `min`; the editor
enforces the minimum while dragging. `resize: "fixed"` widgets always
render at `size` — the editor locks their handles. Design free widgets to
degrade gracefully: put the tier-1 number first in the DOM so a shrunken
slot clips the detail, not the headline. Widgets that need a floor below
`min` should render a designed "too small" state instead of broken layout.

## Image-source widgets

`"source": "image"` opts out of the iframe: the platform serves your pixels
directly (nvtop: `/api/nvtop`, a transparent-background PNG sized
server-side from the slot). You ship no `index.html`; the platform owns
rendering. This is for widgets whose content is a process rendered into a
pty or similar — if you're painting DOM, you want the default `"html"`.

T28 pages: with a multi-page layout the pane derives its slot size (nvtop)
and its command (tty) from the **first page carrying the widget**, in
document order — the server cannot know which page a cycling panel shows.
A tty with a different `cmd` per page serves the first page's command; the
pane itself stays warm across page swaps (it keys on the saved layout, not
the DOM), and a widget not on the displayed page simply stops being polled.

### The tty widget (T18) — a command in a pane

The bundled `tty` widget is an image source with one text option, `cmd`:
the platform runs the command under `sh -c` in a pty sized to the slot and
serves its terminal grid as pixels. Pipes and arguments work
(`top -d 2`, `watch -n1 'sensors | cut -c…'`).

Security posture, stated plainly: whoever holds the dashboard token is the
machine's owner, and the command is only writable through the token-gated
editor — it lives in the layout file and is never taken from the request
(``/api/tty`` ignores query params entirely, D19). The pane kills its whole
process group on recreation, removal and service stop, so a command's
children don't outlive it. If you would not paste the command into that
machine's own shell, do not put it in the option.

## Motion budget (D37)

The platform tick drives the panel: `--tlcd-tick` steps at ~15 Hz, matching
the 15 FPS capture, so every panel frame carries fresh pixels. Nothing in
a widget should cycle faster than the tick, and ambient animation should
sit well under it — a 0.2–0.5 Hz pulse reads smoothly; anything faster
strobes at the capture rate. Prefer animating opacity/transform/color of
existing elements over layout.

## Accessibility (D35)

- Text over the glass must clear 4.5:1 in the worst case (the scene's
  brightest pixels bleed through the 82% glass) — the tokens guarantee
  this; a hardcoded color almost certainly won't.
- Widget content is display-only (the panel has no pointer input); keep
  the information in the numbers, not the color — `--tlcd-accent` and
  `--tlcd-warn` differ by more than hue.

## Worked example

A complete widget — the skeleton to copy:

```html
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<link rel="stylesheet" href="/widgets/theme.css">
<script src="/widgets/boot.js"></script>
</head>
<body>
  <div class="tlcd-title">MEMORY</div>
  <div class="tlcd-big"><span id="v">&mdash;</span><span class="tlcd-accent-text">%</span></div>
  <div id="detail" class="tlcd-dim-text" style="font-size:14px"></div>
<script>
  window.tlcd.onMetrics(function (state, data) {
    if (state === "stale") return;          // keep last values, stay dimmed
    if (data.mem == null) return;           // no measurement — never fake a zero
    document.getElementById("v").textContent = Math.round(data.mem.used_pct);
    document.getElementById("detail").textContent =
      (data.mem.avail_kb / 1048576).toFixed(1) + " GiB free";
  });
</script>
</body>
</html>
```

with `manifest.json` naming the directory and a `size` that fits the
content (`1×2` fits a title + number + one detail line). The snapshot's
fields (`cpu`, `cpu_cores`, `mem`, `load`, `disk`, `gpu`, `llm_tasks`) come
from the platform's monitor — a widget never shells out for its own data
unless it ships a source module like nvtop's. That's the whole contract:
tokens in, metrics through `window.tlcd`, honest empty states, and let the
platform do the rest.
