const test = require('node:test');
const assert = require('node:assert');
const Sync = require('../src/sync.js');

// A fake OMNIA that reproduces the behaviours seen on the real server.
function fakeOmnia() {
  let nextLayout = 100;
  const layouts = new Map();
  const api = {
    layouts,
    async saveLayout({ id, name, asStyle }) {
      if (id === -1) { id = nextLayout++; layouts.set(id, { id, name, asStyle, hasLU: true, boxes: [{ vid: 1, type: 'Graphic', x1: 1, y1: 1, x2: 272, y2: 96 }], links: [] }); }
      else Object.assign(layouts.get(id), { name, asStyle });
      return id;
    },
    async getLayouts() {
      return [...layouts.values()].map((l) => ({ id: l.id, name: l.name, asStyle: l.asStyle, address: '01', boxes: l.hasLU ? l.boxes.map((b) => ({ ...b, raw: null })) : [] }));
    },
    async saveVU(lid, address, styleId, box) {
      const l = layouts.get(lid);
      if (!l.hasLU) throw new Error('SaveVU was refused by OMNIA: ""'); // empty reply once the drawing area is gone
      const rec = { type: box.type, x1: box.x1, y1: box.y1, x2: box.x2, y2: box.y2 };
      if (box.vid == null) { rec.vid = l.boxes.length + 1; l.boxes.push(rec); return rec.vid; }
      Object.assign(l.boxes.find((b) => b.vid === box.vid), rec);
      return box.vid;
    },
    async deleteVU(lid, address, styleId, vid) {
      const l = layouts.get(lid);
      l.boxes = l.boxes.filter((b) => b.vid !== vid).map((b) => (b.vid > vid ? { ...b, vid: b.vid - 1 } : b)); // renumbers
      l.links = l.links.filter(([a, b]) => a !== vid && b !== vid).map(([a, b]) => [a > vid ? a - 1 : a, b > vid ? b - 1 : b]);
      if (!l.boxes.length) l.hasLU = false; // deleting the only box removes the drawing area
    },
    async getLinks(lid, address, vid) {
      return layouts.get(lid).links.filter(([a, b]) => a === vid || b === vid).map(([a, b]) => ({ VirtualUnitID: a === vid ? b : a, IsDirectLink: true }));
    },
    async deleteLayout(id) { layouts.delete(id); },
    async linkVu(styleId, lid, address, a, b) { layouts.get(lid).links.push([a, b]); },
    async unlinkVu(styleId, lid, address, a, b) {
      const l = layouts.get(lid); const n = l.links.length;
      l.links = l.links.filter(([x, y]) => !((x === a && y === b) || (x === b && y === a)));
      return l.links.length < n;
    },
  };
  return api;
}

const T = (uid, x1, y1, x2, y2, type = 'Text', vid = null) => ({ uid, vid, type, x1, y1, x2, y2, raw: null });
const norm = (l) => l.boxes.map((b) => `${b.type[0]}${b.x1},${b.y1},${b.x2},${b.y2}`).sort();
const linkedByPos = (l) => l.links.map(([a, b]) => { const p = (v) => { const x = l.boxes.find((q) => q.vid === v); return `${x.x1},${x.y1}`; }; return [p(a), p(b)].sort().join('|'); }).sort();

// Travel Times: 8 text boxes, linked in pairs, no pictogram. This is the case that left an empty layout.
const travel = () => [T(1, 4, 5, 206, 23), T(2, 4, 28, 206, 46), T(3, 4, 51, 206, 69), T(4, 4, 74, 206, 92),
  T(5, 210, 5, 269, 23), T(6, 210, 28, 269, 46), T(7, 210, 51, 269, 69), T(8, 210, 74, 269, 92)];

test('new text-only layout (no pictogram) is created with all boxes and links', async () => {
  const api = fakeOmnia();
  const lid = await Sync.run(api, { styleId: 2, existingEdit: false, name: 'Travel Times_new', asStyle: true, boxes: travel(), links: [[1, 5], [2, 6], [3, 7], [4, 8]], dry: false });
  const l = api.layouts.get(lid);
  assert.strictEqual(l.boxes.length, 8, 'all 8 boxes present');
  assert.ok(!l.boxes.some((b) => b.type === 'Graphic'), 'default pictogram removed');
  assert.deepStrictEqual(l.boxes.map((b) => b.vid).sort((a, b) => a - b), [1, 2, 3, 4, 5, 6, 7, 8], 'ids are 1..8');
  assert.deepStrictEqual(linkedByPos(l), ['210,28|4,28', '210,5|4,5', '210,51|4,51', '210,74|4,74'].sort());
});

test('new layout with a pictogram reuses the default box', async () => {
  const api = fakeOmnia();
  const boxes = [T(1, 6, 6, 91, 91, 'Graphic'), T(2, 95, 6, 267, 24), T(3, 95, 28, 267, 46)];
  const lid = await Sync.run(api, { styleId: 2, existingEdit: false, name: 'P', asStyle: false, boxes, links: [[2, 3]], dry: false });
  const l = api.layouts.get(lid);
  assert.deepStrictEqual(norm(l), ['G6,6,91,91', 'T95,28,267,46', 'T95,6,267,24'].sort());
  assert.strictEqual(l.links.length, 1);
});

test('editing an existing layout: deleting a box does not shift edits onto the wrong boxes', async () => {
  const api = fakeOmnia();
  const lid = await Sync.run(api, { styleId: 2, existingEdit: false, name: 'E', asStyle: false, boxes: [T(1, 5, 5, 100, 20), T(2, 5, 25, 100, 40), T(3, 5, 45, 100, 60), T(4, 5, 65, 100, 80)], links: [[3, 4]], dry: false });
  const cur = (await api.getLayouts())[0].boxes;
  const byPos = (y) => cur.find((b) => b.y1 === y).vid;
  // delete the first box, move the third, keep the links of 3-4
  const boxes = [T(12, 5, 25, 100, 40, 'Text', byPos(25)), T(13, 8, 45, 100, 60, 'Text', byPos(45)), T(14, 5, 65, 100, 80, 'Text', byPos(65))];
  await Sync.run(api, { styleId: 2, existingEdit: true, layoutId: lid, name: 'E', asStyle: false, boxes, links: [[13, 14]], linkPairs: [[byPos(45), byPos(65)]], dry: false });
  const l = api.layouts.get(lid);
  assert.deepStrictEqual(norm(l), ['T5,25,100,40', 'T5,65,100,80', 'T8,45,100,60'].sort());
  assert.deepStrictEqual(linkedByPos(l), ['5,65|8,45']);
});

test('removing a link in the editor removes it in OMNIA', async () => {
  const api = fakeOmnia();
  const lid = await Sync.run(api, { styleId: 2, existingEdit: false, name: 'L', asStyle: false, boxes: [T(1, 5, 5, 100, 20), T(2, 5, 25, 100, 40)], links: [[1, 2]], dry: false });
  const cur = (await api.getLayouts())[0].boxes;
  const boxes = cur.map((b, i) => T(20 + i, b.x1, b.y1, b.x2, b.y2, b.type, b.vid)).filter((b) => b.type === 'Text');
  await Sync.run(api, { styleId: 2, existingEdit: true, layoutId: lid, name: 'L', asStyle: false, boxes, links: [], linkPairs: [[boxes[0].vid, boxes[1].vid]], dry: false });
  assert.strictEqual(api.layouts.get(lid).links.length, 0);
});

test('dry run sends no real changes through the same sequence', async () => {
  const calls = [];
  const rec = (n, v) => async (...a) => { calls.push(n); return v; };
  const api = { saveLayout: rec('saveLayout', 77), getLayouts: rec('getLayouts', []), saveVU: rec('saveVU', 5), deleteVU: rec('deleteVU'), getLinks: rec('getLinks', []), linkVu: rec('linkVu'), unlinkVu: rec('unlinkVu', true) };
  await Sync.run(api, { styleId: 2, existingEdit: false, name: 'D', asStyle: false, boxes: [T(1, 5, 5, 100, 20), T(2, 5, 25, 100, 40)], links: [[1, 2]], dry: true });
  assert.ok(calls.indexOf('deleteVU') > calls.lastIndexOf('saveVU'), 'default box is deleted after all saves');
  assert.ok(!calls.includes('getLayouts'), 'dry run does not read back');
});

test('a failed save of a NEW layout removes the half-made layout', async () => {
  const api = fakeOmnia();
  let n = 0; const real = api.saveVU;
  api.saveVU = async (...a) => { if (++n === 5) throw new Error('SaveVU was refused by OMNIA: "boom"'); return real(...a); };
  await assert.rejects(() => Sync.run(api, { styleId: 2, existingEdit: false, name: 'X', asStyle: true, boxes: travel(), links: [], dry: false }), (e) => e.rolledBack === true);
  assert.strictEqual(api.layouts.size, 0, 'no layout left behind');
});

test('a failed edit of an EXISTING layout never deletes that layout', async () => {
  const api = fakeOmnia();
  const lid = await Sync.run(api, { styleId: 2, existingEdit: false, name: 'K', asStyle: false, boxes: [T(1, 5, 5, 100, 20), T(2, 5, 25, 100, 40)], links: [], dry: false });
  api.saveVU = async () => { throw new Error('SaveVU was refused by OMNIA: "locked"'); };
  await assert.rejects(() => Sync.run(api, { styleId: 2, existingEdit: true, layoutId: lid, name: 'K', asStyle: false, boxes: [T(11, 6, 5, 100, 20, 'Text', 2), T(12, 5, 25, 100, 40, 'Text', 3)], links: [], linkPairs: [], dry: false }), (e) => !e.rolledBack);
  assert.ok(api.layouts.has(lid));
});

test('a leftover default box is reported and the layout rolled back', async () => {
  const api = fakeOmnia();
  api.deleteVU = async () => {}; // OMNIA silently keeps the default pictogram
  await assert.rejects(() => Sync.run(api, { styleId: 2, existingEdit: false, name: 'Y', asStyle: true, boxes: travel(), links: [], dry: false }), /still has 1 box/);
});
