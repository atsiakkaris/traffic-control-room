"""
routes_page.py - the Control Room's "Routes" tab: live travel times of Bluetooth paths added along a route.

The page itself only draws and asks the server (/api/routes/chain, /api/routes/plan); the adding and the route search are in
runner/route_times.py. Only live readings are used, and only existing OMNIA nodes can be picked as start and end.
"""
import json

ROUTES_PAGE = r"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><title>Routes</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<script src="https://unpkg.com/leaflet-polylinedecorator@1.6.0/dist/leaflet.polylineDecorator.js"></script>
<style>
html,body{margin:0;height:100%;background:#0f1720;color:#cfd8dc;font:13px Arial,sans-serif}
#wrap{display:flex;height:100%}
#side{width:390px;flex:none;overflow:auto;padding:14px;box-sizing:border-box;background:#16212b;border-right:1px solid #243342}
#map{flex:1}
h2{margin:0 0 4px;font-size:17px;color:#fff}
.sm{color:#8fa3b3;font-size:12px;line-height:1.45;margin:4px 0 10px}
.feed{padding:7px 10px;border-radius:6px;background:#1d2d3a;margin:8px 0;font-size:12px}
.feed.bad{background:#5a1d1d;color:#ffd7d7}
.seg{display:flex;margin:10px 0}
.seg button{flex:1;padding:8px;border:1px solid #2e4358;background:#1b2a37;color:#cfd8dc;cursor:pointer;font:13px Arial}
.seg button:first-child{border-radius:6px 0 0 6px}.seg button:last-child{border-radius:0 6px 6px 0}
.seg button.on{background:#2e86c1;color:#fff;border-color:#2e86c1}
label{display:block;margin:8px 0 3px;color:#8fa3b3;font-size:12px}
select{width:100%;box-sizing:border-box;padding:8px;border-radius:6px;border:1px solid #2e4358;background:#0f1720;color:#fff;font:13px Arial}
input{width:100%;box-sizing:border-box;padding:8px;border-radius:6px;border:1px solid #2e4358;background:#0f1720;color:#fff;font:13px Arial}
button.act{background:#2e86c1;color:#fff;border:0;border-radius:6px;padding:8px 14px;cursor:pointer;font:13px Arial;margin:8px 6px 0 0}
button.act.sec{background:#34495e}
button.act:hover{filter:brightness(1.15)}
#out{margin-top:12px}
.total{font-size:26px;font-weight:700;color:#fff;margin:2px 0}
.card{border:1px solid #2e4358;border-radius:8px;padding:9px 11px;margin:8px 0;cursor:pointer;background:#1b2a37}
.card.on{border-color:#facc15;background:#22323f}
table{width:100%;border-collapse:collapse;margin-top:8px}
td,th{padding:5px 4px;border-bottom:1px solid #243342;text-align:left;vertical-align:top}
th{color:#8fa3b3;font-weight:normal;font-size:12px}
td.n{text-align:right;white-space:nowrap}
.warn{background:#4b3a12;color:#ffe9a8;border-radius:6px;padding:7px 10px;margin:6px 0;font-size:12px;line-height:1.4}
.chip{display:inline-block;margin:3px 4px 0 0;padding:5px 9px;border-radius:999px;background:#243342;color:#cfd8dc;cursor:pointer;font-size:12px;border:0}
.chip:hover{background:#34495e}
.chip.on{background:#2e86c1;color:#fff}
.x{background:none;border:0;color:#e57373;cursor:pointer;font-size:15px}
.ep{width:22px;height:22px;border-radius:50%;background:#1a1a2e;color:#fff;border:2px solid #fff;text-align:center;line-height:18px;font-weight:700;font-size:11px}
.ep.a{background:#1d9e75}
[hidden]{display:none!important}
</style></head><body>
<div id="wrap"><div id="side">
<h2>Route times</h2>
<div class="sm">Adds up the <b>live</b> travel times of Bluetooth paths. Only existing nodes can be picked, and a path with no live reading is never guessed: it is left out and called out.</div>
<div id="feed" class="feed">Reading the live feed...</div>
<div class="seg"><button id="m-ab" class="on">A to B</button><button id="m-ch">Add paths</button><button id="m-gp">Gaps</button></div>
<div id="ab">
  <label for="from">From (a node, or click one on the map)</label><input id="from" list="siteList" autocomplete="off" placeholder="Type a node name">
  <label for="to">To</label><input id="to" list="siteList" autocomplete="off" placeholder="Type a node name">
  <button class="act" id="go">Find fastest route</button><button class="act sec" id="swap">Swap A and B</button>
</div>
<div id="ch" hidden>
  <div class="sm">Type a path or click it on the map. Paths must join: each one starts where the one before ends.</div>
  <input id="addp" list="pathList" autocomplete="off" placeholder="Path ID or name">
  <button class="act" id="add">Add path</button><button class="act sec" id="clr">Clear</button>
  <div id="next"></div>
</div>
<div id="gp" hidden>
  <div class="sm">Nodes close together whose chain of paths is much longer than the straight line, or does not exist: a path may be missing there. Lengths only, live readings do not matter. Worst first.</div>
  <div style="display:flex;gap:10px"><div style="flex:1"><label for="gr">Within (km)</label><input id="gr" type="number" min="0.5" max="10" step="0.5" value="3"></div>
  <div style="flex:1"><label for="gx">Detour of at least</label><input id="gx" type="number" min="1.5" max="20" step="0.5" value="3"></div>
  <div style="flex:1"><label for="gm" title="Nodes closer than this are one place: a replacement sensor beside the old one. They are never listed against each other and a chain may pass from one to the other.">Same place (m)</label><input id="gm" type="number" min="0" max="1000" step="25" value="150"></div></div>
  <label for="gsort">Order</label><select id="gsort"><option value="worst">Worst first (no chain, then biggest detour)</option><option value="close">Closest nodes first</option></select>
  <button class="act" id="gfind">Find gaps</button><button class="act sec" id="gcsv">Save list as CSV</button>
  <div class="sm" id="gstat"></div>
  <div id="gchips"></div>
</div>
<div id="upd" class="sm"></div>
<div id="out"></div>
<div class="sm" id="foot"></div>
</div><div id="map"></div></div>
<datalist id="siteList"></datalist><datalist id="pathList"></datalist>
<script>
(function(){
var D = __DATA__;
var mode = 'ab', A = null, B = null, chain = [], routes = [], pick = 0, measured = null, last = null;
function el(tag, cls, text){ var e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; }
function $(id){ return document.getElementById(id); }
function fmt(s){ if (s == null) return '-'; s = Math.round(s); if (s >= 3600) return Math.floor(s / 3600) + ' h ' + ('0' + Math.round((s % 3600) / 60)).slice(-2) + ' min'; return Math.floor(s / 60) + ' min ' + ('0' + (s % 60)).slice(-2) + ' s'; }
function km(m){ return (m / 1000).toFixed(1) + ' km'; }
function clock(d){ var ms = Date.parse(d); if (isNaN(ms)) return '-'; return new Date(ms).toLocaleTimeString('en-GB', {timeZone:'Asia/Nicosia', hour:'2-digit', minute:'2-digit'}); }
function api(url){ return fetch(url, {cache:'no-store'}).then(function(r){ return r.json().then(function(j){ if (!r.ok) throw new Error(j.error || ('HTTP ' + r.status)); return j; }); }); }

if (!D.has_network){
  $('side').appendChild(el('div', 'warn', 'There is no saved OMNIA data on this PC yet. Open the Wiring tab and press "Fetch from OMNIA" first: the routes are built from the nodes each path starts and ends at.'));
  $('ab').hidden = true; $('feed').hidden = true; document.querySelector('.seg').hidden = true;
  return;
}

/* ---- pickers ---- */
var siteLabel = {}, siteByLabel = {}, pathLabel = {}, pathByLabel = {};
(function(){
  var seen = {}; Object.keys(D.sites).forEach(function(s){ seen[D.sites[s].name] = (seen[D.sites[s].name] || 0) + 1; });
  var dl = $('siteList');
  Object.keys(D.sites).sort(function(a, b){ return D.sites[a].name < D.sites[b].name ? -1 : 1; }).forEach(function(s){
    var n = D.sites[s].name, lab = seen[n] > 1 ? n + ' (node ' + s + ')' : n;
    siteLabel[s] = lab; siteByLabel[lab.toLowerCase()] = s; var o = document.createElement('option'); o.value = lab; dl.appendChild(o);
  });
  var pl = $('pathList');
  Object.keys(D.paths).sort(function(a, b){ return (+a) - (+b); }).forEach(function(id){
    var lab = id + ' - ' + D.paths[id].name; pathLabel[id] = lab; pathByLabel[lab.toLowerCase()] = id; var o = document.createElement('option'); o.value = lab; pl.appendChild(o);
  });
})();

/* ---- map ---- */
var map = L.map('map', {zoomControl:true, doubleClickZoom:false}).setView([34.95, 33.15], 9);   // no double-click zoom: its two clicks would clear the selection
L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {attribution:'&copy; OpenStreetMap contributors', maxZoom:19}).addTo(map);
var baseLines = {}, routeLayer = L.layerGroup().addTo(map), siteMarkers = {}, pickMarks = L.layerGroup().addTo(map);

function mPerDeg(lat){ return [110574, 111320 * Math.cos(lat * Math.PI / 180)]; }
function distSeg(p, a, b){
  var k = mPerDeg(p[0]), ax = (a[1] - p[1]) * k[1], ay = (a[0] - p[0]) * k[0], bx = (b[1] - p[1]) * k[1], by = (b[0] - p[0]) * k[0];
  var dx = bx - ax, dy = by - ay, l2 = dx * dx + dy * dy, t = l2 ? Math.max(0, Math.min(1, -(ax * dx + ay * dy) / l2)) : 0;
  return Math.hypot(ax + dx * t, ay + dy * t);
}
function pathsAt(ll){
  var p = [ll.lat, ll.lng], tol = Math.max(15, 6 * 40075016 * Math.cos(ll.lat * Math.PI / 180) / (256 * Math.pow(2, map.getZoom()))), out = [];
  Object.keys(D.paths).forEach(function(id){
    var c = D.paths[id].coords; for (var i = 0; i < c.length - 1; i++){ if (distSeg(p, c[i], c[i + 1]) <= tol){ out.push(id); return; } }
  });
  return out;
}
Object.keys(D.paths).forEach(function(id){
  var pth = D.paths[id]; if (pth.coords.length < 2) return;
  var l = L.polyline(pth.coords, {color:'#7f8c9a', weight:3, opacity:0.55, bubblingMouseEvents:false}).addTo(map);
  l.bindTooltip(pth.name + ' (path ' + id + ')', {sticky:true});
  l.on('click', function(e){ if (mode === 'ch') addFromMap(e.latlng); });
  baseLines[id] = l;
});
Object.keys(D.sites).forEach(function(s){
  var st = D.sites[s], m = L.circleMarker([st.lat, st.lon], {radius:5, weight:2, color:'#fff', fillColor:'#2e86c1', fillOpacity:1, bubblingMouseEvents:false}).addTo(map);
  m.bindTooltip(siteLabel[s]);
  m.on('click', function(){ if (mode === 'ab') pickSite(s); });
  siteMarkers[s] = m;
});
function epIcon(letter, cls){ return L.divIcon({className:'', html:'<div class="ep ' + cls + '">' + letter + '</div>', iconSize:[22, 22], iconAnchor:[11, 11]}); }
function drawPicks(){
  pickMarks.clearLayers();
  if (A && D.sites[A]) L.marker([D.sites[A].lat, D.sites[A].lon], {icon:epIcon('A', 'a'), interactive:false}).addTo(pickMarks);
  if (B && D.sites[B]) L.marker([D.sites[B].lat, D.sites[B].lon], {icon:epIcon('B', ''), interactive:false}).addTo(pickMarks);
}
function drawRoute(ids, keepView){
  routeLayer.clearLayers(); var pts = [];
  ids.forEach(function(id){
    var c = D.paths[id] && D.paths[id].coords; if (!c || c.length < 2) return;
    var l = L.polyline(c, {color:'#facc15', weight:7, opacity:0.95, interactive:false}).addTo(routeLayer);
    if (L.polylineDecorator && L.Symbol) L.polylineDecorator(l, {patterns:[{offset:15, repeat:60, symbol:L.Symbol.arrowHead({pixelSize:10, headAngle:40, pathOptions:{color:'#222', fillColor:'#222', fillOpacity:0.9, weight:0, interactive:false}})}]}).addTo(routeLayer);
    pts = pts.concat(c);
  });
  if (pts.length && !keepView) map.fitBounds(L.latLngBounds(pts), {padding:[40, 40], maxZoom:16});
}

/* ---- feed line ---- */
function drawFeed(m){
  if (m !== undefined) measured = m;
  var f = $('feed'), age = measured ? Math.round((Date.now() - Date.parse(measured)) / 60000) : null;
  f.className = 'feed' + (age != null && age > 20 ? ' bad' : '');
  f.textContent = measured ? 'Live feed measured ' + clock(measured) + ' (' + (age < 1 ? 'just now' : age + ' min ago') + ')' + (age > 20 ? ' - OLD: these times are not current' : '') : 'Live feed time unknown';
}
$('foot').textContent = 'Nodes and paths from the OMNIA data saved ' + (D.fetched_at ? D.fetched_at.slice(0, 16).replace('T', ' ') + ' (Wiring tab, Fetch from OMNIA)' : '(unknown)') + '. Look-alike nodes with the same name close together count as one place.' +
  (D.unlinked.length ? ' ' + D.unlinked.length + ' drawn paths have no start or end node in OMNIA yet (' + D.unlinked.slice(0, 12).join(', ') + (D.unlinked.length > 12 ? ', ...' : '') + '), so they cannot be used here.' : '');

/* ---- A to B ---- */
function setSite(which, s){
  if (which === 'A') A = s; else B = s;
  $(which === 'A' ? 'from' : 'to').value = s ? siteLabel[s] : '';
  drawPicks();
}
function pickSite(s){
  if (A && !B && s !== A){ setSite('B', s); plan(); return; }
  setSite('A', s); setSite('B', null); routes = []; $('out').textContent = ''; routeLayer.clearLayers(); stamp('');
}
function fromInput(which){
  var v = $(which === 'A' ? 'from' : 'to').value.trim().toLowerCase();
  setSite(which, siteByLabel[v] || null);
}
$('from').onchange = function(){ fromInput('A'); if (A && B) plan(); };
$('to').onchange = function(){ fromInput('B'); if (A && B) plan(); };
$('swap').onclick = function(){ var a = A, b = B; setSite('A', b); setSite('B', a); if (A && B) plan(); };
$('go').onclick = function(){ fromInput('A'); fromInput('B'); plan(); };
function sig(r){ return r.legs.map(function(l){ return l.id; }).join(','); }
// auto = the 60-second refresh: same route stays selected, the map keeps its view, an error leaves the old numbers on screen.
function plan(auto){
  var out = $('out');
  if (!A || !B){ out.textContent = ''; out.appendChild(el('div', 'warn', 'Pick both nodes from the list (or click two nodes on the map).')); return; }
  if (!auto){ out.textContent = ''; out.appendChild(el('div', 'sm', 'Looking for the fastest route...')); }
  var keep = auto && routes[pick] ? sig(routes[pick]) : null;
  return api('/api/routes/plan?from=' + encodeURIComponent(A) + '&to=' + encodeURIComponent(B)).then(function(j){
    drawFeed(j.measured); routes = j.routes || []; pick = 0; out.textContent = '';
    routes.forEach(function(r, i){ if (keep && sig(r) === keep) pick = i; });
    stamp();
    if (!routes.length){
      out.appendChild(el('div', 'warn', j.note || 'No route found.'));
      if (j.gap) drawRoute(j.gap.legs.map(function(l){ return l.id; }), auto);
      return;
    }
    routes.forEach(function(r, i){
      var c = el('div', 'card' + (i === pick ? ' on' : ''));
      c.appendChild(el('div', 'total', fmt(r.seconds)));
      c.appendChild(el('div', 'sm', (i === 0 ? 'Fastest' : 'Alternative ' + i) + ' - ' + km(r.length_m) + ', ' + r.legs.length + (r.legs.length === 1 ? ' path' : ' paths') + (r.kmh ? ', about ' + r.kmh + ' km/h' : '')));
      c.onclick = function(){ pick = i; showRoutes(); };
      out.appendChild(c);
    });
    out.appendChild(el('div', null)).id = 'detail';
    showRoutes(auto);
  }).catch(function(e){
    if (auto){ stamp('Could not refresh (' + e.message + '): these are the values from ' + (updated ? clockNow(updated) : 'before')); return; }
    out.textContent = ''; out.appendChild(el('div', 'warn', 'Could not get the live times: ' + e.message));
  });
}
function legsTable(legs, removable){
  var t = el('table'), h = el('tr'); ['', 'Path', 'Live time'].forEach(function(x){ h.appendChild(el('th', null, x)); }); if (removable) h.appendChild(el('th')); t.appendChild(h);
  legs.forEach(function(l, i){
    var r = el('tr'); r.appendChild(el('td', null, String(i + 1)));
    var c = el('td'); c.appendChild(el('b', null, l.id)); c.appendChild(document.createTextNode(' ' + l.name)); r.appendChild(c);
    r.appendChild(el('td', 'n', l.seconds == null ? 'no reading' : fmt(l.seconds)));
    if (removable){ var x = el('td'); var b = el('button', 'x', '×'); b.title = 'Remove this path'; b.onclick = function(){ chain.splice(i, 1); runChain(); }; x.appendChild(b); r.appendChild(x); }
    t.appendChild(r);
  });
  return t;
}
function showRoutes(keepView){
  Array.prototype.forEach.call(document.querySelectorAll('#out .card'), function(c, i){ c.className = 'card' + (i === pick ? ' on' : ''); });
  var d = $('detail'); if (!d || !routes[pick]) return; d.textContent = '';
  d.appendChild(legsTable(routes[pick].legs, false)); drawRoute(routes[pick].legs.map(function(l){ return l.id; }), keepView);
}

/* ---- add paths ---- */
function addPath(id){ if (!D.paths[id]) return; chain.push(id); runChain(); }
function addFromMap(ll){
  var here = pathsAt(ll); if (!here.length) return;
  var lastB = chain.length ? D.paths[chain[chain.length - 1]].b : null;
  var id = here.filter(function(p){ return lastB != null && D.paths[p].a === lastB && chain.indexOf(p) < 0; })[0] || here.filter(function(p){ return chain.indexOf(p) < 0; })[0];
  if (id) addPath(id);
}
$('add').onclick = function(){
  var v = $('addp').value.trim().toLowerCase(), id = pathByLabel[v] || (D.paths[v] ? v : null);
  if (id){ addPath(id); $('addp').value = ''; } else $('out').appendChild(el('div', 'warn', 'That is not a path in the list.'));
};
$('addp').onkeydown = function(e){ if (e.key === 'Enter') $('add').onclick(); };
$('clr').onclick = function(){ chain = []; runChain(); };
function runChain(auto){
  var out = $('out'), nx = $('next');
  if (!auto){ out.textContent = ''; nx.textContent = ''; }
  if (!chain.length){ out.textContent = ''; nx.textContent = ''; routeLayer.clearLayers(); stamp(''); return; }
  return api('/api/routes/chain?ids=' + chain.join(',')).then(function(j){
    out.textContent = ''; nx.textContent = ''; stamp();
    drawFeed(j.measured); var r = j.result;
    out.appendChild(el('div', 'sm', r.complete ? 'Total live time' : 'Total live time (see the notes)'));
    out.appendChild(el('div', 'total', fmt(r.seconds)));
    out.appendChild(el('div', 'sm', km(r.length_m) + ', ' + r.legs.length + (r.legs.length === 1 ? ' path' : ' paths') + (r.kmh ? ', about ' + r.kmh + ' km/h' : '')));
    r.problems.forEach(function(p){ out.appendChild(el('div', 'warn', p)); });
    out.appendChild(legsTable(r.legs, true));
    drawRoute(chain, auto);
    if (j.next && j.next.length){
      nx.appendChild(el('div', 'sm', 'Continue with (starts where the last path ends):'));
      j.next.forEach(function(l){
        var c = el('button', 'chip', l.id + ' ' + l.name + ' - ' + (l.seconds == null ? 'no reading' : fmt(l.seconds))); c.onclick = function(){ addPath(l.id); }; nx.appendChild(c);
      });
    }
  }).catch(function(e){
    if (auto){ stamp('Could not refresh (' + e.message + '): these are the values from ' + (updated ? clockNow(updated) : 'before')); return; }
    out.appendChild(el('div', 'warn', 'Could not get the live times: ' + e.message));
  });
}

/* ---- refresh by itself, every minute ---- */
var updated = null, REFRESH_MS = 60000, refreshing = false;
function clockNow(ms){ return new Date(ms).toLocaleTimeString('en-GB', {timeZone:'Asia/Nicosia', hour:'2-digit', minute:'2-digit', second:'2-digit'}); }
// stamp() = a fresh answer just arrived; stamp(text) = show that text instead (an error, or '' to clear).
function stamp(text){
  var u = $('upd');
  if (text === undefined){ updated = Date.now(); u.className = 'sm'; u.textContent = 'Updated ' + clockNow(updated) + ' - refreshes by itself every minute'; }
  else { u.className = text ? 'warn' : 'sm'; u.textContent = text; }
}
function autoRefresh(){
  if (refreshing || document.hidden) return;
  var p = mode === 'ab' ? (A && B && routes.length ? plan(true) : null) : (mode === 'ch' && chain.length ? runChain(true) : null);   // Gaps use lengths, not live times: nothing to refresh
  if (!p) return;
  refreshing = true;
  p.then(function(){ refreshing = false; }, function(){ refreshing = false; });
}
setInterval(autoRefresh, REFRESH_MS);
document.addEventListener('visibilitychange', function(){ if (!document.hidden && updated && Date.now() - updated >= REFRESH_MS) autoRefresh(); });

// A click on empty map clears the selection (clicks on a node or a path are handled by them and do not reach the map).
map.on('click', function(){
  if (mode === 'ab'){
    if (!A && !B) return;
    setSite('A', null); setSite('B', null); routes = []; $('out').textContent = ''; routeLayer.clearLayers(); stamp('');
  } else if (mode === 'gp'){
    if (gsel) { gsel = null; gapLayer.clearLayers(); drawGaps(); }
  } else if (chain.length){ chain = []; runChain(); }
});

/* ---- gaps: nearby nodes the paths do not join sensibly ---- */
var gaps = null, gsel = null, gfilter = {none:true, detour:true}, gapLayer = L.layerGroup().addTo(map);
function gname(s){ return D.sites[s] ? D.sites[s].name : String(s); }
function gkm(m){ return (m / 1000).toFixed(1) + ' km'; }
function loadGaps(){
  var out = $('out'); out.textContent = ''; $('gstat').textContent = ''; out.appendChild(el('div', 'sm', 'Looking for gaps...'));
  api('/api/routes/gaps?radius=' + encodeURIComponent($('gr').value) + '&ratio=' + encodeURIComponent($('gx').value) + '&same=' + encodeURIComponent($('gm').value)).then(function(j){
    gaps = j; gsel = null; gapLayer.clearLayers(); drawGaps();
  }).catch(function(e){ out.textContent = ''; out.appendChild(el('div', 'warn', 'Could not look for gaps: ' + e.message)); });
}
function gapRows(){
  var rows = gaps.rows.filter(function(r){ return r.ratio === null ? gfilter.none : gfilter.detour; });
  if ($('gsort').value === 'close') rows = rows.slice().sort(function(x, y){ return x.straight_m - y.straight_m; });   // else the server's order: no chain first, then biggest detour
  return rows;
}
function drawGaps(){
  var out = $('out'), ch = $('gchips'); out.textContent = ''; ch.textContent = '';
  [['none', 'No chain', gaps.none], ['detour', 'Detour', gaps.detour]].forEach(function(k){
    var b = el('button', 'chip' + (gfilter[k[0]] ? ' on' : ''), k[1] + ' ' + k[2]);
    b.onclick = function(){ gfilter[k[0]] = !gfilter[k[0]]; gsel = null; gapLayer.clearLayers(); drawGaps(); };
    ch.appendChild(b);
  });
  var rows = gapRows();
  if (!rows.length) out.appendChild(el('div', 'warn', gaps.total ? 'Nothing to show with these filters.' : 'No gaps found with these settings.'));
  if (gaps.total > gaps.rows.length) out.appendChild(el('div', 'sm', 'Showing the worst ' + gaps.rows.length + ' of ' + gaps.total + '. Narrow the distance or raise the detour to see fewer.'));
  rows.forEach(function(r){
    var c = el('div', 'card' + (gsel === r ? ' on' : '')), top = el('div'), mid = el('div'), sub = el('div', 'sm');
    top.style.cssText = 'display:flex;justify-content:space-between;align-items:baseline;gap:8px';
    var lead = el('b', null, r.ratio === null ? 'No chain' : r.ratio.toFixed(1) + 'x detour'); if (r.ratio === null) lead.style.color = '#ff9b9b';
    top.appendChild(lead); top.appendChild(el('span', 'sm', gkm(r.straight_m) + (r.chain_m === null ? ' straight' : ' vs ' + gkm(r.chain_m))));
    mid.textContent = r.a_names.join(' / ') + ' to ' + r.b_names.join(' / ');   // a place with a replacement sensor shows both names
    sub.textContent = (r.chain_m === null ? 'No chain of paths from the first to the second' :
      r.legs.length === 1 ? 'One winding path' : r.legs.length + ' paths, through ' + gname(D.paths[r.legs[Math.floor(r.legs.length / 2)]].a)) +
      '. The other way: ' + (r.back.chain_m === null ? 'no chain' : gkm(r.back.chain_m)) + '.';
    c.appendChild(top); c.appendChild(mid); c.appendChild(sub);
    c.onclick = function(){ gsel = r; drawGaps(); showGap(r); };
    out.appendChild(c);
  });
}
function showGap(r){
  gapLayer.clearLayers(); var pts = [], a = D.sites[r.a], b = D.sites[r.b];
  r.legs.forEach(function(id){
    var c = D.paths[id] && D.paths[id].coords; if (!c || c.length < 2) return;
    var l = L.polyline(c, {color:'#EF9F27', weight:6, opacity:0.95, interactive:false}).addTo(gapLayer);
    if (L.polylineDecorator && L.Symbol) L.polylineDecorator(l, {patterns:[{offset:15, repeat:60, symbol:L.Symbol.arrowHead({pixelSize:10, headAngle:40, pathOptions:{color:'#222', fillColor:'#222', fillOpacity:0.9, weight:0, interactive:false}})}]}).addTo(gapLayer);
    pts = pts.concat(c);
  });
  L.polyline([[a.lat, a.lon], [b.lat, b.lon]], {color:'#E24B4A', weight:3, dashArray:'7 5', interactive:false}).addTo(gapLayer);
  L.marker([a.lat, a.lon], {icon:epIcon('A', 'a'), interactive:false}).addTo(gapLayer);
  L.marker([b.lat, b.lon], {icon:epIcon('B', ''), interactive:false}).addTo(gapLayer);
  map.fitBounds(L.latLngBounds(pts.concat([[a.lat, a.lon], [b.lat, b.lon]])), {padding:[50, 50], maxZoom:16});
}
$('gfind').onclick = loadGaps;
$('gsort').onchange = function(){ if (gaps){ gsel = null; gapLayer.clearLayers(); drawGaps(); } };
$('gcsv').onclick = function(){
  if (!gaps) return;
  var rows = [['From', 'To', 'Straight line (km)', 'Chain of paths (km)', 'Times longer', 'Paths in the chain', 'Other direction chain (km)']];
  gapRows().forEach(function(r){
    rows.push([r.a_names.join(' / '), r.b_names.join(' / '),(r.straight_m / 1000).toFixed(2), r.chain_m === null ? 'none' : (r.chain_m / 1000).toFixed(2),
               r.ratio === null ? 'none' : r.ratio, r.legs.join(' '), r.back.chain_m === null ? 'none' : (r.back.chain_m / 1000).toFixed(2)]);
  });
  var st = $('gstat'); st.textContent = 'Choose where to save in the dialog that opened (it may be behind this window)...';
  fetch('/api/save-csv', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({name:'routes_gaps', rows:rows})})
    .then(function(r){ return r.json(); })
    .then(function(r){ st.textContent = r.cancelled ? 'Cancelled - nothing saved.' : r.path ? 'Saved to ' + r.path + (r.fallback ? ' (no Save dialog available, so the Downloads folder was used)' : '') : 'Could not save: ' + (r.error || 'unknown error'); })
    .catch(function(e){ st.textContent = 'Could not save: ' + e; });
};

/* ---- mode ---- */
function setMode(m){
  mode = m;
  ['ab', 'ch', 'gp'].forEach(function(k){ $('m-' + k).className = m === k ? 'on' : ''; $(k).hidden = m !== k; });
  $('out').textContent = ''; routeLayer.clearLayers(); gapLayer.clearLayers(); stamp(''); $('feed').hidden = m === 'gp';
  if (m === 'ab') drawPicks(); else pickMarks.clearLayers();
  if (m === 'ch') runChain();
  else if (m === 'ab'){ if (A && B) plan(); }
  else if (gaps){ gsel = null; drawGaps(); } else loadGaps();
}
$('m-ab').onclick = function(){ setMode('ab'); };
$('m-ch').onclick = function(){ setMode('ch'); };
$('m-gp').onclick = function(){ setMode('gp'); };
setInterval(function(){ drawFeed(); }, 30000);
api('/api/routes/chain?ids=').then(function(j){ drawFeed(j.measured); }).catch(function(e){ $('feed').className = 'feed bad'; $('feed').textContent = 'Could not read the live feed: ' + e.message; });
})();
</script></body></html>"""


def build_routes_page(network, fetched_at=None):
    """The Routes tab. network: route_times.build_network(...) or None when there is no saved OMNIA data."""
    data = {"has_network": bool(network and network["paths"]), "fetched_at": fetched_at or "",
            "sites": (network or {}).get("sites", {}), "paths": (network or {}).get("paths", {}),
            "unlinked": (network or {}).get("unlinked", [])}
    return ROUTES_PAGE.replace("__DATA__", json.dumps(data).replace("</", "<\\/"))
