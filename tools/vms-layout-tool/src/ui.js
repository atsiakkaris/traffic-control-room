// Layout editor overlay. Depends on Geo (geometry.js) and Api (api.js).
(function () {
  if (window.__vlt) { window.__vlt.open(); return; }

  // ---------- state ----------
  const SETTINGS_KEY = 'vlt.settings.v1';
  const defaults = { margin: 5, gap: 3, grid: 1, snap: true, preview: true, rows: 4, picto: 'left', theme: 'dark' };
  const loadSettings = () => { try { return Object.assign({}, defaults, JSON.parse(localStorage.getItem(SETTINGS_KEY) || '{}')); } catch (e) { return Object.assign({}, defaults); } };
  const saveSettings = () => { try { localStorage.setItem(SETTINGS_KEY, JSON.stringify(S.set)); } catch (e) { /* ignore */ } };

  const S = {
    styles: [], style: null, layouts: [], layoutId: null, name: 'TEST_', asStyle: false,
    boxes: [], sel: [], links: [], undo: [], redo: [], uid: 1, scale: 3, guides: { x: [], y: [] },
    set: loadSettings(), busy: false, drag: null, dry: false,
  };

  // ---------- helpers ----------
  const el = (tag, attrs, ...kids) => {
    const n = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (k === 'class') n.className = v;
      else if (k === 'text') n.textContent = v;
      else if (k.startsWith('on')) n.addEventListener(k.slice(2), v);
      else if (v === true) n.setAttribute(k, '');
      else if (v !== false && v != null) n.setAttribute(k, v);
    }
    kids.flat().forEach((c) => c != null && n.append(c));
    return n;
  };
  const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
  // Link groups: each group of linked text boxes gets a letter and a colour.
  const LINK_COLORS = ['#ff6b81', '#5fd068', '#6c8cff', '#ffb347', '#c77dff', '#4fd8ee', '#f06ff0', '#c5f04a'];
  const groupOf = (uid) => S.links.findIndex((g) => g.includes(uid));
  const groupLetter = (i) => String.fromCharCode(65 + (i % 26));
  const groupColor = (i) => LINK_COLORS[i % LINK_COLORS.length];
  const boxNo = (uid) => S.boxes.findIndex((b) => b.uid === uid) + 1;
  const partnersOf = (uid) => { const g = S.links[groupOf(uid)]; return g ? g.filter((u) => u !== uid) : []; };
  const byUid = (id) => S.boxes.find((b) => b.uid === id);
  const selBoxes = () => S.sel.map(byUid).filter(Boolean);
  const snapshot = () => JSON.stringify({ boxes: S.boxes, links: S.links });
  function pushUndo() { S.undo.push(snapshot()); if (S.undo.length > 100) S.undo.shift(); S.redo = []; }
  function restore(s) { const o = JSON.parse(s); S.boxes = o.boxes; S.links = o.links; S.sel = S.sel.filter((id) => byUid(id)); }
  const undo = () => { if (!S.undo.length) return; S.redo.push(snapshot()); restore(S.undo.pop()); refresh(); };
  const redo = () => { if (!S.redo.length) return; S.undo.push(snapshot()); restore(S.redo.pop()); refresh(); };
  const newBox = (spec) => Object.assign({ uid: S.uid++, vid: null, text: spec.type === 'Text' ? 'SAMPLE TEXT' : '', align: 'center', raw: null }, spec);
  const edit = (fn) => { pushUndo(); fn(); refresh(); };
  function say(msg) { ui.status.textContent = msg; }

  // ---------- DOM ----------
  const host = el('div', { id: 'vlt-host' });
  host.style.cssText = 'all:initial;position:fixed;inset:0;z-index:2147483647;';
  const root = host.attachShadow({ mode: 'open' });
  root.append(el('style', { text: `
    :host{all:initial}
    .wrap{--bg:#10151bf2;--fg:#e7ecf1;--panel:#18202a;--line:#2b3643;--muted:#9fb0c0;--input:#0e1318;--inputline:#33414f;--btn:#26323f;--btnline:#3a4a5a;--btnhover:#31414f;--faint:#7f93a6;--pre:#b8c7d4;--selrow:#2d4558;--okc:#52d49a;--badc:#ffb454}
    .wrap.light{--bg:#eef2f6f7;--fg:#17202a;--panel:#ffffff;--line:#cdd6df;--muted:#52606d;--input:#ffffff;--inputline:#aab6c2;--btn:#e6ecf2;--btnline:#b5c1cd;--btnhover:#d8e1ea;--faint:#617080;--pre:#2e3d4b;--selrow:#cfe3f3;--okc:#14804f;--badc:#b25e00}
    
    *{box-sizing:border-box;font-family:system-ui,Segoe UI,Arial,sans-serif;font-size:13px}
    .wrap{position:absolute;inset:0;background:var(--bg);color:var(--fg);display:flex;flex-direction:column}
    header{display:flex;gap:10px;align-items:center;padding:8px 12px;background:var(--panel);border-bottom:1px solid var(--line);flex-wrap:wrap}
    header h1{font-size:15px;margin:0 8px 0 0;font-weight:700}
    label{display:inline-flex;gap:5px;align-items:center;color:var(--muted)}
    select,input[type=text],input[type=number]{background:var(--input);color:var(--fg);border:1px solid var(--inputline);border-radius:5px;padding:4px 6px}
    input[type=number]{width:62px}
    button{background:var(--btn);color:var(--fg);border:1px solid var(--btnline);border-radius:5px;padding:5px 9px;cursor:pointer}
    button:hover{background:var(--btnhover)}
    button.primary{background:#1f6f5a;border-color:#2c8e75}
    button.primary:hover{background:#25826a}
    button.danger{background:#6b2b2b;border-color:#8a3a3a}
    button:disabled{opacity:.45;cursor:default}
    .main{flex:1;min-height:0;display:flex;gap:12px;padding:12px;overflow:auto;align-items:flex-start;flex-wrap:wrap}
    .stage{flex:1 1 520px;min-width:0}
    .side{flex:0 0 330px;display:flex;flex-direction:column;gap:10px}
    .card{background:var(--panel);border:1px solid var(--line);border-radius:8px;padding:9px 10px}
    .card h2{font-size:11px;letter-spacing:.08em;text-transform:uppercase;color:var(--faint);margin:0 0 7px;font-weight:700}
    .row{display:flex;gap:6px;flex-wrap:wrap;align-items:center;margin-bottom:6px}
    .row:last-child{margin-bottom:0}
    .cv{position:relative;background:#05070a;outline:2px solid var(--btnline);user-select:none;touch-action:none;margin:6px 0}
    .box{position:absolute;border:1px solid;display:flex;align-items:center;overflow:hidden;cursor:move}
    .box.txt{background:#f6922222;border-color:#f69222;color:#f69222}
    .box.pic{background:#38b6ff22;border-color:#38b6ff;color:#38b6ff;justify-content:center;font-weight:700}
    .box.sel{outline:2px solid #fff;z-index:5}
    .box .t{white-space:nowrap;overflow:hidden;font-family:ui-monospace,Consolas,monospace;font-weight:700;width:100%;pointer-events:none}
    .box .lk{position:absolute;right:2px;top:1px;font-size:11px;font-weight:700;line-height:1.2;padding:0 4px;border-radius:3px;color:#000;pointer-events:none}
    .box .num{position:absolute;left:1px;top:1px;font-size:10px;line-height:1;background:#000b;color:#fff;padding:1px 3px;border-radius:3px;pointer-events:none}
    .tag{font-weight:700;color:#000;padding:0 5px;border-radius:3px}
    .linkinfo{margin:2px 0 6px;color:var(--muted);font-size:12px}
    svg.links{position:absolute;inset:0;width:100%;height:100%;pointer-events:none;z-index:3}
    .dim{position:absolute;left:0;top:-17px;font-size:11px;color:#fff;background:#000a;padding:0 4px;pointer-events:none;white-space:nowrap}
    .h{position:absolute;width:9px;height:9px;background:#fff;border:1px solid #000;z-index:6}
    .g{position:absolute;background:#ff4fd8;pointer-events:none;z-index:4}
    .m{position:absolute;border:1px dashed #ffffff33;pointer-events:none}
    .grid2{display:grid;grid-template-columns:repeat(2,1fr);gap:6px}
    .grid3{display:grid;grid-template-columns:repeat(3,1fr);gap:6px}
    .iss{display:flex;gap:6px;align-items:center;margin:3px 0}
    .ok{color:var(--okc)}.bad{color:var(--badc)}
    .layer{display:flex;gap:6px;padding:3px 6px;border-radius:4px;cursor:pointer;font-family:ui-monospace,Consolas,monospace;font-size:12px}
    .layer.sel{background:var(--selrow)}
    pre{margin:0;white-space:pre-wrap;max-height:150px;overflow:auto;font-size:12px;color:var(--pre);font-family:ui-monospace,Consolas,monospace}
    .hint{color:var(--faint);font-size:12px}
    .sp{flex:1}
  ` }));

  const ui = {};
  function numInput(key, label, min, max, onchange) {
    const i = el('input', { type: 'number', min, max, onchange: (e) => onchange(parseInt(e.target.value, 10)) });
    ui[key] = i;
    return el('label', {}, label, i);
  }

  ui.style = el('select', { onchange: (e) => selectStyle(parseInt(e.target.value, 10)) });
  ui.layout = el('select', { onchange: (e) => selectLayout(e.target.value) });
  ui.name = el('input', { type: 'text', value: S.name, size: 22, oninput: (e) => { S.name = e.target.value; } });
  ui.asStyle = el('input', { type: 'checkbox', onchange: (e) => { S.asStyle = e.target.checked; } });
  ui.status = el('pre', {}, 'Loading styles from OMNIA…');
  ui.cv = el('div', { class: 'cv' });
  ui.size = el('span', { class: 'hint' });

  ui.banner = el('div', { style: 'display:none;flex:1 0 100%;background:#6b2b2b;color:#fff;padding:6px 10px;border-radius:5px' });
  ui.retry = el('button', { text: 'Retry', style: 'display:none' });
  const header = el('header', {},
    ui.banner, ui.retry,
    el('h1', { text: 'VMS Layout Tool' }),
    el('label', {}, 'Sign size', ui.style),
    el('label', {}, 'Layout', ui.layout),
    el('label', {}, 'Name', ui.name),
    el('label', {}, ui.asStyle, 'send as style'),
    ui.size,
    el('span', { class: 'sp' }),
    (ui.theme = el('button', { onclick: () => { S.set.theme = S.set.theme === 'light' ? 'dark' : 'light'; saveSettings(); applyTheme(); } })),
    el('button', { text: 'Undo', onclick: undo }),
    el('button', { text: 'Redo', onclick: redo }),
    el('button', { text: 'Close ✕', onclick: close }),
  );

  // presets card
  ui.rows = el('input', { type: 'number', min: 0, max: 8, value: S.set.rows, style: 'width:52px', onchange: (e) => { S.set.rows = clamp(parseInt(e.target.value, 10) || 0, 0, 8); saveSettings(); } });
  ui.picto = el('select', { onchange: (e) => { S.set.picto = e.target.value; saveSettings(); } },
    el('option', { value: 'left', text: 'Pictogram left' }), el('option', { value: 'right', text: 'Pictogram right' }), el('option', { value: 'none', text: 'No pictogram' }));
  ui.picto.value = S.set.picto;
  ui.pictoSize = el('input', { type: 'number', min: 0, max: 400, value: 0, style: 'width:62px', title: '0 = automatic square' });
  const mkSet = (key, label, min, max) => {
    const i = el('input', { type: 'number', min, max, value: S.set[key], onchange: (e) => { S.set[key] = clamp(parseInt(e.target.value, 10) || 0, min, max); saveSettings(); } });
    ui['set_' + key] = i;
    return el('label', {}, label, i);
  };
  const presets = el('div', { class: 'card' }, el('h2', { text: 'Start from a preset' }),
    el('div', { class: 'row' }, ui.picto, el('label', {}, 'text rows', ui.rows)),
    el('div', { class: 'row' }, el('label', {}, 'pictogram size', ui.pictoSize), el('span', { class: 'hint', text: '0 = auto' })),
    el('div', { class: 'row' }, mkSet('margin', 'margin (empty px)', 0, 30), mkSet('gap', 'gap (empty px)', 0, 30)),
    el('div', { class: 'row' }, el('button', { class: 'primary', text: 'Build layout', onclick: buildPreset }), el('span', { class: 'hint', text: 'replaces the boxes (undoable)' })));

  // boxes card
  const lX = numInput('fX', 'X', 1, 999, (v) => setField('x1', v));
  const lY = numInput('fY', 'Y', 1, 999, (v) => setField('y1', v));
  const lW = numInput('fW', 'W', 1, 999, (v) => setField('w', v));
  const lH = numInput('fH', 'H', 1, 999, (v) => setField('h', v));
  ui.type = el('select', { onchange: (e) => edit(() => selBoxes().forEach((b) => { b.type = e.target.value; if (b.type === 'Text' && !b.text) b.text = 'SAMPLE TEXT'; })) },
    el('option', { value: 'Text', text: 'Text' }), el('option', { value: 'Graphic', text: 'Pictogram' }));
  ui.text = el('input', { type: 'text', size: 18, oninput: (e) => { selBoxes().forEach((b) => { b.text = e.target.value; }); renderCanvas(); } });
  ui.align = el('select', { onchange: (e) => { selBoxes().forEach((b) => { b.align = e.target.value; }); renderCanvas(); } },
    el('option', { value: 'left', text: 'left' }), el('option', { value: 'center', text: 'centre' }), el('option', { value: 'right', text: 'right' }));
  const boxes = el('div', { class: 'card' }, el('h2', { text: 'Boxes' }),
    el('div', { class: 'row' },
      el('button', { text: '+ Text', onclick: () => addBox('Text') }), el('button', { text: '+ Pictogram', onclick: () => addBox('Graphic') }),
      el('button', { text: 'Duplicate', onclick: duplicate }), el('button', { class: 'danger', text: 'Delete', onclick: del })),
    el('div', { class: 'row' }, lX, lY, lW, lH),
    el('div', { class: 'row' }, el('label', {}, 'type', ui.type), el('label', {}, 'align', ui.align)),
    el('div', { class: 'row' }, el('label', {}, 'preview text', ui.text)),
    el('div', { class: 'row' }, el('button', { text: 'Link text rows (share font)', onclick: linkSel }), el('button', { text: 'Select linked', onclick: selectLinked, title: 'Select every box linked to the selected one' }), el('button', { text: 'Unlink', onclick: unlinkSel })),
    (ui.linkInfo = el('div', { class: 'linkinfo' })),
    (ui.layers = el('div', {})));

  // symmetry card
  const tool = (text, fn, title) => el('button', { text, title, onclick: () => op(fn) });
  ui.issues = el('div', {});
  const symmetry = el('div', { class: 'card' }, el('h2', { text: 'Symmetry' }),
    el('div', { class: 'grid3' },
      tool('Centre ↔ sign', (s) => Geo.centreInSign(s.length ? s : S.boxes, 'h', S.style.W, S.style.H), 'Equal left and right margins'),
      tool('Centre ↕ sign', (s) => Geo.centreInSign(s.length ? s : S.boxes, 'v', S.style.W, S.style.H), 'Equal top and bottom margins'),
      tool('Mirror ↔', () => Geo.mirror(S.boxes, S.style.W, S.style.H, 'h'), 'Flip the whole layout left/right'),
      tool('Align left', (s) => Geo.align(s, 'left')), tool('Align right', (s) => Geo.align(s, 'right')), tool('Mirror ↕', () => Geo.mirror(S.boxes, S.style.W, S.style.H, 'v')),
      tool('Align top', (s) => Geo.align(s, 'top')), tool('Align bottom', (s) => Geo.align(s, 'bottom')), tool('Centre rows', (s) => Geo.align(s, 'vcentre'), 'Align vertical centres'),
      tool('Same width', (s) => Geo.sameSize(s, 'w'), 'Match the first selected'), tool('Same height', (s) => Geo.sameSize(s, 'h')), tool('Same size', (s) => Geo.sameSize(s, 'both')),
      tool('Equal gaps ↕', (s) => Geo.distribute(s, 'v', null), 'Keep first and last, space the rest evenly'),
      tool('Equal gaps ↔', (s) => Geo.distribute(s, 'h', null)),
      tool('Set gap…', (s) => { const g = parseInt(prompt('Gap between boxes (empty pixels)', S.set.gap), 10); if (!isNaN(g)) Geo.distribute(s, 'v', g); }, 'Fixed vertical gap from the first box')),
    el('div', { class: 'row', style: 'margin-top:8px' }, el('button', { class: 'primary', text: 'Tidy everything', onclick: () => edit(() => Geo.tidy(S.boxes, S.style.W, S.style.H)), title: 'Equal edges, heights and gaps, then centre' }),
      el('span', { class: 'hint', text: 'Select 2+ boxes for align / same / equal-gap tools.' })),
    ui.issues);

  // view + save card
  ui.snap = el('input', { type: 'checkbox', checked: S.set.snap, onchange: (e) => { S.set.snap = e.target.checked; saveSettings(); } });
  ui.preview = el('input', { type: 'checkbox', checked: S.set.preview, onchange: (e) => { S.set.preview = e.target.checked; saveSettings(); renderCanvas(); } });
  ui.grid = el('select', { onchange: (e) => { S.set.grid = parseInt(e.target.value, 10); saveSettings(); } }, [1, 2, 4, 8].map((n) => el('option', { value: n, text: n + ' px' })));
  ui.grid.value = String(S.set.grid);
  ui.dry = el('input', { type: 'checkbox', onchange: (e) => { S.dry = e.target.checked; } });
  ui.saveNew = el('button', { class: 'primary', text: 'Save as new layout', onclick: () => save(true) });
  ui.saveEdit = el('button', { text: 'Save changes to this layout', onclick: () => save(false) });
  ui.delLayout = el('button', { class: 'danger', text: 'Delete layout', onclick: deleteLayout });
  const saveCard = el('div', { class: 'card' }, el('h2', { text: 'View and save' }),
    el('div', { class: 'row' }, el('label', {}, ui.snap, 'snap guides'), el('label', {}, 'grid', ui.grid), el('label', {}, ui.preview, 'preview text')),
    el('div', { class: 'row' }, el('label', {}, ui.dry, 'dry run (log calls, send nothing)')),
    el('div', { class: 'row' }, ui.saveNew, ui.saveEdit, ui.delLayout),
    ui.status);

  const stage = el('div', { class: 'stage' }, ui.cv,
    el('div', { class: 'hint', text: 'Drag to move, drag handles to resize, arrows nudge (Shift = 5px), Ctrl+Z undo, Ctrl+C / Ctrl+V copy and paste, Ctrl+D duplicate, Del delete, Shift-click multi-select. Pink lines show snaps.' }));
  const side = el('div', { class: 'side' }, presets, boxes, symmetry, saveCard);
  ui.wrap = el('div', { class: 'wrap', tabindex: '-1' }, header, el('div', { class: 'main' }, stage, side));
  root.append(ui.wrap);
  function applyTheme() {
    const light = S.set.theme === 'light';
    ui.wrap.classList.toggle('light', light);
    ui.theme.textContent = light ? '🌙 Dark' : '☀ Light';
  }
  applyTheme();
  // Keep keyboard focus inside the tool so shortcuts (Ctrl+C/V/Z...) work
  // without first clicking on the sign. Clicks on text fields keep their own focus.
  ui.wrap.addEventListener('pointerdown', (e) => {
    const t = e.target;
    if (!(t.closest && t.closest('input[type=text],input[type=number],textarea,select'))) setTimeout(() => ui.wrap.focus({ preventScroll: true }), 0);
  });
  ui.wrap.style.outline = 'none';

  // ---------- operations ----------
  function op(fn) {
    const s = selBoxes();
    edit(() => fn(s));
  }
  function addBox(type) {
    const { W, H } = S.style;
    edit(() => {
      const w = type === 'Graphic' ? Math.min(60, W) : Math.min(120, W), h = type === 'Graphic' ? Math.min(60, H) : Math.min(20, H);
      const b = newBox({ type, x1: Math.floor((W - w) / 2) + 1, y1: Math.floor((H - h) / 2) + 1, x2: Math.floor((W - w) / 2) + w, y2: Math.floor((H - h) / 2) + h });
      S.boxes.push(b); S.sel = [b.uid];
    });
  }
  function duplicate() {
    const s = selBoxes(); if (!s.length) return;
    edit(() => {
      const ids = [];
      for (const b of s) {
        const c = newBox({ type: b.type, x1: b.x1, y1: b.y1, x2: b.x2, y2: b.y2, text: b.text, align: b.align });
        Geo.shift(c, Math.min(8, S.style.W - c.x2), Math.min(8, S.style.H - c.y2)); // offset the copy so it is visible
        S.boxes.push(c); ids.push(c.uid);
      }
      S.sel = ids;
    });
  }
  // Ctrl+C / Ctrl+V. The clipboard survives switching layouts or sign sizes,
  // so a box can be copied from one layout into another. Each paste lands a
  // little further down-right so copies don't hide each other.
  function copySel() {
    const s = selBoxes(); if (!s.length) return;
    S.clip = { boxes: s.map((b) => ({ type: b.type, x1: b.x1, y1: b.y1, x2: b.x2, y2: b.y2, text: b.text, align: b.align })), n: 0 };
    say(`Copied ${s.length} box${s.length > 1 ? 'es' : ''}. Ctrl+V to paste.`);
  }
  function pasteClip() {
    if (!S.clip || !S.clip.boxes.length) { say('Nothing copied yet. Select a box and press Ctrl+C.'); return; }
    S.clip.n += 1;
    const { W, H } = S.style, bb = Geo.bbox(S.clip.boxes), step = 8 * S.clip.n;
    const dx = clamp(step, 1 - bb.x1, W - bb.x2), dy = clamp(step, 1 - bb.y1, H - bb.y2);
    edit(() => {
      const ids = S.clip.boxes.map((c) => { const b = newBox({ ...c }); Geo.shift(b, dx, dy); S.boxes.push(b); return b.uid; });
      S.sel = ids;
    });
  }
  function del() {
    const s = new Set(S.sel); if (!s.size) return;
    edit(() => { S.boxes = S.boxes.filter((b) => !s.has(b.uid)); S.links = S.links.map((g) => g.filter((id) => !s.has(id))).filter((g) => g.length > 1); S.sel = []; });
  }
  function setField(k, v) {
    const s = selBoxes(); if (s.length !== 1 || isNaN(v)) return;
    const b = s[0], { W, H } = S.style;
    edit(() => {
      if (k === 'x1') { const w = Geo.bw(b); b.x1 = clamp(v, 1, W - w + 1); b.x2 = b.x1 + w - 1; }
      else if (k === 'y1') { const h = Geo.bh(b); b.y1 = clamp(v, 1, H - h + 1); b.y2 = b.y1 + h - 1; }
      else if (k === 'w') b.x2 = clamp(b.x1 + v - 1, b.x1, W);
      else if (k === 'h') b.y2 = clamp(b.y1 + v - 1, b.y1, H);
    });
  }
  function buildPreset() {
    const { W, H } = S.style;
    const specs = Geo.preset({ W, H, margin: S.set.margin, gap: S.set.gap, rows: S.set.rows, picto: S.set.picto, pictoSize: parseInt(ui.pictoSize.value, 10) || null });
    edit(() => { S.boxes = specs.map(newBox); S.links = []; S.sel = []; });
  }
  function linkSel() {
    const t = selBoxes().filter((b) => b.type === 'Text'); if (t.length < 2) { say('Select 2 or more text boxes to link.'); return; }
    const ids = new Set(t.map((b) => b.uid));
    edit(() => { S.links = S.links.map((g) => g.filter((id) => !ids.has(id))).filter((g) => g.length > 1); S.links.push(t.map((b) => b.uid)); });
  }
  function selectLinked() {
    const ids = new Set(S.sel);
    S.links.forEach((g) => { if (g.some((u) => ids.has(u))) g.forEach((u) => ids.add(u)); });
    S.sel = [...ids]; refresh();
  }
  function unlinkSel() {
    const ids = new Set(S.sel);
    edit(() => { S.links = S.links.map((g) => g.filter((id) => !ids.has(id))).filter((g) => g.length > 1); });
    say('Unlinked here. A link that already exists in OMNIA is removed when you save the layout.');
  }

  // ---------- rendering ----------
  function refresh() { renderCanvas(); renderFields(); renderLayers(); renderIssues(); }

  function renderCanvas() {
    const { W, H } = S.style, s = S.scale, cv = ui.cv, sel = new Set(S.sel);
    cv.style.width = W * s + 'px'; cv.style.height = H * s + 'px';
    cv.replaceChildren();
    const m = S.set.margin;
    cv.append(el('div', { class: 'm', style: `left:${m * s}px;top:${m * s}px;width:${(W - 2 * m) * s}px;height:${(H - 2 * m) * s}px` }));
    cv.append(el('div', { class: 'g', style: `left:${(W * s) / 2 - 0.5}px;top:0;width:1px;height:100%;opacity:.25` }));
    cv.append(el('div', { class: 'g', style: `top:${(H * s) / 2 - 0.5}px;left:0;height:1px;width:100%;opacity:.25` }));
    const partners = new Set(S.sel.flatMap(partnersOf));
    S.boxes.forEach((b, bi) => {
      const w = Geo.bw(b), h = Geo.bh(b), gi = groupOf(b.uid);
      const ring = gi >= 0 ? `box-shadow:inset 0 0 0 2px ${groupColor(gi)};` : '';
      const part = partners.has(b.uid) && !sel.has(b.uid) ? `outline:3px dashed ${groupColor(gi)};outline-offset:1px;z-index:5;` : '';
      const d = el('div', { class: `box ${b.type === 'Graphic' ? 'pic' : 'txt'}${sel.has(b.uid) ? ' sel' : ''}`, 'data-uid': b.uid, style: `left:${(b.x1 - 1) * s}px;top:${(b.y1 - 1) * s}px;width:${w * s}px;height:${h * s}px;${ring}${part}` });
      d.append(el('span', { class: 'num', text: String(bi + 1) }));
      if (S.set.preview) {
        const fs = Math.max(8, Math.min(h * s * 0.72, 60));
        if (b.type === 'Text') d.append(el('div', { class: 't', style: `font-size:${fs}px;text-align:${b.align}`, text: b.text }));
        else d.append(el('div', { class: 't', style: `font-size:${Math.max(10, Math.min(fs * 0.5, 40))}px;text-align:center`, text: '▲ PICTO' }));
      }
      if (gi >= 0) d.append(el('span', { class: 'lk', style: `background:${groupColor(gi)}`, text: '🔗 ' + groupLetter(gi) }));
      if (sel.has(b.uid)) {
        d.append(el('div', { class: 'dim', text: `${w}×${h}  @${b.x1},${b.y1}` }));
        if (S.sel.length === 1) ['nw', 'n', 'ne', 'e', 'se', 's', 'sw', 'w'].forEach((k) => {
          const x = k.includes('w') ? -5 : k.includes('e') ? w * s - 4 : (w * s) / 2 - 5;
          const y = k.includes('n') ? -5 : k.includes('s') ? h * s - 4 : (h * s) / 2 - 5;
          d.append(el('div', { class: 'h', 'data-h': k, style: `left:${x}px;top:${y}px;cursor:${k}-resize` }));
        });
      }
      cv.append(d);
    });
    // Dashed connector lines between the boxes of each link group.
    if (S.links.some((g) => g.length > 1)) {
      const NS = 'http://www.w3.org/2000/svg', svg = document.createElementNS(NS, 'svg');
      svg.setAttribute('class', 'links');
      S.links.forEach((g, gi) => {
        const pts = g.map(byUid).filter(Boolean).sort((p, q) => p.y1 - q.y1 || p.x1 - q.x1)
          .map((b) => `${((b.x1 + b.x2) / 2 - 0.5) * s},${((b.y1 + b.y2) / 2 - 0.5) * s}`);
        if (pts.length < 2) return;
        const pl = document.createElementNS(NS, 'polyline');
        pl.setAttribute('points', pts.join(' ')); pl.setAttribute('fill', 'none'); pl.setAttribute('stroke', groupColor(gi));
        pl.setAttribute('stroke-width', '2'); pl.setAttribute('stroke-dasharray', '6 4'); svg.append(pl);
      });
      cv.append(svg);
    }
    S.guides.x.forEach((x) => cv.append(el('div', { class: 'g', style: `left:${x * s}px;top:0;width:1px;height:100%` })));
    S.guides.y.forEach((y) => cv.append(el('div', { class: 'g', style: `top:${y * s}px;left:0;height:1px;width:100%` })));
    ui.size.textContent = `${W}×${H} px · scale ${s}×`;
  }

  function renderFields() {
    const s = selBoxes(), one = s.length === 1 ? s[0] : null;
    const setv = (inp, v) => { if (root.activeElement !== inp) inp.value = v; };
    setv(ui.fX, one ? one.x1 : ''); setv(ui.fY, one ? one.y1 : ''); setv(ui.fW, one ? Geo.bw(one) : ''); setv(ui.fH, one ? Geo.bh(one) : '');
    [ui.fX, ui.fY, ui.fW, ui.fH, ui.text, ui.type, ui.align].forEach((i) => { i.disabled = !s.length || (i === ui.fX || i === ui.fY || i === ui.fW || i === ui.fH ? !one : false); });
    if (s.length) { ui.type.value = s[0].type; ui.align.value = s[0].align; setv(ui.text, s[0].text || ''); }
    const existing = S.layoutId != null;
    ui.saveEdit.style.display = existing ? '' : 'none';
    ui.delLayout.style.display = existing ? '' : 'none';
    ui.saveNew.textContent = existing ? 'Save as a copy' : 'Save as new layout';
  }

  function renderLayers() {
    ui.layers.replaceChildren(...S.boxes.map((b, i) => {
      const gi = groupOf(b.uid), p = partnersOf(b.uid).map(boxNo);
      return el('div', { class: 'layer' + (S.sel.includes(b.uid) ? ' sel' : ''), onclick: (e) => selectBox(b.uid, e.shiftKey) },
        el('span', { text: String(i + 1) }), el('span', { text: b.type === 'Graphic' ? 'Picto' : 'Text' }), el('span', { text: `${Geo.bw(b)}×${Geo.bh(b)} @${b.x1},${b.y1}` }),
        gi >= 0 ? el('span', { class: 'tag', style: `background:${groupColor(gi)}`, title: `Linked with box ${p.join(', ')}`, text: `🔗${groupLetter(gi)} ↔ ${p.join(',')}` }) : null);
    }));
    // plain-language description of the current selection's links
    const s = selBoxes();
    if (!S.links.length) ui.linkInfo.textContent = 'No links. Select 2+ text boxes and press "Link text rows" so they share one font and size.';
    else if (s.length === 1) {
      const gi = groupOf(s[0].uid), p = partnersOf(s[0].uid).map(boxNo);
      ui.linkInfo.textContent = gi < 0 ? `Box ${boxNo(s[0].uid)} is not linked to anything.` : `Box ${boxNo(s[0].uid)} is linked with box ${p.join(', ')} (group ${groupLetter(gi)}): they share one font and size.`;
    } else ui.linkInfo.textContent = 'Link groups: ' + S.links.map((g, gi) => `${groupLetter(gi)} = boxes ${g.map(boxNo).join(', ')}`).join(' · ');
  }

  function renderIssues() {
    const list = Geo.analyse(S.boxes, S.style.W, S.style.H);
    ui.issues.replaceChildren(...list.map((i) => el('div', { class: 'iss' },
      el('span', { class: i.ok ? 'ok' : 'bad', text: i.ok ? '✓' : '⚠' }), el('span', { text: i.msg, style: 'flex:1' }),
      !i.ok && i.fix ? el('button', { text: 'Fix', onclick: () => fixIssue(i.fix) }) : null)));
    if (!list.length) ui.issues.append(el('div', { class: 'hint', text: 'No boxes yet — pick a preset or add boxes.' }));
  }
  function fixIssue(f) {
    const { W, H } = S.style;
    edit(() => {
      if (f === 'centreH') Geo.centreInSign(S.boxes, 'h', W, H);
      else if (f === 'centreV') Geo.centreInSign(S.boxes, 'v', W, H);
      else if (f === 'tidy') Geo.tidy(S.boxes, W, H);
    });
  }

  function selectBox(uid, additive) {
    if (additive) S.sel = S.sel.includes(uid) ? S.sel.filter((i) => i !== uid) : [uid, ...S.sel];
    else S.sel = [uid];
    refresh();
  }

  // ---------- pointer interaction ----------
  const snapT = () => Math.max(1, Math.ceil(6 / S.scale));
  function candidates(axis, exclude) {
    const { W, H } = S.style, m = S.set.margin, g = S.set.gap, size = axis === 'x' ? W : H;
    const out = [1, size, (1 + size) / 2, m + 1, size - m];
    S.boxes.filter((b) => !exclude.has(b.uid)).forEach((b) => {
      const lo = axis === 'x' ? b.x1 : b.y1, hi = axis === 'x' ? b.x2 : b.y2;
      out.push(lo, hi, (lo + hi) / 2, hi + 1 + g, lo - 1 - g);
    });
    return out;
  }
  function bestSnap(vals, cands) {
    let best = null;
    for (const v of vals) for (const c of cands) { const d = c - v.v; if (Math.abs(d) <= snapT() && (!best || Math.abs(d) < Math.abs(best.d))) best = { d, line: v.line(c) }; }
    return best;
  }

  ui.cv.addEventListener('pointerdown', (e) => {
    const boxEl = e.target.closest('.box');
    if (!boxEl) { if (!e.shiftKey) { S.sel = []; refresh(); } return; }
    const uid = parseInt(boxEl.dataset.uid, 10);
    if (!S.sel.includes(uid)) { S.sel = e.shiftKey ? [uid, ...S.sel] : [uid]; }
    else if (e.shiftKey && !e.target.dataset.h) { S.sel = S.sel.filter((i) => i !== uid); refresh(); return; }
    const hnd = e.target.dataset.h || null;
    S.drag = { mode: hnd ? 'resize' : 'move', hnd, sx: e.clientX, sy: e.clientY, orig: S.sel.map((id) => ({ ...byUid(id) })), moved: false };
    ui.cv.setPointerCapture(e.pointerId);
    refresh();
    e.preventDefault();
  });
  ui.cv.addEventListener('pointermove', (e) => {
    const d = S.drag; if (!d) return;
    const { W, H } = S.style, s = S.scale;
    let dx = Math.round((e.clientX - d.sx) / s), dy = Math.round((e.clientY - d.sy) / s);
    // a plain click must not nudge a box: only start dragging after a few screen pixels
    if (!d.moved && Math.hypot(e.clientX - d.sx, e.clientY - d.sy) < 5) return;
    if (!d.moved && !dx && !dy) return;
    if (!d.moved) { pushUndo(); d.moved = true; }
    const ids = new Set(d.orig.map((o) => o.uid));
    const snapOn = S.set.snap && !e.altKey;
    S.guides = { x: [], y: [] };
    if (d.mode === 'move') {
      const g = S.set.grid;
      dx = Math.round(dx / g) * g; dy = Math.round(dy / g) * g;
      const bb = Geo.bbox(d.orig);
      if (snapOn) {
        const sx = bestSnap([{ v: bb.x1 + dx, line: (c) => c - 1 }, { v: bb.x2 + dx, line: (c) => c }, { v: (bb.x1 + bb.x2) / 2 + dx, line: (c) => c - 0.5 }], candidates('x', ids));
        const sy = bestSnap([{ v: bb.y1 + dy, line: (c) => c - 1 }, { v: bb.y2 + dy, line: (c) => c }, { v: (bb.y1 + bb.y2) / 2 + dy, line: (c) => c - 0.5 }], candidates('y', ids));
        if (sx) { dx += Math.round(sx.d); S.guides.x.push(sx.line); }
        if (sy) { dy += Math.round(sy.d); S.guides.y.push(sy.line); }
      }
      dx = clamp(dx, 1 - bb.x1, W - bb.x2); dy = clamp(dy, 1 - bb.y1, H - bb.y2);
      d.orig.forEach((o) => { const b = byUid(o.uid); b.x1 = o.x1 + dx; b.x2 = o.x2 + dx; b.y1 = o.y1 + dy; b.y2 = o.y2 + dy; });
    } else {
      const o = d.orig[0], b = byUid(o.uid), h = d.hnd, MIN = 2;
      let { x1, y1, x2, y2 } = o;
      const snapEdge = (axis, v, lineOf) => { if (!snapOn) return v; const r = bestSnap([{ v, line: lineOf }], candidates(axis, ids)); if (r) { S.guides[axis].push(r.line); return v + Math.round(r.d); } return v; };
      if (h.includes('w')) x1 = snapEdge('x', clamp(o.x1 + dx, 1, o.x2 - MIN + 1), (c) => c - 1);
      if (h.includes('e')) x2 = snapEdge('x', clamp(o.x2 + dx, o.x1 + MIN - 1, W), (c) => c);
      if (h.includes('n')) y1 = snapEdge('y', clamp(o.y1 + dy, 1, o.y2 - MIN + 1), (c) => c - 1);
      if (h.includes('s')) y2 = snapEdge('y', clamp(o.y2 + dy, o.y1 + MIN - 1, H), (c) => c);
      b.x1 = clamp(x1, 1, W); b.x2 = clamp(x2, b.x1, W); b.y1 = clamp(y1, 1, H); b.y2 = clamp(y2, b.y1, H);
    }
    renderCanvas(); renderFields();
  });
  const endDrag = () => { if (!S.drag) return; S.drag = null; S.guides = { x: [], y: [] }; refresh(); };
  ui.cv.addEventListener('pointerup', endDrag);
  ui.cv.addEventListener('pointercancel', endDrag);

  function onKey(e) {
    if (!document.body.contains(host)) return;
    const t = e.composedPath()[0];
    // Leave only real text-entry fields alone; selects, checkboxes and buttons
    // must not swallow Ctrl+C / Ctrl+V / Delete.
    if (t && (t.tagName === 'TEXTAREA' || (t.tagName === 'INPUT' && /^(text|number|search)$/.test(t.type || 'text')))) return;
    if (t && t.tagName === 'SELECT' && /^Arrow/.test(e.key)) return;
    const k = e.key, mod = e.ctrlKey || e.metaKey;
    if (mod && k.toLowerCase() === 'z') { e.preventDefault(); e.stopPropagation(); e.shiftKey ? redo() : undo(); }
    else if (mod && k.toLowerCase() === 'y') { e.preventDefault(); e.stopPropagation(); redo(); }
    else if (mod && k.toLowerCase() === 'd') { e.preventDefault(); e.stopPropagation(); duplicate(); }
    else if (mod && k.toLowerCase() === 'c') { e.preventDefault(); e.stopPropagation(); copySel(); }
    else if (mod && k.toLowerCase() === 'v') { e.preventDefault(); e.stopPropagation(); pasteClip(); }
    else if (mod && k.toLowerCase() === 'a') { e.preventDefault(); e.stopPropagation(); S.sel = S.boxes.map((b) => b.uid); refresh(); }
    else if (k === 'Delete' || k === 'Backspace') { e.preventDefault(); e.stopPropagation(); del(); }
    else if (k.startsWith('Arrow') && S.sel.length) {
      e.preventDefault(); e.stopPropagation();
      const n = e.shiftKey ? 5 : 1, { W, H } = S.style, s = selBoxes(), bb = Geo.bbox(s);
      const dx = clamp(k === 'ArrowLeft' ? -n : k === 'ArrowRight' ? n : 0, 1 - bb.x1, W - bb.x2);
      const dy = clamp(k === 'ArrowUp' ? -n : k === 'ArrowDown' ? n : 0, 1 - bb.y1, H - bb.y2);
      edit(() => s.forEach((b) => Geo.shift(b, dx, dy)));
    } else if (k === 'Escape') { S.sel = []; refresh(); }
  }
  window.addEventListener('keydown', onKey, true);
  window.addEventListener('resize', fitScale);

  // ---------- OMNIA I/O ----------
  function fitScale() {
    if (!S.style) return;
    const stageW = ui.cv.parentElement.clientWidth || window.innerWidth - 400;
    S.scale = clamp(Math.floor((stageW - 8) / S.style.W), 1, 5);
    if (S.drag == null) renderCanvas();
  }

  async function selectStyle(id) {
    S.style = S.styles.find((s) => s.id === id) || S.styles[0];
    ui.style.value = String(S.style.id);
    S.boxes = []; S.links = []; S.sel = []; S.layoutId = null; S.undo = []; S.redo = [];
    await loadLayouts();
    fitScale(); refresh();
  }
  async function loadLayouts(keepId) {
    S.layouts = await Api.getLayouts(S.style.id);
    ui.layout.replaceChildren(el('option', { value: 'new', text: '＋ New layout' }),
      ...S.layouts.map((l) => el('option', { value: l.id, text: `${l.name}${l.asStyle ? ' (style)' : ''}` })));
    ui.layout.value = keepId != null ? String(keepId) : 'new';
  }
  async function selectLayout(v) {
    S.undo = []; S.redo = []; S.sel = [];
    S.linkPairs = [];
    if (v === 'new') { S.layoutId = null; S.boxes = []; S.links = []; S.name = 'TEST_'; S.asStyle = false; }
    else {
      const l = S.layouts.find((x) => String(x.id) === v);
      S.layoutId = l.id; S.name = l.name; S.asStyle = l.asStyle;
      S.boxes = l.boxes.map((b) => newBox({ vid: b.vid, type: b.type, x1: b.x1, y1: b.y1, x2: b.x2, y2: b.y2, raw: b.raw, text: b.type === 'Text' ? 'SAMPLE TEXT' : '' }));
      S.links = []; S.linkPairs = [];
      const seen = new Map();
      try {
        for (const b of S.boxes.filter((x) => x.type === 'Text')) {
          const links = await Api.getLinks(l.id, l.address, b.vid);
          for (const k of links) {
            const o = S.boxes.find((x) => x.vid === k.VirtualUnitID);
            if (o && k.IsDirectLink && !S.linkPairs.some((p) => (p[0] === b.vid && p[1] === o.vid) || (p[0] === o.vid && p[1] === b.vid))) S.linkPairs.push([b.vid, o.vid]);
            if (o) { const g = seen.get(b.uid) || seen.get(o.uid) || []; [b.uid, o.uid].forEach((id) => { if (!g.includes(id)) g.push(id); seen.set(id, g); }); }
          }
        }
        S.links = [...new Set(seen.values())];
      } catch (e) { say('Loaded layout, but could not read its links: ' + e.message); }
    }
    ui.name.value = S.name; ui.asStyle.checked = S.asStyle;
    refresh();
    if (S.layoutId != null) say('Editing an existing layout. If signs use it, OMNIA may refuse changes; use "Save as a copy" to be safe.');
  }

  async function save(asNew) {
    if (S.busy) return;
    const { W, H } = S.style;
    const existingEdit = !asNew && S.layoutId != null;
    const name = ui.name.value.trim();
    if (!name) { say('Give the layout a name first.'); return; }
    if (!S.boxes.length) { say('Nothing to save: no boxes.'); return; }
    const bad = Geo.analyse(S.boxes, W, H).filter((i) => !i.ok && (i.key === 'overlap' || i.key === 'bounds'));
    if (bad.length && !confirm(bad.map((b) => b.msg).join('\n') + '\n\nSave anyway?')) return;
    if (!S.dry) {
      const msg = existingEdit
        ? `Change the EXISTING layout "${name}" on ${S.style.name}?\nSigns using it will be affected.`
        : `Create layout "${name}" on ${S.style.name} with ${S.boxes.length} boxes?`;
      if (!confirm(msg)) return;
    }
    S.busy = true; Api.state.dry = S.dry; Api.state.log = []; Api.state.writes = 0;
    ui.status.textContent = S.dry ? 'DRY RUN — nothing is sent.\n' : 'Saving…\n';
    try {
      const meta = S.layouts.find((l) => l.id === S.layoutId);
      const lid = await Sync.run(Api, {
        styleId: S.style.id, address: '01', existingEdit, layoutId: S.layoutId, name, asStyle: S.asStyle,
        metaChanged: !!meta && (meta.name !== name || meta.asStyle !== S.asStyle),
        boxes: S.boxes, links: S.links, linkPairs: S.linkPairs || [], dry: S.dry,
      });
      const describe = (c) => {
        const p = c.params || {};
        if (c.fn === 'SaveVU') { const v = p.vunit; return `box ${v.vUnitId === '-1' ? 'NEW' : '#' + v.vUnitId} ${v.custom.Type} ${v.StartX},${v.StartY} → ${v.EndX},${v.EndY}`; }
        return JSON.stringify(p);
      };
      const lines = Api.state.log.map((c) => `${c.dry ? '· ' : '✓ '}${c.fn} ${describe(c)}`);
      ui.status.textContent = (S.dry ? 'DRY RUN — these calls would be sent:\n' : `Saved layout "${name}" (id ${lid}). Reopen the Layout/Style Designer to see it.\n`) + lines.join('\n');
      if (!S.dry) { await loadLayouts(lid); await selectLayout(String(lid)); }
    } catch (e) {
      const changed = Api.state.writes > 0;
      const state = e.rolledBack ? 'The half-made layout was removed again, so nothing was left behind.'
        : e.rollbackFailed ? 'The layout was only partly saved and could NOT be cleaned up automatically; check it in the designer and delete it if it is incomplete.'
        : changed ? 'Some changes were already saved before it stopped. Check the layout in the designer.' : 'Nothing was changed in OMNIA.';
      let lead = 'STOPPED: ' + e.message;
      if (/Default Value Management/i.test(e.message)) {
        lead = 'OMNIA will not let this layout be changed because it is in use ("involved in Default Value Management operations"), '
          + 'most likely as default content of a sign. ' + state
          + '\n\nWhat you can do: use "Save as a copy" with a new name (this always works), '
          + 'or free the layout in OMNIA first and then save again.';
      } else lead += '\n' + state;
      ui.status.textContent = lead + '\n\nOMNIA said / calls so far:\n' + Api.state.log.map((c) => `${c.fn} → ${c.status || 'dry'} ${c.reply || ''}`).join('\n');
    } finally { S.busy = false; Api.state.dry = false; }
  }

  async function deleteLayout() {
    if (S.layoutId == null || S.busy) return;
    if (!confirm(`Permanently delete layout "${S.name}"?\nThis cannot be undone. OMNIA refuses if signs use it.`)) return;
    try {
      Api.state.dry = S.dry; Api.state.log = [];
      // highest id first: OMNIA renumbers the remaining boxes after each delete
      for (const b of S.boxes.filter((x) => x.vid != null).sort((p, q) => q.vid - p.vid)) await Api.deleteVU(S.layoutId, '01', S.style.id, b.vid);
      await Api.deleteLayout(S.layoutId);
      say(S.dry ? 'DRY RUN: would delete the layout.' : 'Layout deleted.');
      if (!S.dry) { S.layoutId = null; S.boxes = []; await loadLayouts(); ui.layout.value = 'new'; refresh(); }
    } catch (e) { say('Delete failed: ' + e.message); } finally { Api.state.dry = false; }
  }

  function close() { window.removeEventListener('keydown', onKey, true); window.removeEventListener('resize', fitScale); host.remove(); window.__vlt = null; if (window.__vltTabMode) window.close(); }
  function open() { if (!document.body.contains(host)) document.body.append(host); fitScale(); ui.wrap.focus({ preventScroll: true }); }

  window.__vlt = { open, close, state: S };
  document.body.append(host);
  async function init() {
    ui.banner.style.display = 'none'; ui.retry.style.display = 'none';
    try {
      if (!Api.origin) throw new Error('This is not the OMNIA page. Log in to OMNIA first, then click the bookmark there (not on the install page).');
      S.styles = await Api.getStyles();
      if (!S.styles.length) throw new Error('OMNIA returned no drawable sign styles.');
      ui.style.replaceChildren(...S.styles.map((s) => el('option', { value: s.id, text: `${s.name} (${s.W}×${s.H})` })));
      const pref = S.styles.find((s) => s.W === 272) || S.styles[0];
      await selectStyle(pref.id);
      say((window.__vltNote ? window.__vltNote + '\n\n' : '') + 'Ready. Pick a preset and press "Build layout", or add boxes by hand. Tick "dry run" first if you want to see the calls without sending them.');
    } catch (e) {
      console.error('[VMS Layout Tool]', e);
      ui.banner.textContent = 'Could not load from OMNIA — ' + (e && e.message ? e.message : e) + ' (page: ' + location.origin + ')';
      ui.banner.style.display = ''; ui.retry.style.display = '';
      say('Load failed. See the red message at the top.');
    }
  }
  ui.retry.addEventListener('click', init);
  ui.wrap.focus({ preventScroll: true });
  init();
})();
