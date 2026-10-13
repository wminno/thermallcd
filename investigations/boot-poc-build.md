---
type: concept
title: 'Investigation: boot showed old PoC build'
source_kind: put_page
ingested_via: put_page
ingested_at: '2026-10-13T07:58:19.413Z'
tags:
  - boot-service
  - investigation
  - trofeo-panel
---

## Symptom
On reboot the panel showed the original proof-of-concept build (nvtop left half + "widget goes here" placeholder) instead of the dashboard platform.

## Root cause (two layers)
1. `/etc/systemd/system/thermallcd.service` was installed in the PoC era and never re-run: ExecStart ran `thermallcd/monitor.py` (the PoC entry point drawing "widget goes here" at monitor.py:61, spawning /usr/bin/nvtop). Repo template `deploy/thermallcd.service.template` runs `python3 -m thermallcd.supervisor --port 8899` — but `deploy/install.sh` had not been re-run since the platform replaced the PoC.
2. Re-running install.sh then crash-looped: install.sh substituted `@REPO@` with `$DIR` = the deploy/ directory (install.sh's own location), so WorkingDirectory=deploy/ had no `thermallcd` package. Fixed in install.sh: `REPO="$(cd "$DIR/.." && pwd)"`.

## Panel behavior (not stale files)
The Trofeo panel retains the last pushed frame after the writer process dies. On any service restart the old image shows for the ~2s handover gap (old process killed -> handshake -> panel open -> chromium -> first frame). Not a stale-file artifact; no code path draws the PoC anymore.

## Cleanup
- Deleted `thermallcd/monitor.py` (dead PoC entry point; `nvtop_pane.py` stays — dashboard.py NvtopSource uses it).
- Deleted stale `data/layouts/default.corrupt-1791797545.json` backup.

## Verification
Service active, supervisor PID 85872, capture handshake profile (1920, 462), chromium :9222, ~12 FPS streaming. Repo grep: zero "widget goes here" hits, zero monitor.py references in thermallcd/ and deploy/.
