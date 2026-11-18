// Pure layout maths for the VMS layout tool. No DOM, no network.
//
// Coordinates follow OMNIA: 1-based and inclusive, so a 272x96 sign spans
// x 1..272 and y 1..96, and a box's width is x2 - x1 + 1.
// Margin and gap values are counts of EMPTY pixels.
//
// Functions that take `sel` / `boxes` mutate the box objects in place; the UI
// snapshots state before calling them so everything is undoable.
const Geo = (function () {
  const bw = (b) => b.x2 - b.x1 + 1;
  const bh = (b) => b.y2 - b.y1 + 1;
  const AX = {
    h: { lo: 'x1', hi: 'x2', size: bw },
    v: { lo: 'y1', hi: 'y2', size: bh },
  };

  function bbox(bs) {
    return {
      x1: Math.min(...bs.map((b) => b.x1)),
      y1: Math.min(...bs.map((b) => b.y1)),
      x2: Math.max(...bs.map((b) => b.x2)),
      y2: Math.max(...bs.map((b) => b.y2)),
    };
  }

  function shift(b, dx, dy) {
    b.x1 += dx; b.x2 += dx; b.y1 += dy; b.y2 += dy;
  }

  // mode: left | right | top | bottom | hcentre | vcentre (relative to the selection)
  function align(sel, mode) {
    if (sel.length < 2) return;
    const bb = bbox(sel);
    for (const b of sel) {
      if (mode === 'left') shift(b, bb.x1 - b.x1, 0);
      else if (mode === 'right') shift(b, bb.x2 - b.x2, 0);
      else if (mode === 'top') shift(b, 0, bb.y1 - b.y1);
      else if (mode === 'bottom') shift(b, 0, bb.y2 - b.y2);
      else if (mode === 'hcentre') shift(b, Math.floor((bb.x1 + bb.x2 - (bw(b) - 1)) / 2) - b.x1, 0);
      else if (mode === 'vcentre') shift(b, 0, Math.floor((bb.y1 + bb.y2 - (bh(b) - 1)) / 2) - b.y1);
    }
  }

  // Centre the selection's bounding box in the sign so both margins are equal
  // (to within 1px when the leftover is odd).
  function centreInSign(sel, axis, W, H) {
    if (!sel.length) return;
    const bb = bbox(sel);
    if (axis === 'h') {
      const free = W - (bb.x2 - bb.x1 + 1);
      const dx = 1 + Math.floor(free / 2) - bb.x1;
      sel.forEach((b) => shift(b, dx, 0));
    } else {
      const free = H - (bb.y2 - bb.y1 + 1);
      const dy = 1 + Math.floor(free / 2) - bb.y1;
      sel.forEach((b) => shift(b, 0, dy));
    }
  }

  // Equal gaps between the boxes. gap == null keeps the first and last boxes
  // where they are and spaces the rest evenly; a number sets a fixed gap
  // starting from the first box. Spare pixels go to the middle gaps first so
  // the result stays as symmetrical as integers allow.
  function distribute(sel, axis, gap) {
    const a = AX[axis];
    const s = sel.slice().sort((p, q) => p[a.lo] - q[a.lo]);
    const n = s.length;
    if (n < 2) return;
    let gaps;
    if (gap == null) {
      if (n < 3) return;
      const span = s[n - 1][a.hi] - s[0][a.lo] + 1;
      const free = span - s.reduce((t, b) => t + a.size(b), 0);
      if (free < 0) return;
      const base = Math.floor(free / (n - 1));
      let rem = free - base * (n - 1);
      gaps = new Array(n - 1).fill(base);
      const order = gaps.map((_, i) => i).sort((p, q) => Math.abs(p - (n - 2) / 2) - Math.abs(q - (n - 2) / 2));
      for (const i of order) { if (rem <= 0) break; gaps[i] += 1; rem -= 1; }
    } else {
      gaps = new Array(n - 1).fill(gap);
    }
    let cursor = s[0][a.lo];
    s.forEach((b, i) => {
      const size = a.size(b);
      b[a.lo] = cursor;
      b[a.hi] = cursor + size - 1;
      cursor = b[a.hi] + 1 + (gaps[i] || 0);
    });
  }

  // dim: w | h | both — match every box to the first one in the list.
  function sameSize(sel, dim) {
    if (sel.length < 2) return;
    const ref = sel[0];
    for (const b of sel.slice(1)) {
      if (dim === 'w' || dim === 'both') b.x2 = b.x1 + bw(ref) - 1;
      if (dim === 'h' || dim === 'both') b.y2 = b.y1 + bh(ref) - 1;
    }
  }

  // Flip across the sign centre. axis: h (left<->right) | v (top<->bottom)
  function mirror(boxes, W, H, axis) {
    for (const b of boxes) {
      if (axis === 'h') { const x1 = W + 1 - b.x2; const x2 = W + 1 - b.x1; b.x1 = x1; b.x2 = x2; }
      else { const y1 = H + 1 - b.y2; const y2 = H + 1 - b.y1; b.y1 = y1; b.y2 = y2; }
    }
  }

  // Build a starting layout.
  // o: { W, H, margin, gap, rows, picto: none|left|right, pictoSize (null = auto square) }
  function preset(o) {
    const { W, H, margin: m, gap: g } = o;
    const rows = Math.max(0, o.rows | 0);
    const out = [];
    const areaX1 = m + 1, areaX2 = W - m, areaY1 = m + 1, areaY2 = H - m;
    const areaW = areaX2 - areaX1 + 1, areaH = areaY2 - areaY1 + 1;
    let tx1 = areaX1, tx2 = areaX2;
    if (o.picto === 'left' || o.picto === 'right') {
      const P = Math.max(1, Math.min(o.pictoSize || areaH, areaW));
      const py1 = areaY1 + Math.floor((areaH - Math.min(P, areaH)) / 2);
      if (o.picto === 'left') {
        out.push({ type: 'Graphic', x1: areaX1, y1: py1, x2: areaX1 + P - 1, y2: py1 + Math.min(P, areaH) - 1 });
        tx1 = areaX1 + P + g;
      } else {
        out.push({ type: 'Graphic', x1: areaX2 - P + 1, y1: py1, x2: areaX2, y2: py1 + Math.min(P, areaH) - 1 });
        tx2 = areaX2 - P - g;
      }
    }
    if (rows > 0 && tx2 > tx1) {
      const rowH = Math.max(1, Math.floor((areaH - (rows - 1) * g) / rows));
      const used = rows * rowH + (rows - 1) * g;
      const top = areaY1 + Math.floor((areaH - used) / 2);
      for (let i = 0; i < rows; i++) {
        const y1 = top + i * (rowH + g);
        out.push({ type: 'Text', x1: tx1, y1, x2: tx2, y2: y1 + rowH - 1 });
      }
    }
    return out;
  }

  const overlap = (p, q) => p.x1 <= q.x2 && q.x1 <= p.x2 && p.y1 <= q.y2 && q.y1 <= p.y2;

  // Group boxes into columns (boxes whose x-ranges overlap mostly).
  function columns(boxes) {
    const parent = boxes.map((_, i) => i);
    const find = (i) => (parent[i] === i ? i : (parent[i] = find(parent[i])));
    for (let i = 0; i < boxes.length; i++) {
      for (let j = i + 1; j < boxes.length; j++) {
        const lo = Math.max(boxes[i].x1, boxes[j].x1);
        const hi = Math.min(boxes[i].x2, boxes[j].x2);
        const ov = hi - lo + 1;
        if (ov > 0 && ov >= 0.5 * Math.min(bw(boxes[i]), bw(boxes[j]))) parent[find(i)] = find(j);
      }
    }
    const groups = new Map();
    boxes.forEach((b, i) => {
      const r = find(i);
      if (!groups.has(r)) groups.set(r, []);
      groups.get(r).push(b);
    });
    return [...groups.values()].map((c) => c.sort((p, q) => p.y1 - q.y1));
  }

  // Report symmetry/consistency problems. Each: { key, ok, msg, fix }.
  function analyse(boxes, W, H) {
    const out = [];
    if (!boxes.length) return out;
    const bb = bbox(boxes);
    const l = bb.x1 - 1, r = W - bb.x2, t = bb.y1 - 1, bo = H - bb.y2;
    out.push({ key: 'h', ok: Math.abs(l - r) <= 1, msg: `Left margin ${l}px, right ${r}px`, fix: 'centreH' });
    out.push({ key: 'v', ok: Math.abs(t - bo) <= 1, msg: `Top margin ${t}px, bottom ${bo}px`, fix: 'centreV' });
    columns(boxes).forEach((col, ci) => {
      if (col.length < 2) return;
      const gaps = col.slice(1).map((b, i) => b.y1 - col[i].y2 - 1);
      const hs = col.map(bh);
      out.push({ key: 'gaps' + ci, ok: Math.max(...gaps) - Math.min(...gaps) <= 0, msg: `Row gaps ${gaps.join(', ')}px`, fix: 'tidy' });
      out.push({ key: 'hts' + ci, ok: Math.max(...hs) === Math.min(...hs), msg: `Row heights ${hs.join(', ')}px`, fix: 'tidy' });
      const aligned = col.every((b) => b.x1 === col[0].x1 && b.x2 === col[0].x2);
      out.push({ key: 'x' + ci, ok: aligned, msg: aligned ? 'Rows share left and right edges' : 'Rows have different left/right edges', fix: 'tidy' });
    });
    const clash = [];
    for (let i = 0; i < boxes.length; i++) for (let j = i + 1; j < boxes.length; j++) if (overlap(boxes[i], boxes[j])) clash.push(`${i + 1}&${j + 1}`);
    if (clash.length) out.push({ key: 'overlap', ok: false, msg: `Overlapping boxes: ${clash.join(', ')} (OMNIA may reject this)`, fix: null });
    const oob = boxes.some((b) => b.x1 < 1 || b.y1 < 1 || b.x2 > W || b.y2 > H || b.x2 < b.x1 || b.y2 < b.y1);
    if (oob) out.push({ key: 'bounds', ok: false, msg: 'A box is outside the sign or has no size', fix: null });
    return out;
  }

  const modeOf = (arr) => {
    const c = new Map();
    arr.forEach((v) => c.set(v, (c.get(v) || 0) + 1));
    return [...c.entries()].sort((p, q) => q[1] - p[1] || q[0] - p[0])[0][0];
  };

  // One-click clean-up: equal edges per column, equal heights, equal gaps,
  // then centre the whole layout in the sign.
  function tidy(boxes, W, H) {
    if (!boxes.length) return;
    for (const col of columns(boxes)) {
      if (col.length < 2) continue;
      // Use the most common edges so one stray box doesn't widen the column.
      const x1 = modeOf(col.map((b) => b.x1));
      const x2 = modeOf(col.map((b) => b.x2));
      col.forEach((b) => { b.x1 = x1; b.x2 = x2; });
      const h = modeOf(col.map(bh));
      col.forEach((b) => { b.y2 = b.y1 + h - 1; });
      const gaps = col.slice(1).map((b, i) => b.y1 - col[i].y2 - 1);
      const gap = Math.max(0, Math.round(gaps.reduce((p, q) => p + q, 0) / gaps.length));
      distribute(col, 'v', gap);
    }
    centreInSign(boxes, 'h', W, H);
    centreInSign(boxes, 'v', W, H);
  }

  const api = { bw, bh, bbox, shift, align, centreInSign, distribute, sameSize, mirror, preset, columns, analyse, tidy, overlap };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  return api;
})();
