// OMNIA VmsDataService calls, as captured from the Layout/Style Designer.
// Everything is a GET on one handler. Runs in the OMNIA page, so it uses the
// logged-in session (same origin) and never touches credentials.
//
// Write calls honour dry-run mode: they are recorded and not sent.
const Api = (function () {
  // The tool can run in a new tab opened from the OMNIA page (about:blank, same origin),
  // so build absolute URLs from the OMNIA origin instead of relying on the page URL.
  const ORIGIN = (function () {
    try {
      if (/^https?:$/.test(location.protocol)) return location.origin;
      if (window.opener && /^https?:$/.test(window.opener.location.protocol)) return window.opener.location.origin;
    } catch (e) { /* opener is on another origin */ }
    return '';
  })();
  const BASE = ORIGIN + '/VmsDataService/VmsDataServiceHandler.ashx';
  const state = { dry: false, log: [], fakeId: 9000, writes: 0 }; // writes = changes OMNIA accepted
  const WRITES = new Set(['SaveLayout', 'SaveVU', 'DeleteVU', 'DeleteLinkOnDeleteVu', 'DeleteLayout', 'LinkVu', 'DeleteLinkVu']);

  function url(fn, params) {
    const q = new URLSearchParams();
    q.set('function', fn);
    for (const [k, v] of Object.entries(params || {})) {
      q.set(k, typeof v === 'object' && v !== null ? JSON.stringify(v) : String(v));
    }
    q.set('_dc', String(Date.now()));
    return BASE + '?' + q.toString();
  }

  async function call(fn, params) {
    const u = url(fn, params);
    if (WRITES.has(fn) && state.dry) {
      state.log.push({ fn, params, dry: true });
      if (fn === 'SaveLayout' || fn === 'SaveVU') return String(++state.fakeId);
      return '';
    }
    const res = await fetch(u, { credentials: 'same-origin' });
    const text = await res.text();
    state.log.push({ fn, params, status: res.status, reply: text.slice(0, 120) });
    if (!res.ok) throw new Error(`${fn} failed: HTTP ${res.status}`);
    // SaveLayout / SaveVU answer with an id when they worked and with a message when refused
    if (WRITES.has(fn) && (fn !== 'SaveLayout' && fn !== 'SaveVU' ? true : /^\d+$/.test(text.trim()))) state.writes += 1;
    return text;
  }

  const json = async (fn, params) => {
    const text = await call(fn, params);
    try { return JSON.parse(text); } catch (e) {
      throw new Error(`${fn} did not return data (session expired? you may need to log in to OMNIA again). Reply began: "${text.slice(0, 80).replace(/\s+/g, ' ')}"`);
    }
  };
  const numeric = (fn, text) => {
    if (!/^\d+$/.test(String(text).trim())) throw new Error(`${fn} was refused by OMNIA: "${String(text).slice(0, 200)}"`);
    return parseInt(text, 10);
  };

  const kids = (n) => (n && n.children) || [];

  // [{ id, name, W, H }] — only styles that have a drawable (RGB) module.
  async function getStyles() {
    const tree = await json('GetStyles');
    return kids(tree)
      .map((s) => {
        const lu = kids(s)[0];
        return lu && lu.EndX ? { id: s.mainId, name: s.Name, W: lu.EndX, H: lu.EndY, address: lu.lUnitId || '01' } : null;
      })
      .filter(Boolean);
  }

  // [{ id, name, asStyle, isDefault, address, boxes: [{ vid, type, x1, y1, x2, y2, raw }] }]
  async function getLayouts(styleId) {
    const tree = await json('GetLayouts', { styleId });
    return kids(tree).map((l) => {
      const lu = kids(l)[0];
      return {
        id: l.mainId,
        name: l.Name,
        asStyle: !!l.useLayoutAsStyle,
        isDefault: !!l.isLayoutDefault,
        address: (lu && lu.lUnitId) || '01',
        boxes: kids(lu).map((v) => ({
          vid: parseInt(v.vUnitId, 10),
          type: (v.custom && v.custom.Type) || 'Text',
          x1: v.StartX, y1: v.StartY, x2: v.EndX, y2: v.EndY,
          raw: v,
        })),
      };
    });
  }

  async function getLinks(layoutId, address, vid) {
    return json('GetLinks', { layoutID: layoutId, logicalUnitID: address, virtualUnitID: vid });
  }

  async function saveLayout({ id, name, styleId, asStyle }) {
    const t = await call('SaveLayout', {
      layoutId: id, layoutName: name, styleId, layoutAsStyle: !!asStyle, isLaneManagement: false, isLayoutDefault: false,
    });
    return numeric('SaveLayout', t);
  }

  const TEMPLATE = {
    Name: null, useLayoutAsStyle: false, isLaneManagement: false, isLayoutDefault: false, objectTypeId: 0, Width: null, Height: null,
    custom: { Type: 'Text', FontName: null, FontSize: 0, Chars: 0, AbsStartX: 0, AbsStartY: 0, LinkedVuIDs: [] },
    leaf: true, children: null,
  };

  // box: { vid (null = new), type, x1..y2, raw? }. Returns the VU id.
  async function saveVU(layoutId, address, styleId, box) {
    const base = box.raw || TEMPLATE;
    const vunit = Object.assign({}, base, {
      mainId: layoutId, lUnitId: address, vUnitId: String(box.vid == null ? -1 : box.vid),
      Name: null, StartX: box.x1, StartY: box.y1, EndX: box.x2, EndY: box.y2, leaf: true, children: null,
    });
    vunit.custom = Object.assign({}, (base.custom || TEMPLATE.custom), { Type: box.type });
    const t = await call('SaveVU', { vunit, styleId });
    return numeric('SaveVU', t);
  }

  async function deleteVU(layoutId, address, styleId, vid) {
    await call('DeleteVU', { layoutId, address, vUnitId: vid });
    await call('DeleteLinkOnDeleteVu', { styleID: styleId, layoutID: layoutId, logicalUnitID: address, linkVirtualUnitID: vid });
  }

  const deleteLayout = (layoutId) => call('DeleteLayout', { layoutId });

  // Both argument orders have been seen from the designer (4 linked to 2, and 1 linked to 5).
  const linkVu = (styleId, layoutId, address, vid, linkVid) =>
    call('LinkVu', { styleID: styleId, layoutID: layoutId, logicalUnitID: address, virtualUnitID: vid, linkVirtualUnitID: linkVid });

  // Captured: DeleteLinkVu&styleID&layoutID&logicalUnitID&virtualUnitID&linkVirtualUnitID -> "1".
  // GetLinks lists a link from both ends, so the owner/target order is unknown;
  // try a->b first and b->a if OMNIA does not answer "1".
  async function unlinkVu(styleId, layoutId, address, a, b) {
    const send = (v, l) => call('DeleteLinkVu', { styleID: styleId, layoutID: layoutId, logicalUnitID: address, virtualUnitID: v, linkVirtualUnitID: l });
    const r = await send(a, b);
    if (state.dry || String(r).trim() === '1') return true;
    return String(await send(b, a)).trim() === '1';
  }

  return { origin: ORIGIN, state, getStyles, getLayouts, getLinks, saveLayout, saveVU, deleteVU, deleteLayout, linkVu, unlinkVu };
})();
