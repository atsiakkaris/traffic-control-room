// The save sequence, kept separate from the UI so it can be tested against a
// fake OMNIA. Behaviours of the real server this order depends on (observed):
//   - a new layout starts with a default full-size pictogram box, id 1
//   - deleting a box renumbers every higher box down by one
//   - deleting a layout's ONLY box removes its drawing area, and any SaveVU
//     afterwards returns an empty reply
// So: save everything first, delete leftovers last (highest id first), then
// re-read the layout to learn the final ids before creating links.
const Sync = (function () {
  const sameBox = (f, d) => f.type === d.type && f.x1 === d.x1 && f.y1 === d.y1 && f.x2 === d.x2 && f.y2 === d.y2;

  // o: { styleId, address, existingEdit, layoutId, name, asStyle, metaChanged, boxes, links, linkPairs, dry }
  // boxes: [{ uid, vid|null, type, x1, y1, x2, y2, raw }]; links: [[uid, ...]]; linkPairs: [[vidA, vidB]]
  // If saving a NEW layout fails part way, remove the half-made layout again so no empty or
  // incomplete copy (with a stray default pictogram) is left behind. Only ever touches a
  // layout this very call created.
  async function run(api, o) {
    const st = { created: null };
    try {
      return await runInner(api, o, st);
    } catch (e) {
      if (st.created != null && !o.dry) {
        try {
          const cur = (await api.getLayouts(o.styleId)).find((l) => l.id === st.created);
          for (const b of (cur ? cur.boxes : []).slice().sort((p, q) => q.vid - p.vid)) await api.deleteVU(st.created, o.address || '01', o.styleId, b.vid);
          await api.deleteLayout(st.created);
          e.rolledBack = true;
        } catch (e2) { e.rollbackFailed = true; }
      }
      throw e;
    }
  }

  async function runInner(api, o, st) {
    const { styleId, dry, existingEdit } = o;
    const address = o.address || '01';
    let lid, existing;

    if (existingEdit) {
      lid = o.layoutId;
      const cur = (await api.getLayouts(styleId)).find((l) => l.id === lid);
      existing = cur ? cur.boxes : [];
      if (o.metaChanged) await api.saveLayout({ id: lid, name: o.name, styleId, asStyle: o.asStyle });
    } else {
      lid = await api.saveLayout({ id: -1, name: o.name, styleId, asStyle: o.asStyle });
      st.created = lid;
      const cur = dry ? null : (await api.getLayouts(styleId)).find((l) => l.id === lid);
      existing = cur ? cur.boxes : [{ vid: 1, type: 'Graphic', raw: null }];
    }

    const desired = o.boxes.map((b) => ({ ...b }));
    const claimed = new Set();
    if (existingEdit) {
      desired.forEach((d) => { if (d.vid != null && existing.some((e) => e.vid === d.vid)) claimed.add(d.vid); else d.vid = null; });
    } else {
      desired.forEach((d) => { d.vid = null; d.raw = null; });
      // reuse the default pictogram box for the first pictogram, if there is one
      existing.forEach((e) => {
        const d = desired.find((x) => x.vid == null && x.type === e.type);
        if (d) { d.vid = e.vid; d.raw = e.raw || null; claimed.add(e.vid); }
      });
    }

    // 1) links that exist in OMNIA but were removed in the editor (ids still current here)
    const groupIdx = (uid) => (o.links || []).findIndex((g) => g.includes(uid));
    if (existingEdit) {
      for (const [a, b] of o.linkPairs || []) {
        const ba = desired.find((x) => x.vid === a), bb = desired.find((x) => x.vid === b);
        if (!ba || !bb) continue; // a deleted box loses its links when it is deleted
        const ga = groupIdx(ba.uid);
        if (ga >= 0 && ga === groupIdx(bb.uid)) continue;
        if (!(await api.unlinkVu(styleId, lid, address, a, b))) throw new Error(`OMNIA would not remove the link between boxes ${a} and ${b}.`);
      }
    }

    // 2) save every box: existing ones keep their id, new ones are appended
    const idOf = new Map();
    for (const d of desired) idOf.set(d.uid, await api.saveVU(lid, address, styleId, d));

    // 3) delete boxes that are gone, highest id first, only after the saves
    for (const e of existing.filter((x) => !claimed.has(x.vid)).sort((p, q) => q.vid - p.vid)) await api.deleteVU(lid, address, styleId, e.vid);

    // 4) ids after the deletions (they may have shifted)
    const finalOf = new Map();
    if (dry) desired.forEach((d) => finalOf.set(d.uid, idOf.get(d.uid)));
    else {
      const cur = (await api.getLayouts(styleId)).find((l) => l.id === lid);
      const pool = (cur ? cur.boxes : []).slice();
      for (const d of desired) {
        const i = pool.findIndex((f) => sameBox(f, d));
        if (i < 0) throw new Error(`Box saved, but not found again afterwards (${d.type} ${d.x1},${d.y1} → ${d.x2},${d.y2}). Links were not created; check the layout in the designer.`);
        finalOf.set(d.uid, pool[i].vid); pool.splice(i, 1);
      }
      // the layout must now hold exactly the boxes that were asked for
      if (pool.length) throw new Error(`OMNIA still has ${pool.length} box${pool.length > 1 ? 'es' : ''} that should not be there (${pool.map((f) => `${f.type} ${f.x1},${f.y1}→${f.x2},${f.y2}`).join('; ')}).`);
    }

    // 5) links to create
    for (const g of o.links || []) {
      const ids = g.map((u) => finalOf.get(u)).filter((v) => v != null).sort((a, b) => a - b);
      for (let i = 1; i < ids.length; i++) {
        let already = false;
        if (!dry) { try { already = (await api.getLinks(lid, address, ids[i])).some((k) => k.VirtualUnitID === ids[0]); } catch (e) { /* link below */ } }
        if (!already) await api.linkVu(styleId, lid, address, ids[i], ids[0]);
      }
    }
    return lid;
  }

  const api = { run, runInner };
  if (typeof module !== 'undefined' && module.exports) module.exports = api;
  return api;
})();
