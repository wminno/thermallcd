/* flair — synthwave scene layer (T13), ported from retro-synth/synth.html.
   The engine is the original canvas renderer; the clock is not. D37 motion
   budget: the panel captures ~6 FPS, so nothing here animates on its own —
   the dashboard's platform tick (0.5 Hz, --tlcd-tick) drives one deliberate
   repaint per step, with camera drift small enough to read as a crawl.
   Between steps the page paints nothing and the capture sees no frames.

   Exposed as window.TLCDFlair: mount() runs the live scene, paintThumb()
   renders static thumbnails for the editor picker, apply() hot-swaps the
   live scene (editor preview pushes). */
(function () {
"use strict";

/* ============ configuration (verbatim scene/theme tables from synth.html) ============ */

var THEMES = {
  synthwave:{ label:"Synthwave",
    skyTop:"#0b0121", skyMid:"#3c1169", skyHor:"#d63e7a",
    sun1:"#ffcf5e", sun2:"#ff2e83", grid:"#ff2e83",
    mFar:{fill:"#180a33", stroke:"#b12a8f"}, mNear:{fill:"#0d0524", stroke:"#ff2e83"},
    star:"#ffffff" },
  vaporwave:{ label:"Vaporwave",
    skyTop:"#182a54", skyMid:"#5a4fae", skyHor:"#ef92c6",
    sun1:"#ffe3c2", sun2:"#ff8fc7", grid:"#5ef2ff",
    mFar:{fill:"#1b2a5e", stroke:"#8f7bd9"}, mNear:{fill:"#101a40", stroke:"#ff9ad5"},
    star:"#e8f4ff" },
  outrun:{ label:"Outrun",
    skyTop:"#02081f", skyMid:"#0c2f6b", skyHor:"#d7263d",
    sun1:"#ffc233", sun2:"#e63946", grid:"#39a0ff",
    mFar:{fill:"#061431", stroke:"#8c2f4f"}, mNear:{fill:"#030b1f", stroke:"#e63946"},
    star:"#cfe4ff" },
  miami:{ label:"Miami",
    skyTop:"#0d0b2e", skyMid:"#45197d", skyHor:"#ff4fa0",
    sun1:"#ffe259", sun2:"#ff4d9d", grid:"#25e8ff",
    mFar:{fill:"#1a0f45", stroke:"#9a4dff"}, mNear:{fill:"#0e0830", stroke:"#25e8ff"},
    star:"#dff6ff" },
  cyber:{ label:"Cyber",
    skyTop:"#010403", skyMid:"#04291f", skyHor:"#0be08a",
    sun1:"#d8ffe9", sun2:"#0be08a", grid:"#00ff9d",
    mFar:{fill:"#02231a", stroke:"#0a8f5c"}, mNear:{fill:"#01130e", stroke:"#00ff9d"},
    star:"#aaffd0" },
  noir:{ label:"Noir",
    skyTop:"#060606", skyMid:"#1c1c1e", skyHor:"#9a9aa2",
    sun1:"#f2f2f2", sun2:"#8e8e96", grid:"#e6e6ea",
    mFar:{fill:"#101012", stroke:"#55555c"}, mNear:{fill:"#08080a", stroke:"#dcdce2"},
    star:"#ffffff" },
};

var M = function (par, freq, base, amp, seed) {
  return {par: par, freq: freq, base: base, amp: amp, seed: seed};
};

var SCENES = {
  sunset:{ label:"Retro Sunset",
    sun:[{r:.34, yf:.5, x:0}],
    mts:[M(.05,.0021,.04,.115,3.7), M(.1125,.0032,.015,.19,9.2)] },
  grid:{ label:"Infinite Grid",
    sun:[{r:.16, yf:.85, x:0}], mts:[] },
  ridge:{ label:"Mountain Ridge",
    sun:[], mts:[M(.045,.0019,.06,.19,12.4), M(.105,.0029,.025,.27,9.2)] },
  city:{ label:"Neon City",
    sun:[{r:.13, yf:.9, x:0}], mts:[],
    city:[{par:.035, cw:.017, hmin:.05, hvar:.13, seed:5.1, win:false},
          {par:.07,  cw:.03,  hmin:.015,hvar:.19, seed:8.4, win:true}] },
  twin:{ label:"Twin Suns",
    sun:[{r:.19, yf:.5, x:-.16}, {r:.115, yf:.85, x:.21}],
    mts:[M(.05,.0021,.045,.14,6.1)] },
  valley:{ label:"Side Ranges", motions:["forward","backward","none"],
    sun:[{r:.20, yf:.5, x:0}], mts:[],
    terrain:{amp:2.6, seed:3.7, gap:4, fade:1.0} },
  canyon:{ label:"Deep Canyon", motions:["forward","backward","none"],
    sun:[{r:.16, yf:.75, x:0}], mts:[],
    terrain:{amp:3.6, seed:6.4, gap:3, fade:0.9} },
  mesa:{ label:"Mesa Vista", motions:["forward","backward","none"],
    sun:[{r:.19, yf:.6, x:0}], mts:[],
    sides:[{par:.05, freq:.0024, base:.10, amp:.24, seed:11.6, gap:.32, shape:"mesa"},
           {par:.1125, freq:.0036, base:.05, amp:.30, seed:7.8, gap:.38, shape:"mesa"}] },
};

var MOTION_LABELS = {left:"Drift left", right:"Drift right",
                     forward:"Drive forward", backward:"Roll backward", none:"Still"};
var DEFAULTS = {scene:"sunset", theme:"synthwave", motion:"right",
                glass:.82, speed:25, spectrum:true, stars:true, road:false,
                scanlines:true, enabled:true};

/* D37: per-tick camera step. The original deck ran camX at 1.7 units/s
   (≈117 px/s at panel width) and the tick keeps that speed: camX advances
   CAM_SPEED * TICK_S per tick. The tick runs just above the capture's 6 FPS
   so every panel frame carries fresh pixels — evenly spaced small steps,
   the smoothest motion the firmware frame rate allows. Earlier cuts failed
   on both ends: 2 s ticks crawled ~100x slower (frozen), and 4 Hz steps of
   0.42 units read as teleporting (jarring). */
var TICK_S = 0.14;         // matches the dashboard's ~7 Hz platform tick
var CAM_SPEED = 1.7;       // lateral grid units/s at speed 100 — the deck's rate
var CAM_Z_SPEED = 0.9;     // forward/backward units/s at speed 100 — deck's rate

/* ============ helpers (verbatim) ============ */

function hash(n){ const s = Math.sin(n) * 43758.5453; return s - Math.floor(s); }
function noise1(x){
  const i = Math.floor(x), f = x - i, u = f * f * (3 - 2 * f);
  return hash(i) * (1 - u) + hash(i + 1) * u;
}
function fbm(x){ return noise1(x) * .55 + noise1(x * 2.31 + 7.3) * .28 + noise1(x * 4.73 + 13.1) * .17; }
function hash2(x, y){ const s = Math.sin(x * 127.1 + y * 311.7) * 43758.5453; return s - Math.floor(s); }
function noise2(x, y){
  const ix = Math.floor(x), iy = Math.floor(y), fx = x - ix, fy = y - iy;
  const ux = fx * fx * (3 - 2 * fx), uy = fy * fy * (3 - 2 * fy);
  const a = hash2(ix, iy), b = hash2(ix + 1, iy), c = hash2(ix, iy + 1), d = hash2(ix + 1, iy + 1);
  return a + (b - a) * ux + (c - a) * uy + (a - b - c + d) * ux * uy;
}
function hexRGB(h){ const n = parseInt(h.slice(1), 16); return [n >> 16 & 255, n >> 8 & 255, n & 255]; }
function hexA(h, a){ const c = hexRGB(h); return "rgba(" + c[0] + "," + c[1] + "," + c[2] + "," + a + ")"; }
function shade(h, f){ const c = hexRGB(h); return "rgb(" + (c[0]*f|0) + "," + (c[1]*f|0) + "," + (c[2]*f|0) + ")"; }

/* ============ canvas binding (one ctx; live scene and thumbnails share it) ============ */

let cvs = null, ctx = null, W = 2, H = 2;
function bindCanvas(canvas){
  cvs = canvas;
  ctx = cvs.getContext("2d", {alpha: false});
  W = cvs.width; H = cvs.height;
  GRKey = "";
  sunCache.clear();
}

/* ============ gradients (cached per theme+size, as upstream) ============ */

let GR = null, GRKey = "";
function grads(th){
  const key = th.skyTop + th.skyHor + th.grid + W + "x" + H;
  if (GR && GRKey === key) return GR;
  GRKey = key;
  const hor = H * .64, K = (H - hor) * 1.75;
  GR = {};
  let g = ctx.createLinearGradient(0, 0, 0, hor);          // sky
  g.addColorStop(0, th.skyTop); g.addColorStop(.55, th.skyMid); g.addColorStop(1, th.skyHor);
  GR.sky = g;
  g = ctx.createLinearGradient(0, hor, 0, H);              // floor
  g.addColorStop(0, shade(th.skyHor, .30)); g.addColorStop(1, shade(th.skyHor, .07));
  GR.floor = g;
  g = ctx.createLinearGradient(0, hor, 0, hor + H * .16);  // horizon haze band
  g.addColorStop(0, hexA(th.skyHor, .30)); g.addColorStop(1, hexA(th.skyHor, 0));
  GR.haze = g;
  g = ctx.createLinearGradient(0, hor, 0, hor + K);        // road surface
  g.addColorStop(0, shade(th.skyHor, .05)); g.addColorStop(1, shade(th.skyHor, .13));
  GR.road = g;
  g = ctx.createLinearGradient(0, hor, 0, H);              // terrain: floor fill with the
  for (let k = 0; k <= 10; k++){                           // haze band composited in
    const y = hor + (H - hor) * k / 10;
    let f = .30 - .23 * (y - hor) / (H - hor);
    if (y < hor + H * .16){
      const a = .30 * (1 - (y - hor) / (H * .16));
      f = f * (1 - a) + a;
    }
    g.addColorStop(k / 10, shade(th.skyHor, f));
  }
  GR.terrain = g;
  g = ctx.createRadialGradient(W / 2, H * .52, Math.min(W, H) * .34,   // vignette
                               W / 2, H * .52, Math.max(W, H) * .74);
  g.addColorStop(0, "rgba(0,0,0,0)"); g.addColorStop(1, "rgba(0,0,0,.42)");
  GR.vig = g;
  return GR;
}

/* ============ sun (offscreen: glow + gradient disc + scanline cuts) ============ */

const sunCache = new Map();
function sunCanvas(r, th){
  const key = Math.round(r * 8) + "|" + th.sun1 + th.sun2;
  let c = sunCache.get(key);
  if (c) return c;
  const S = Math.ceil(r * 2.7), cc = S / 2;
  c = document.createElement("canvas"); c.width = c.height = S;
  const g = c.getContext("2d");
  let gr = g.createRadialGradient(cc, cc, r * .1, cc, cc, r * 1.28);
  gr.addColorStop(0, hexA(th.sun2, .5)); gr.addColorStop(.55, hexA(th.sun2, .16)); gr.addColorStop(1, hexA(th.sun2, 0));
  g.fillStyle = gr; g.fillRect(0, 0, S, S);
  gr = g.createLinearGradient(0, cc - r, 0, cc + r);
  gr.addColorStop(0, th.sun1); gr.addColorStop(1, th.sun2);
  g.fillStyle = gr; g.beginPath(); g.arc(cc, cc, r, 0, 7); g.fill();
  g.globalCompositeOperation = "destination-out";
  for (let k = 0; k < 10; k++){
    const o = r * (.06 + .11 * k + .028 * k * k);
    if (o > r) break;
    const t = r * (.018 + .021 * k);
    g.fillRect(0, cc + o - t / 2, S, t);
  }
  g.globalCompositeOperation = "source-over";
  sunCache.set(key, c);
  return c;
}
function drawSun(s, th, hor, cx){
  const r = s.r * H, x = cx + s.x * W, cy = hor - r * s.yf;
  const c = sunCanvas(r, th);
  ctx.drawImage(c, x - c.width / 2, cy - c.height / 2);
}

/* ============ stars ============ */

function drawStars(th, hor, t){
  const B = W / 7, off = camX * B * .05;
  const cell = Math.max(24, W * .02);
  const i0 = Math.floor(-off / cell) - 1, i1 = Math.floor((W - off) / cell) + 1;
  ctx.fillStyle = th.star;
  for (let i = i0; i <= i1; i++){
    const h1 = hash(i * 7.31 + 1.7), h2 = hash(i * 3.77 + 9.1), h3 = hash(i * 1.93 + 4.2);
    const sx = (i + h1) * cell - off, sy = (.06 + .86 * h2) * hor;
    const tw = Math.sin(t * (.6 + h3 * 1.6) + h3 * 6.28);
    ctx.globalAlpha = (.30 + .55 * tw * tw) * (1 - .55 * (sy / hor));
    const r = .5 + h3 * 1.1;
    ctx.fillRect(sx, sy, r, r);
  }
  ctx.globalAlpha = 1;
}

/* ============ mountains ============ */

function ridgeY(sx, L, off, hor){
  let n = Math.pow(fbm((sx + off) * L.freq + L.seed), 1.35);
  if (L.shape === "mesa"){                       // flat-topped plateaus
    const cap = .30 + .22 * noise1((sx + off) * L.freq * .37 + L.seed * 2.7);
    n = Math.min(n, cap);
  }
  let h = (L.base + n * L.amp) * H;
  if (L.gap != null){                            // flank mask: ridges rise at the
    const m = Math.max(0, Math.min(1, (Math.abs(sx - W / 2) - L.gap * W / 2) / (W * .16)));
    h *= m * m * (3 - 2 * m);                    // sides, dipping to a center corridor
  }
  return hor - h;
}
function drawRidge(L, colors, hor, B){
  const off = camX * B * L.par;
  ctx.beginPath();
  ctx.moveTo(-4, hor + 2);
  for (let sx = -4; sx <= W + 4; sx += 3) ctx.lineTo(sx, ridgeY(sx, L, off, hor));
  ctx.lineTo(W + 4, hor + 2);
  ctx.closePath();
  ctx.fillStyle = colors.fill; ctx.fill();
  ctx.beginPath();                               // stroke breaks where the ridge
  let pen = false;                               // meets the horizon (corridor)
  for (let sx = -4; sx <= W + 4; sx += 3){
    const y = ridgeY(sx, L, off, hor);
    if (hor - y > 2){
      pen ? ctx.lineTo(sx, y) : ctx.moveTo(sx, y);
      pen = true;
    } else pen = false;
  }
  ctx.strokeStyle = colors.stroke;
  ctx.shadowColor = colors.stroke;
  ctx.globalAlpha = .18; ctx.lineWidth = 4.5; ctx.shadowBlur = 0; ctx.stroke();
  ctx.globalAlpha = .95; ctx.lineWidth = 1.6; ctx.shadowBlur = 8; ctx.stroke();
  ctx.globalAlpha = 1; ctx.shadowBlur = 0;
}

/* ============ city skyline ============ */

function drawCity(L, th, hor, t){
  const B = W / 7, off = camX * B * L.par, cw = Math.max(26, W * L.cw);
  const i0 = Math.floor(off / cw) - 1, i1 = Math.floor((off + W) / cw) + 1;
  const colors = L.win ? th.mNear : th.mFar;
  for (let i = i0; i <= i1; i++){
    let h = (L.hmin + fbm(i * .618 + L.seed) * L.hvar) * H, w = cw * .86;
    if (hash(i * 3.31 + L.seed * 7.7) > .82){ h *= 1.55; w *= .62; }
    const x = i * cw - off + (cw - w) / 2, y = hor - h;
    ctx.fillStyle = colors.fill;
    ctx.fillRect(x, y, w, h + 1);
    ctx.fillStyle = colors.stroke; ctx.globalAlpha = .85;
    ctx.fillRect(x, y, w, 1.6);
    ctx.globalAlpha = 1;
    if (L.win){
      // windows are indexed building-locally so their state never depends on
      // scroll position; each flips on/off on its own slow clock (5-14s) —
      // sampled at 0.5 Hz that stays well under the D37 budget
      ctx.fillStyle = th.grid;
      for (let c = 0; x + 4 + c * 9 < x + w - 4; c++){
        for (let r = 0; y + 8 + r * 11 < hor - 6; r++){
          const per = 5 + 9 * hash(i * 1.7 + c * 5.3 + r * 11.9);
          const flip = Math.floor(t / per);
          if (hash(i * 91.7 + c * 13.1 + r * 7.3 + flip) > .74){
            ctx.globalAlpha = .25 + .6 * hash(i * 3.7 + c * 1.9 + r * 23.3);
            ctx.fillRect(x + 4 + c * 9, y + 8 + r * 11, 2.2, 3.2);
          }
        }
      }
      ctx.globalAlpha = 1;
    }
  }
}

/* ============ perspective grid ============ */

function drawGrid(th, hor){
  const cx = W / 2, K = (H - hor) * 1.75;
  const waveRows = state.spectrum;   // waveform rows replace these lines
  ctx.strokeStyle = th.grid; ctx.shadowColor = th.grid;
  for (let n = Math.floor(camZ) + 1; n < Math.floor(camZ) + 17; n++){
    const z = n - camZ;
    if (z > 15) break;
    if (waveRows && (n & 1) === 1 && n < Math.floor(camZ) + 12) continue;
    const y = hor + K / z;
    if (y > H + 40) continue;
    const a = Math.max(0, (15 - z) / 13);
    ctx.globalAlpha = .9 * a; ctx.lineWidth = 1 + 1.3 * a;
    ctx.shadowBlur = 9 * a >= 1.5 ? 9 * a : 0;
    ctx.beginPath(); ctx.moveTo(-2, y); ctx.lineTo(W + 2, y); ctx.stroke();
  }
  // verticals — drawn far past the screen edges and faded by lateral
  // distance (upstream: the glow is carried by the wide pass alone)
  const R0 = cx * 1.15, R1 = cx * 6, B = W / 7;
  for (let i = Math.ceil(camX - R1 / B); i <= Math.floor(camX + R1 / B); i++){
    const dx = (i - camX) * B;
    const a = Math.min(1, (R1 - Math.abs(dx)) / (R1 - R0));
    if (a <= 0) continue;
    ctx.globalAlpha = .20 * a; ctx.lineWidth = 6; ctx.shadowBlur = 0;
    ctx.beginPath(); ctx.moveTo(cx, hor); ctx.lineTo(cx + dx, H + 2); ctx.stroke();
    ctx.globalAlpha = .85 * a; ctx.lineWidth = 1.4;
    ctx.beginPath(); ctx.moveTo(cx, hor); ctx.lineTo(cx + dx, H + 2); ctx.stroke();
  }
  ctx.globalAlpha = 1; ctx.shadowBlur = 0;
}

/* ============ road down the center (forward/backward motion) ============ */

function roadActive(){
  return state.road && (state.motion === "forward" || state.motion === "backward" ||
                        state.motion === "none");
}
function drawRoad(th, hor){
  const cx = W / 2, K = (H - hor) * 1.75, rw = 2 * (W / 7);
  ctx.beginPath();
  ctx.moveTo(cx, hor);
  ctx.lineTo(cx + rw, hor + K);
  ctx.lineTo(cx - rw, hor + K);
  ctx.closePath();
  ctx.fillStyle = grads(th).road;
  ctx.fill();
  ctx.strokeStyle = th.grid; ctx.shadowColor = th.grid;
  ctx.globalAlpha = .9; ctx.lineWidth = 2; ctx.shadowBlur = 8;
  ctx.beginPath();
  ctx.moveTo(cx, hor); ctx.lineTo(cx - rw, hor + K);
  ctx.moveTo(cx, hor); ctx.lineTo(cx + rw, hor + K);
  ctx.stroke();
  ctx.globalAlpha = 1; ctx.shadowBlur = 0;
}

/* ============ terrain mesh (Side Ranges / Deep Canyon), ported verbatim ============ */

function drawTerrain(T, th, hor){
  const cx = W / 2, K = (H - hor) * 1.75;
  const gap = T.gap, fade = T.fade, amp = T.amp * K, seed = T.seed;
  const zFar = 15;
  const hAt = (u, wz) => {
    const n = noise2(u * .24 + seed, wz * .24 + seed * 1.7) * .75 +
              noise2(u * .57 + seed * 2.3, wz * .57 + seed * 3.9) * .25;
    const h = Math.pow(n, 1.4) * amp;
    const m = Math.max(0, Math.min(1, (Math.abs(u - camX) - gap) / fade));
    return h * (m * m * (3 - 2 * m));
  };
  ctx.strokeStyle = th.grid; ctx.shadowColor = th.grid;
  const px = (u, z) => cx + (u - camX) * (W / 7) / z;
  const py = (h, z) => hor + (K - h) / z;
  const gf = grads(th).terrain;
  const span = cx * zFar / (W / 7) + 1;
  const cols = [];
  for (let i = Math.ceil((camX - span) * 2) / 2; i <= Math.floor((camX + span) * 2) / 2; i += .5)
    if (Math.abs(i - camX) >= gap) cols.push(i);
  cols.push(camX - gap, camX + gap);
  cols.sort((p, q) => p - q);
  const NROW = 28, r0 = Math.floor(camZ) + zFar - 1;
  const hbuf = new Float32Array(cols.length * NROW);
  for (let c = 0; c < cols.length; c++)
    for (let r = 0; r < NROW; r++)
      hbuf[c * NROW + r] = hAt(cols[c], r0 - r * .5);
  const hc = (c, n) => hbuf[c * NROW + Math.round((r0 - n) * 2)];
  const vis = [];
  for (let n = r0; n >= Math.floor(camZ) + 1; n -= .5){
    const z = n - camZ, ground = hor + K / z;
    const a = Math.max(0, (zFar - z) / (zFar - 2));
    if (a <= 0) continue;
    const runs = [];
    let run = [];
    const flush = () => {
      if (run.length > 1) runs.push(run);
      run = [];
    };
    for (let sx = 0; sx <= W + 5; sx += 5){
      const u = camX + (sx - cx) * z / (W / 7);
      if (Math.abs(u - camX) < gap){ flush(); continue; }
      const h = hAt(u, n);
      if (h / z < 2){ flush(); continue; }
      run.push([px(u, z), py(h, z)]);
    }
    flush();
    if (runs.length){
      ctx.globalAlpha = 1;
      ctx.fillStyle = gf;
      ctx.beginPath();
      for (const r of runs){
        ctx.moveTo(r[0][0], ground);
        for (const [x, y] of r) ctx.lineTo(x, y);
        ctx.lineTo(r[r.length - 1][0], ground);
        ctx.closePath();
      }
      ctx.fill();
      ctx.globalAlpha = .9 * a; ctx.lineWidth = 1.2;
      ctx.shadowBlur = 9 * a >= 1.5 ? 9 * a : 0;
      ctx.beginPath();
      for (const r of runs){
        ctx.moveTo(r[0][0], r[0][1]);
        for (const [x, y] of r) ctx.lineTo(x, y);
      }
      ctx.stroke();
    }
    const z2 = Math.max((n - .5) - camZ, .7);
    if (z2 >= .7){
      const ground2 = hor + K / z2;
      ctx.shadowBlur = 0;
      vis.length = 0;
      for (let c = 0; c < cols.length; c++){
        const x = px(cols[c], z);
        if (x > -40 && x < W + 40) vis.push(c);
      }
      ctx.globalAlpha = 1;
      ctx.fillStyle = gf;
      ctx.beginPath();
      for (const c of vis){
        const i = cols[c], h1 = hc(c, n), h2 = hc(c, n - .5);
        if (h1 / z < 2 && h2 / z2 < 2) continue;
        ctx.moveTo(px(i, z), py(h1, z));
        ctx.lineTo(px(i, z2), py(h2, z2));
        ctx.lineTo(px(i, z2), ground2);
        ctx.lineTo(px(i, z), ground);
        ctx.closePath();
      }
      ctx.fill();
      ctx.fillStyle = gf;
      ctx.beginPath();
      for (let v = 0; v < vis.length - 1; v++){
        const c = vis[v], cn = vis[v + 1];
        if (cn !== c + 1) continue;
        const u1 = cols[c], u2 = cols[cn];
        if ((u1 - camX) * (u2 - camX) < 0) continue;
        const h11 = hc(c, n), h12 = hc(cn, n);
        const h21 = hc(c, n - .5), h22 = hc(cn, n - .5);
        if (h11 < 2 * z && h12 < 2 * z && h21 < 2 * z2 && h22 < 2 * z2) continue;
        ctx.moveTo(px(u1, z), py(h11, z));
        ctx.lineTo(px(u2, z), py(h12, z));
        ctx.lineTo(px(u2, z2), py(h22, z2));
        ctx.lineTo(px(u1, z2), py(h21, z2));
        ctx.closePath();
      }
      ctx.fill();
      ctx.globalAlpha = .5 * a; ctx.lineWidth = 1;
      ctx.beginPath();
      for (const c of vis){
        const i = cols[c], h1 = hc(c, n), h2 = hc(c, n - .5);
        if (h1 / z < 2 && h2 / z2 < 2) continue;
        ctx.moveTo(px(i, z), py(h1, z));
        ctx.lineTo(px(i, z2), py(h2, z2));
      }
      ctx.stroke();
    }
  }
  ctx.globalAlpha = 1; ctx.shadowBlur = 0;
}

/* ============ spectrum: the scene pulses with your server ============
   Not audio — /api/metrics drives the bands (cpu + gpu load). Bars slew
   toward their target each tick (fast rise, slow fall) so 0.5 Hz sampling
   reads as a smooth swell, never a pulse (D37). */

const SPEC_N = 48;
const specBands = new Float32Array(SPEC_N);
const specWave = new Float32Array(2048);
let loadLevel = 0;          // 0..1, from the last metrics poll

function pollMetrics(){
  if (typeof window.TLCD_TOKEN !== "string") return;
  fetch("/api/metrics?token=" + encodeURIComponent(window.TLCD_TOKEN))
    .then(function (r){ return r.json(); })
    .then(function (doc){
      const d = doc.data || {};
      const cpu = (typeof d.cpu === "number") ? d.cpu / 100 : 0;
      const gpu = (d.gpu && typeof d.gpu.busy === "number") ? d.gpu.busy / 100 : 0;
      loadLevel = Math.max(cpu, gpu);
    })
    .catch(function (){});
}

function specCompute(t){
  if (!state.spectrum){
    for (let i = 0; i < SPEC_N; i++) specBands[i] *= .82;
    for (let i = 0; i < specWave.length; i++) specWave[i] *= .88;
    return;
  }
  const lvl = .08 + .92 * loadLevel;      // small idle floor: alive at rest
  for (let i = 0; i < SPEC_N; i++){
    const f = i / SPEC_N;
    // bass-weighted tilt + per-band wander that steps on the tick clock
    const target = Math.pow(1 - f, 1.7) * lvl *
                   (.75 + .5 * hash(i * 7.31 + Math.floor(t / 2) * 13.7));
    const cur = specBands[i];
    specBands[i] = cur + (target - cur) * (target > cur ? .65 : .35);
  }
  const env = .25 + .75 * lvl;
  for (let i = 0; i < specWave.length; i++){
    const v = Math.sin(i * .021 + t * 1.3) * .35 +
              Math.sin(i * .093 - t * 3.1) * .22 +
              (hash(i * 3.71 + Math.floor(t / 2) * 24) - .5) * .9;
    specWave[i] = Math.max(-1, Math.min(1, v * env));
  }
}

/* faded bar spectrum across the horizon — paints behind the sun */
function drawSpecHorizon(th, hor){
  const bw = W / SPEC_N;
  const g = ctx.createLinearGradient(0, hor - H * .34, 0, hor);
  g.addColorStop(0, hexA(th.grid, 0));
  g.addColorStop(1, hexA(th.grid, .38));
  ctx.fillStyle = g;
  for (let i = 0; i < SPEC_N; i++){
    const v = specBands[i];
    if (v < .015) continue;
    const h = v * H * .34;
    ctx.fillRect(i * bw + .5, hor - h, bw - 1.5, h);
  }
}

/* every other horizontal grid row carries the waveform */
function drawSpecGrid(th, hor){
  const K = (H - hor) * 1.75;
  ctx.strokeStyle = th.grid; ctx.shadowColor = th.grid;
  const zMax = 12, wn = specWave.length;
  for (let n = Math.floor(camZ) + 1; n < Math.floor(camZ) + zMax; n++){
    if ((n & 1) === 0) continue;
    const z = n - camZ;
    if (z < 1.05) continue;
    const y = hor + K / z;
    if (y > H + 40) continue;
    const a = Math.max(0, Math.min(1, (zMax - z) / (zMax - 3)));
    if (a <= 0) continue;
    const amp = Math.min(H * .17, H * .21 / z);
    const ph = (n * 251) % wn;
    ctx.globalAlpha = .18 * a; ctx.lineWidth = 5; ctx.shadowBlur = 0;
    strokeWaveRow(y, W, amp, ph, wn);
    ctx.globalAlpha = .9 * a; ctx.lineWidth = 1.4;
    ctx.shadowBlur = 9 * a >= 1.5 ? 9 * a : 0;
    strokeWaveRow(y, W, amp, ph, wn);
    ctx.shadowBlur = 0;
  }
  ctx.globalAlpha = 1;
}
function strokeWaveRow(y, W, amp, ph, wn){
  ctx.beginPath();
  for (let x = -2, k = 0; x <= W + 2; x += 5, k++){
    const yy = y - specWave[(ph + k * 3) % wn] * amp;
    if (x === -2) ctx.moveTo(x, yy); else ctx.lineTo(x, yy);
  }
  ctx.stroke();
}

/* ============ state + camera ============ */

let state = Object.assign({}, DEFAULTS);
let camX = 0, camZ = 0, clock = 0;

function motionLegal(scene, motion){
  const allowed = SCENES[scene].motions;
  if (allowed && allowed.indexOf(motion) < 0)
    return allowed.indexOf("forward") >= 0 ? "forward" : allowed[0];
  return motion;
}

/* ============ render frame (upstream layer order) ============ */

function render(){
  if (!ctx) return;                   // unmounted (enabled:false) — nothing paints
  const hor = H * .64, cx = W / 2;
  const th = THEMES[state.theme], sc = SCENES[state.scene];
  specCompute(clock);

  const GR = grads(th);
  ctx.fillStyle = GR.sky; ctx.fillRect(0, 0, W, hor + 1);

  if (state.stars) drawStars(th, hor, clock);
  if (state.spectrum) drawSpecHorizon(th, hor);
  for (const s of sc.sun) drawSun(s, th, hor, cx);

  ctx.fillStyle = GR.floor; ctx.fillRect(0, hor, W, H - hor);

  drawGrid(th, hor);
  if (state.spectrum) drawSpecGrid(th, hor);
  if (roadActive()) drawRoad(th, hor);

  ctx.strokeStyle = th.grid; ctx.shadowColor = th.grid;
  ctx.globalAlpha = .18; ctx.lineWidth = 5; ctx.shadowBlur = 16;
  ctx.beginPath(); ctx.moveTo(0, hor); ctx.lineTo(W, hor); ctx.stroke();
  ctx.globalAlpha = .95; ctx.lineWidth = 1.5;
  ctx.beginPath(); ctx.moveTo(0, hor); ctx.lineTo(W, hor); ctx.stroke();
  ctx.globalAlpha = 1; ctx.shadowBlur = 0;

  ctx.fillStyle = GR.haze; ctx.fillRect(0, hor, W, H * .16);

  if (sc.terrain) drawTerrain(sc.terrain, th, hor);

  sc.mts.forEach((L, i) => drawRidge(L, i === 0 ? th.mFar : th.mNear, hor, W / 7));
  (sc.sides || []).forEach((L, i) => drawRidge(L, i === 0 ? th.mFar : th.mNear, hor, W / 7));
  for (const L of (sc.city || [])) drawCity(L, th, hor, clock);

  ctx.fillStyle = GR.vig; ctx.fillRect(0, 0, W, H);
}

/* ============ platform-tick interface (D37) ============ */

function step(){
  // the deck's speed dial: 0-100, default 25; lateral and z keep the deck's
  // own rates, so speed 25 cruises exactly like the source project
  const v = Math.max(0, Math.min(100, Number(state.speed) || 0)) / 100;
  const m = state.motion;
  if (m === "left")          camX += v * CAM_SPEED * TICK_S;
  else if (m === "right")    camX -= v * CAM_SPEED * TICK_S;
  else if (m === "forward")  camZ += v * CAM_Z_SPEED * TICK_S;
  else if (m === "backward") camZ -= v * CAM_Z_SPEED * TICK_S;
  if (roadActive() && m !== "left" && m !== "right")
    camX = Math.round(camX);          // settle on a lane so road edges sit on grid lines
  clock += TICK_S;
  if (state.spectrum) pollMetrics();
  render();
}

/* ============ public API ============ */

function applyCfg(next){
  const merged = Object.assign({}, DEFAULTS, state, next || {});
  merged.scene = SCENES[merged.scene] ? merged.scene : DEFAULTS.scene;
  merged.theme = THEMES[merged.theme] ? merged.theme : DEFAULTS.theme;
  merged.motion = MOTION_LABELS[merged.motion] ? merged.motion : DEFAULTS.motion;
  merged.motion = motionLegal(merged.scene, merged.motion);
  merged.glass = Math.max(0, Math.min(1, Number(merged.glass)));
  if (isNaN(merged.glass)) merged.glass = DEFAULTS.glass;
  merged.speed = Math.max(0, Math.min(100, Number(merged.speed)));
  if (isNaN(merged.speed)) merged.speed = DEFAULTS.speed;
  // scanlines ride along as config: the overlay itself is a dashboard DOM
  // layer (not the scene canvas), so apply() only carries the flag
  ["spectrum", "stars", "road", "scanlines", "enabled"].forEach(function (k){
    merged[k] = !!merged[k];
  });
  state = merged;
}

let liveCanvas = null;

/* hot-swap the running scene (editor preview pushes); a push that enables
   the scene in a page booted without it mounts on demand */
function apply(next){
  applyCfg(next);
  if (!liveCanvas && state.enabled !== false){
    const el = document.getElementById("flair-scene");
    if (el) mount(el, null);
  }
  if (liveCanvas) render();
}

/* run the live scene on a canvas; the dashboard calls step() per platform tick */
function mount(canvas, cfg){
  liveCanvas = canvas;
  bindCanvas(canvas);
  applyCfg(cfg);
  render();
}

/* static thumbnail for the editor picker: render straight into the thumb
   canvas at its own resolution (the engine scales with W/H), deterministic
   camera, no spectrum bars */
function paintThumb(canvas, cfg){
  const saveState = state, saveX = camX, saveZ = camZ, saveClock = clock;
  const saveLive = liveCanvas;
  liveCanvas = null;
  bindCanvas(canvas);
  applyCfg(Object.assign({}, cfg, {spectrum: false}));
  camX = 0; camZ = 0; clock = 0;
  render();
  state = saveState; camX = saveX; camZ = saveZ; clock = saveClock;
  liveCanvas = saveLive;
}

window.TLCDFlair = {
  mount: mount, apply: apply, step: step, paintThumb: paintThumb,
  SCENES: SCENES, THEMES: THEMES, MOTIONS: MOTION_LABELS, DEFAULTS: DEFAULTS,
};

/* auto-mount: the dashboard page ships #flair-scene + an injected config;
   enabled:false (the editor's "background scene" checkbox) skips the mount */
if (window.TLCD_FLAIR && window.TLCD_FLAIR.enabled !== false){
  const el = document.getElementById("flair-scene");
  if (el) mount(el, window.TLCD_FLAIR);
}
})();
