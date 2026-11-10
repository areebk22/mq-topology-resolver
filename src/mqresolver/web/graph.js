// Canvas renderer for topology graphs. No dependencies.
//
// Why it stays fast with thousands of nodes:
//  - positions come precomputed from the server, so the browser never runs a layout;
//  - it draws only when something changes (one requestAnimationFrame per change);
//  - edges and nodes are batched into a handful of Path2D objects per frame
//    (one per colour and width class) instead of one draw call per element;
//  - off-screen elements are skipped, labels are capped, and very large edge sets
//    are thinned while the user is panning or zooming.

const TAU = Math.PI * 2;
const WIDTHS = [0.5, 0.9, 1.5, 2.4];
const MAX_LABELS = 140;
const THIN_EDGES_ABOVE = 12000;
const THIN_TO = 5000;

export class GraphView {
  constructor(canvas, { onSelect, onHover, onCamera } = {}) {
    this.c = canvas;
    this.ctx = canvas.getContext('2d', { alpha: false });
    this.onSelect = onSelect || (() => {});
    this.onHover = onHover || (() => {});
    this.onCamera = onCamera || (() => {});
    this.nodes = [];
    this.edges = [];
    this.spokes = [];
    this.cam = { s: 1, x: 0, y: 0 };
    this.fit = 1;
    this.hl = null;
    this.selected = -1;
    this.hover = -1;
    this.w = 1; this.h = 1; this.dpr = 1;
    this.pending = false;
    this.interactingUntil = 0;
    this.perf = { last: 0, max: 0, frames: 0 };
    this.palette = { bg: '#fff', ink: '#000', edge: '#999', spoke: '#bbb', accent: '#e8a317', halo: '#fff', dim: '#ddd' };
    this._pointers = new Map();
    this._bind();
    new ResizeObserver(() => this._resize()).observe(canvas.parentElement);
    this._resize();
  }

  // ---- data -----------------------------------------------------------------
  // nodes: [{id, x, y, r, fill, shape?, hollow?, ring?, label?}] in world units (0..1000)
  // edges: [{s, t, w}] with node indices; spokes: [[a, b], ...] faint connector lines
  setData(nodes, edges, spokes = [], { keepCamera = false } = {}) {
    this.nodes = nodes;
    this.edges = edges;
    this.spokes = spokes;
    const n = nodes.length;
    this.sx = new Float32Array(n);
    this.sy = new Float32Array(n);
    this.sr = new Float32Array(n);
    this.index = new Map(nodes.map((d, i) => [d.id, i]));
    this.order = [...nodes.keys()].sort((a, b) => nodes[b].r - nodes[a].r);
    const wmax = edges.reduce((m, e) => Math.max(m, e.w), 1);
    const lw = Math.log1p(wmax);
    for (const e of edges) e.k = Math.min(3, Math.floor((4 * Math.log1p(e.w)) / (lw + 1e-9)));
    this.byWeight = [...edges.keys()].sort((a, b) => edges[b].w - edges[a].w).slice(0, THIN_TO);
    this.selected = -1;
    this.hover = -1;
    this.hl = null;
    if (!keepCamera) this.fitView();
    this.request();
  }

  setPalette(p) { Object.assign(this.palette, p); this.request(); }

  // nodes: Set of indices to keep bright; focus: Set of indices whose edges light up
  setHighlight(nodes, focus) {
    this.hl = nodes ? { nodes, focus: focus || nodes } : null;
    this.request();
  }

  select(i) { this.selected = i; this.request(); }

  // ---- camera ---------------------------------------------------------------
  bounds() {
    let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
    for (const d of this.nodes) {
      if (d.x < x0) x0 = d.x; if (d.x > x1) x1 = d.x;
      if (d.y < y0) y0 = d.y; if (d.y > y1) y1 = d.y;
    }
    return isFinite(x0) ? [x0, y0, x1, y1] : [0, 0, 1000, 1000];
  }

  // room around the drawing: the biggest node radius, plus space on the right for labels
  _pads() {
    const r = this.nodes.reduce((m, d) => Math.max(m, d.r), 0);
    const pad = Math.min(16 + r * 1.3, Math.min(this.w, this.h) / 4);
    return [pad, pad + Math.min(58, this.w / 8)];
  }

  fitView() {
    const [x0, y0, x1, y1] = this.bounds();
    const [pad, right] = this._pads();
    const bw = Math.max(x1 - x0, 1), bh = Math.max(y1 - y0, 1);
    const s = Math.min((this.w - pad - right) / bw, (this.h - 2 * pad) / bh);
    this.fit = s > 0 ? s : 1;
    this.cam = { s: this.fit, x: pad + ((this.w - pad - right) - bw * this.fit) / 2 - x0 * this.fit,
                 y: this.h / 2 - ((y0 + y1) / 2) * this.fit };
    this._cameraChanged(false);
  }

  zoomAt(px, py, factor) {
    const s = Math.min(this.fit * 60, Math.max(this.fit * 0.4, this.cam.s * factor));
    const k = s / this.cam.s;
    this.cam = { s, x: px - (px - this.cam.x) * k, y: py - (py - this.cam.y) * k };
    this._cameraChanged(true);
  }

  focusNode(i, zoom = 3) {
    if (i < 0) return;
    const d = this.nodes[i];
    const s = Math.max(this.cam.s, this.fit * zoom);
    this.cam = { s, x: this.w / 2 - d.x * s, y: this.h / 2 - d.y * s };
    this._cameraChanged(false);
  }

  // copy another view's camera, relative to each view's own fit
  followCamera(other) {
    const cx = (other.w / 2 - other.cam.x) / other.cam.s;
    const cy = (other.h / 2 - other.cam.y) / other.cam.s;
    const s = (other.cam.s / other.fit) * this.fit;
    this.cam = { s, x: this.w / 2 - cx * s, y: this.h / 2 - cy * s };
    this.interactingUntil = other.interactingUntil;
    this.request();
  }

  _cameraChanged(interactive) {
    if (interactive) {
      this.interactingUntil = performance.now() + 160;
      clearTimeout(this._idle);
      this._idle = setTimeout(() => this.request(), 180);
    }
    this.request();
    this.onCamera(this);
  }

  // ---- drawing --------------------------------------------------------------
  request() {
    if (this.pending) return;
    this.pending = true;
    requestAnimationFrame(() => { this.pending = false; this.draw(); });
  }

  draw() {
    const t0 = performance.now();
    const { ctx, palette: P } = this;
    const W = this.w, H = this.h;
    ctx.setTransform(this.dpr, 0, 0, this.dpr, 0, 0);
    ctx.fillStyle = P.bg;
    ctx.fillRect(0, 0, W, H);
    const n = this.nodes.length;
    if (!n) return;
    const { s, x: ox, y: oy } = this.cam;
    const zf = Math.min(6, Math.max(0.6, Math.sqrt(s / this.fit)));
    const sx = this.sx, sy = this.sy, sr = this.sr;
    for (let i = 0; i < n; i++) {
      const d = this.nodes[i];
      sx[i] = d.x * s + ox;
      sy[i] = d.y * s + oy;
      sr[i] = d.r * zf;
    }
    const hl = this.hl;
    const off = (i) => sx[i] < -30 || sy[i] < -30 || sx[i] > W + 30 || sy[i] > H + 30;

    // spokes (app -> queue manager connectors)
    if (this.spokes.length) {
      const p = new Path2D();
      for (const [a, b] of this.spokes) {
        if (off(a) && off(b)) continue;
        if (hl && !(hl.nodes.has(a) && hl.nodes.has(b))) continue;
        p.moveTo(sx[a], sy[a]); p.lineTo(sx[b], sy[b]);
      }
      ctx.strokeStyle = P.spoke; ctx.globalAlpha = hl ? 0.9 : 0.55; ctx.lineWidth = 0.6;
      ctx.stroke(p);
    }

    // edges, batched by width class and highlight state
    const thin = this.edges.length > THIN_EDGES_ABOVE && performance.now() < this.interactingUntil;
    const list = thin ? this.byWeight : null;
    const norm = [new Path2D(), new Path2D(), new Path2D(), new Path2D()];
    const lit = [new Path2D(), new Path2D(), new Path2D(), new Path2D()];
    const m = list ? list.length : this.edges.length;
    for (let j = 0; j < m; j++) {
      const e = this.edges[list ? list[j] : j];
      const a = e.s, b = e.t;
      if ((sx[a] < 0 && sx[b] < 0) || (sx[a] > W && sx[b] > W) || (sy[a] < 0 && sy[b] < 0) || (sy[a] > H && sy[b] > H)) continue;
      const on = hl && ((hl.focus.has(a) && hl.nodes.has(b)) || (hl.focus.has(b) && hl.nodes.has(a)));
      const p = on ? lit[e.k] : norm[e.k];
      p.moveTo(sx[a], sy[a]); p.lineTo(sx[b], sy[b]);
    }
    const wz = Math.min(2, Math.sqrt(zf));
    ctx.strokeStyle = P.edge;
    ctx.globalAlpha = hl ? 0.1 : 0.32;
    for (let k = 0; k < 4; k++) { ctx.lineWidth = WIDTHS[k] * wz; ctx.stroke(norm[k]); }
    if (hl) {
      ctx.strokeStyle = P.accent; ctx.globalAlpha = 0.9;
      for (let k = 0; k < 4; k++) { ctx.lineWidth = (WIDTHS[k] + 0.6) * wz; ctx.stroke(lit[k]); }
    }
    ctx.globalAlpha = 1;

    // nodes, batched by fill colour
    const fills = new Map();
    const hollow = new Map();
    const rings = new Path2D();
    const shape = (p, i, d) => {
      const x = sx[i], y = sy[i], r = Math.max(sr[i], 1.2);
      if (d.shape === 'diamond') {
        p.moveTo(x, y - r * 1.3); p.lineTo(x + r * 1.3, y); p.lineTo(x, y + r * 1.3); p.lineTo(x - r * 1.3, y); p.closePath();
      } else { p.moveTo(x + r, y); p.arc(x, y, r, 0, TAU); }
    };
    for (let i = 0; i < n; i++) {
      if (off(i)) continue;
      const d = this.nodes[i];
      const dim = hl && !hl.nodes.has(i);
      const key = dim ? P.dim : d.fill;
      const bucket = d.hollow ? hollow : fills;
      if (!bucket.has(key)) bucket.set(key, new Path2D());
      shape(bucket.get(key), i, d);
      if (d.ring && !dim) { const r = sr[i] + 1.6; rings.moveTo(sx[i] + r, sy[i]); rings.arc(sx[i], sy[i], r, 0, TAU); }
    }
    for (const [col, p] of hollow) { ctx.strokeStyle = col; ctx.lineWidth = 1.4; ctx.stroke(p); }
    for (const [col, p] of fills) { ctx.fillStyle = col; ctx.fill(p); }
    ctx.strokeStyle = P.ink; ctx.lineWidth = 1.1; ctx.stroke(rings);

    // selection and hover
    for (const [i, w] of [[this.hover, 1.5], [this.selected, 2.5]]) {
      if (i < 0 || i >= n) continue;
      const p = new Path2D(); const r = Math.max(sr[i], 3) + 3;
      p.moveTo(sx[i] + r, sy[i]); p.arc(sx[i], sy[i], r, 0, TAU);
      ctx.strokeStyle = P.accent; ctx.lineWidth = w; ctx.stroke(p);
    }

    // labels: biggest first, capped, always for the selection and highlighted nodes
    ctx.font = '11px ui-monospace, SFMono-Regular, Menlo, Consolas, monospace';
    ctx.textBaseline = 'middle';
    ctx.lineJoin = 'round';
    let shown = 0;
    const boxes = [];
    const free = (x0, y0, x1, y1) => {
      for (const b of boxes) if (x0 < b[2] && x1 > b[0] && y0 < b[3] && y1 > b[1]) return false;
      return true;
    };
    const want = (i) => i === this.selected || (hl ? hl.focus.has(i) || (hl.nodes.size <= 40 && hl.nodes.has(i)) : sr[i] >= 7);
    const label = (i, force = false) => {
      const d = this.nodes[i];
      if (!d.label || off(i)) return;
      const x = sx[i] + Math.max(sr[i], 3) + 4, y = sy[i];
      const tw = ctx.measureText(d.label).width;
      if (!force && !free(x - 1, y - 7, x + tw + 1, y + 7)) return;
      boxes.push([x - 1, y - 7, x + tw + 1, y + 7]);
      ctx.strokeStyle = P.halo; ctx.lineWidth = 3; ctx.strokeText(d.label, x, y);
      ctx.fillStyle = P.ink; ctx.fillText(d.label, x, y);
      shown++;
    };
    if (this.selected >= 0) label(this.selected, true);
    for (const i of this.order) {
      if (shown >= MAX_LABELS) break;
      if (i !== this.selected && want(i)) label(i);
    }

    const ms = performance.now() - t0;
    this.perf.last = ms; this.perf.frames++; this.perf.max = Math.max(this.perf.max, ms);
  }

  // ---- interaction ------------------------------------------------------------
  hit(px, py) {
    let best = -1, bd = Infinity;
    for (let i = 0; i < this.nodes.length; i++) {
      const dx = px - this.sx[i], dy = py - this.sy[i];
      const r = Math.max(this.sr[i], 4) + 3;
      const dd = dx * dx + dy * dy;
      if (dd <= r * r && dd < bd) { bd = dd; best = i; }
    }
    return best;
  }

  _local(ev) {
    const r = this.c.getBoundingClientRect();
    return [ev.clientX - r.left, ev.clientY - r.top];
  }

  _bind() {
    const c = this.c;
    c.addEventListener('pointerdown', (ev) => {
      c.setPointerCapture(ev.pointerId);
      const [x, y] = this._local(ev);
      this._pointers.set(ev.pointerId, { x, y, x0: x, y0: y });
      this._drag = false;
    });
    c.addEventListener('pointermove', (ev) => {
      const [x, y] = this._local(ev);
      const pt = this._pointers.get(ev.pointerId);
      if (!pt) {
        const i = this.hit(x, y);
        if (i !== this.hover) { this.hover = i; this.request(); }
        this.onHover(i >= 0 ? this.nodes[i] : null, ev.clientX, ev.clientY);
        c.style.cursor = i >= 0 ? 'pointer' : 'grab';
        return;
      }
      if (this._pointers.size === 2) {                    // pinch zoom
        const [a, b] = [...this._pointers.values()];
        const before = Math.hypot(a.x - b.x, a.y - b.y);
        pt.x = x; pt.y = y;
        const after = Math.hypot(a.x - b.x, a.y - b.y);
        if (before > 0) this.zoomAt((a.x + b.x) / 2, (a.y + b.y) / 2, after / before);
        this._drag = true;
        return;
      }
      const dx = x - pt.x, dy = y - pt.y;
      pt.x = x; pt.y = y;
      if (!this._drag && Math.hypot(x - pt.x0, y - pt.y0) < 4) return;
      this._drag = true;
      c.style.cursor = 'grabbing';
      this.cam.x += dx; this.cam.y += dy;
      this.onHover(null);
      this._cameraChanged(true);
    });
    const up = (ev) => {
      const pt = this._pointers.get(ev.pointerId);
      this._pointers.delete(ev.pointerId);
      if (pt && !this._drag && ev.type === 'pointerup') {
        const [x, y] = this._local(ev);
        const i = this.hit(x, y);
        this.onSelect(i >= 0 ? this.nodes[i] : null, i);
      }
      c.style.cursor = 'grab';
    };
    c.addEventListener('pointerup', up);
    c.addEventListener('pointercancel', up);
    c.addEventListener('pointerleave', () => { if (this.hover >= 0) { this.hover = -1; this.request(); } this.onHover(null); });
    c.addEventListener('wheel', (ev) => {
      ev.preventDefault();
      const [x, y] = this._local(ev);
      const dy = ev.deltaMode === 1 ? ev.deltaY * 16 : ev.deltaY;
      this.zoomAt(x, y, Math.exp(-dy * 0.0016));
    }, { passive: false });
    c.addEventListener('dblclick', (ev) => { const [x, y] = this._local(ev); this.zoomAt(x, y, 2); });
    c.addEventListener('keydown', (ev) => {
      const step = 60;
      const k = ev.key;
      if (k === '+' || k === '=') this.zoomAt(this.w / 2, this.h / 2, 1.3);
      else if (k === '-') this.zoomAt(this.w / 2, this.h / 2, 1 / 1.3);
      else if (k === '0') this.fitView();
      else if (k === 'ArrowLeft') { this.cam.x += step; this._cameraChanged(true); }
      else if (k === 'ArrowRight') { this.cam.x -= step; this._cameraChanged(true); }
      else if (k === 'ArrowUp') { this.cam.y += step; this._cameraChanged(true); }
      else if (k === 'ArrowDown') { this.cam.y -= step; this._cameraChanged(true); }
      else if (k === 'Escape') this.onSelect(null, -1);
      else return;
      ev.preventDefault();
    });
  }

  _resize() {
    const box = this.c.parentElement.getBoundingClientRect();
    const w = Math.max(1, Math.floor(box.width)), h = Math.max(1, Math.floor(box.height));
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    if (w === this.w && h === this.h && dpr === this.dpr) return;
    const cx = (this.w / 2 - this.cam.x) / this.cam.s, cy = (this.h / 2 - this.cam.y) / this.cam.s;
    const had = this.w > 1;
    this.w = w; this.h = h; this.dpr = dpr;
    this.c.width = w * dpr; this.c.height = h * dpr;
    this.c.style.width = w + 'px'; this.c.style.height = h + 'px';
    if (!had || !this.nodes.length) { this.fitView(); return; }
    const old = this.fit;
    const [x0, y0, x1, y1] = this.bounds();
    const [pad, right] = this._pads();
    this.fit = Math.min((w - pad - right) / Math.max(x1 - x0, 1), (h - 2 * pad) / Math.max(y1 - y0, 1)) || old;
    this.cam.s *= this.fit / old;
    this.cam.x = w / 2 - cx * this.cam.s; this.cam.y = h / 2 - cy * this.cam.s;
    this.request();
  }
}
