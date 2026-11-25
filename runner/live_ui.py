"""
live_ui.py - the Live tab: how the network looks right now and what needs attention first.

A standalone page (docs/live.html, shown as a tab in index.html). It stores nothing:
every 5 minutes the browser reads the public snapshot of the cyprus-live-traffic Worker
and joins it with the history of the last stored run, which is embedded in the page
when the dashboard is built (attention.live_context).

  * Bluetooth paths are judged live: a path with no speed reading is silent.
  * Bluetooth sensors are judged live by triangulation: one that has paths, none of
    which reports right now, is silent. It is an inference, not a measurement.
  * VMS has no live feed, so it is shown as of the last stored check.
  * A path that goes quiet on a regular pattern (e.g. every night) is "often silent":
    listed on its own and never counted as new, so it does not bury real faults.

Everything the browser shows from the data is set through textContent, never innerHTML.
"""
import html
import json

# Public endpoint of the cyprus-live-traffic Worker (the same one its public map app reads).
LIVE_FEED_URL = "https://cyprus-live-traffic.tsiakkaris-andreas.workers.dev/latest.json"
REFRESH_SECONDS = 300

_THEME_HEAD = (
    '<script>try{var t=localStorage.getItem("live-theme");}catch(e){}'
    'document.documentElement.setAttribute("data-theme",t==="light"?"light":"dark");</script>')

_MAP_HEAD = (
    '<link rel="stylesheet" href="https://cdn.jsdelivr.net/npm/@tabler/icons-webfont@2.44.0/tabler-icons.min.css">'
    '<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>'
    '<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>'
    '<link rel="stylesheet" href="https://unpkg.com/maplibre-gl@4.7.1/dist/maplibre-gl.css"/>'
    '<script src="https://unpkg.com/maplibre-gl@4.7.1/dist/maplibre-gl.js"></script>'
    '<script src="https://unpkg.com/@maplibre/maplibre-gl-leaflet@0.0.22/leaflet-maplibre-gl.js"></script>')

_CSS = """
<style>
*,*::before,*::after{box-sizing:border-box}
:root{--bg:#f5f6f8;--surface:#fff;--border:rgba(0,0,0,.1);--text:#1a1a2e;--muted:#6b7280;--subsurf:#f1f0e8;color-scheme:light}
:root[data-theme=dark]{--bg:#111318;--surface:#1c1f26;--border:rgba(255,255,255,.09);--text:#f0f2f5;--muted:#9ca3af;--subsurf:#232730;color-scheme:dark}
html,body{height:100%;overflow:hidden;margin:0;background:var(--bg);color:var(--text);font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif}
body{display:flex;flex-direction:column;font-size:13px}
header{flex:0 0 auto;background:#1f4e79;color:#fff;padding:16px 24px;display:flex;align-items:center;justify-content:space-between;gap:16px;flex-wrap:wrap}
header h1{font-size:20px;font-weight:bold;margin:0}
.hdr-actions{display:flex;flex-direction:column;align-items:stretch;gap:6px}
.dm-btn{background:rgba(255,255,255,.14);color:#fff;border:none;border-radius:6px;padding:8px 14px;font:inherit;font-size:12px;font-weight:bold;cursor:pointer;white-space:nowrap;display:flex;align-items:center;justify-content:center;gap:6px}
.dm-btn:hover{background:rgba(255,255,255,.24)}
.summary-wrap{flex:0 0 auto}
.summary-bar{display:flex;align-items:center;justify-content:space-between;gap:12px;width:100%;padding:7px 24px;background:var(--surface);border:none;border-bottom:1px solid var(--border);color:var(--text);font:inherit;cursor:pointer;text-align:left}
.summary-bar:hover{background:var(--subsurf)}
.sb-chips{display:flex;flex-wrap:wrap;gap:3px 16px;align-items:center;font-size:12px}
.sb-chips .grp{font-weight:bold;text-transform:uppercase;letter-spacing:.04em;font-size:10.5px;color:var(--muted)}
.sb-chip b{font-size:13px;margin-right:3px}
.sb-chip.bad b{color:#e24b4a}.sb-chip.warn b{color:#e67e22}.sb-chip.ok b{color:#27ae60}
.sb-toggle{flex:0 0 auto;font-size:11px;color:var(--muted);display:flex;align-items:center;gap:5px;white-space:nowrap}
.sb-toggle .chev{font-size:9px;transition:transform .15s}
.sb-toggle .t-open{display:none}
.summary-wrap:not(.collapsed) .sb-toggle .t-open{display:inline}
.summary-wrap:not(.collapsed) .sb-toggle .t-closed{display:none}
.summary-wrap:not(.collapsed) .sb-toggle .chev{transform:rotate(180deg)}
.summary-wrap.collapsed #summary{display:none}
#summary{padding:14px 24px 8px;max-height:40vh;overflow-y:auto}
#bar{display:flex;gap:14px;align-items:center;flex-wrap:wrap;margin-top:4px;font-size:12px;color:#fff}
#bar .pill{display:inline-flex;align-items:center;gap:6px;padding:3px 10px;border-radius:12px;font-weight:500;background:#eaf3de;color:#27500a}
#bar .pill.warn{background:#faeeda;color:#633806} #bar .pill.err{background:#fcebeb;color:#a32d2d}
#bar .pill i{width:8px;height:8px;border-radius:50%;background:currentColor;display:inline-block}
#bar b{font-weight:bold}
.note{display:flex;gap:10px;align-items:center;padding:8px 14px;margin:8px 24px 0;border-radius:6px;font-size:13px;
  background:rgba(250,199,117,.22);border:0.5px solid rgba(186,117,23,.45)}
.note.err{background:rgba(240,149,149,.2);border-color:rgba(163,45,45,.45)}
.row{display:grid;grid-template-columns:170px repeat(4,minmax(0,1fr));gap:10px;align-items:stretch;margin-bottom:6px}
.rowlabel{display:flex;flex-direction:column;justify-content:center;font-size:14px;font-weight:500}
.rowlabel span{font-size:11px;font-weight:400;color:var(--muted);margin-top:2px}
.ctr{background:var(--surface);border:0.5px solid var(--border);border-radius:10px;padding:10px 14px;text-align:left;cursor:pointer;font:inherit;color:inherit}
.ctr:hover{border-color:var(--muted)} .ctr.on{border-color:#378add;box-shadow:0 0 0 1px #378add inset}
.ctr .n{font-size:26px;font-weight:500;line-height:1.1}.ctr .l{font-size:12px;color:var(--muted);margin-top:2px}.ctr .s{font-size:11px;color:var(--muted);opacity:.8;margin-top:1px}
.ctr.bad .n{color:#e24b4a}.ctr.warn .n{color:#ba7517}.ctr.ok .n{color:#1d9e75}.ctr.dead .n{color:#a32d2d}.ctr.zero .n{color:var(--muted)}
.sub{font-size:12px;color:var(--muted);margin:0 0 14px 180px}.sub .cmp{color:var(--text);margin-bottom:3px}.sub .cmp b{font-weight:600}
.sub button{background:none;border:none;color:#378add;cursor:pointer;font:inherit;padding:0}
#list{background:var(--surface);border:0.5px solid var(--border);border-radius:10px;overflow:hidden;display:none;margin-top:6px}
#list .hd{padding:8px 14px;font-size:12px;color:var(--muted);border-bottom:0.5px solid var(--border);display:flex;justify-content:space-between}
#list .hd button{background:none;border:none;color:var(--muted);cursor:pointer;font:inherit}
#list .it{display:grid;grid-template-columns:minmax(0,1fr) 130px minmax(0,300px) 104px 24px;gap:14px;align-items:center;padding:8px 14px;border-bottom:0.5px solid var(--border);cursor:pointer;font-size:13px}
#list .it:hover{background:var(--subsurf)}
#list .it div{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
#list .it .tag,#list .it .rs{font-size:12px;color:var(--muted)}
#list .it .pj{font-size:11px;color:var(--muted);margin-top:1px}
#list .more{padding:8px 14px;font-size:12px;color:var(--muted)}
.abadge{font-size:11px;padding:2px 8px;border-radius:8px;white-space:nowrap;text-align:center}
.abadge.down{background:#fcebeb;color:#a32d2d}.abadge.never{background:#f09595;color:#501313}
.abadge.up{background:#eaf3de;color:#27500a}.abadge.new{background:#faeeda;color:#633806}.abadge.quiet{background:#e6f1fb;color:#0c447c}
#sp{position:fixed;top:0;right:0;bottom:0;width:390px;max-width:100vw;background:var(--surface);border-left:0.5px solid var(--border);
  z-index:2000;transform:translateX(105%);transition:transform .2s;overflow-y:auto;padding:20px 22px;box-shadow:-8px 0 24px rgba(0,0,0,.12)}
#sp.open{transform:none}
#sp .x{position:absolute;top:12px;right:14px;background:none;border:none;font-size:20px;cursor:pointer;color:var(--muted)}
#sp h3{font-size:16px;font-weight:500;margin:0 28px 4px 0;word-break:break-word}
#sp .sub2{font-size:12px;color:var(--muted);margin-bottom:12px}
#sp dl{display:grid;grid-template-columns:95px 1fr;gap:8px 10px;font-size:13px;margin:14px 0}
#sp dt{color:var(--muted)}#sp dd{margin:0;word-break:break-word}
#sp .bars{display:flex;gap:2px}#sp .bars i{display:block;width:10px;height:18px;border-radius:2px}
#sp .btns{display:flex;gap:8px;flex-wrap:wrap;margin-top:16px}
#sp .btns button{font:inherit;font-size:12px;padding:6px 12px;border-radius:8px;border:0.5px solid var(--border);background:var(--subsurf);color:var(--text);cursor:pointer}
#sp .pl{font-size:12px;margin-top:6px;padding-left:4px}#sp .pl div{padding:2px 0}
.foot{flex:0 0 auto;font-size:11px;color:var(--muted);margin:0 24px 8px;line-height:1.4}
#notes{flex:0 0 auto}
.mapwrap{flex:1 1 auto;min-height:0;margin:12px 24px 6px;display:flex;flex-direction:column;background:var(--surface);border:0.5px solid var(--border);border-radius:6px;overflow:hidden;box-shadow:0 1px 6px rgba(0,0,0,.15)}
#maphead{display:flex;gap:8px;flex-wrap:wrap;align-items:center;padding:10px 14px;border-bottom:0.5px solid var(--border);font-size:12px;color:var(--muted)}
#maphead button{font:inherit;font-size:12px;padding:4px 12px;border-radius:12px;border:0.5px solid var(--border);background:var(--subsurf);color:var(--text);cursor:pointer}
#maphead button.on{background:#378add;border-color:#378add;color:#fff}
#maphead .lg{display:inline-flex;align-items:center;gap:5px;margin-left:10px}
#maphead .lg i{display:inline-block;width:14px;height:4px;border-radius:2px}
#maphead .lg i.dot{width:10px;height:10px;border-radius:50%}
#map{flex:1 1 auto;min-height:0}
.mapwrap:fullscreen{margin:0;background:var(--bg);padding:12px;border-radius:0}
.mapwrap:-webkit-full-screen{margin:0;background:var(--bg);padding:12px;border-radius:0}
#list .it .pin{cursor:pointer;font-size:14px;text-align:center;opacity:.7}
#list .it .pin:hover{opacity:1}
.leaflet-container a.mapbtn{display:flex;align-items:center;justify-content:center;width:30px;height:30px;color:#333;background:#fff;text-decoration:none}
.leaflet-container a.mapbtn:hover{background:#f4f6f9}
.leaflet-container a.mapbtn i{font-size:16px}
.ep{width:22px;height:22px;border-radius:50%;background:#facc15;color:#111;font:700 12px/18px Arial,sans-serif;text-align:center;border:2px solid #fff;box-sizing:border-box}
.tip b{font-weight:600}.tip div{white-space:nowrap}
/* anything with a hover note shows the help cursor (? pointer), as in the dashboard; clickable things keep their own pointer */
:where([title]){cursor:help}
.ctr,.ctr[title]{cursor:pointer}
.leaflet-bar a,.leaflet-bar a[title]{cursor:pointer}
#list .it{cursor:pointer}
@media (max-width:1000px){header{padding:13px 20px}.row{grid-template-columns:repeat(4,minmax(0,1fr))}.rowlabel{grid-column:1/-1}.sub{margin-left:0}}
</style>
"""

_BODY = """
<header>
  <div><h1>Live - Network Status</h1><div id="bar"></div></div>
  <div class="hdr-actions" id="act"></div>
</header>
<div id="notes"></div>
<div class="summary-wrap" id="sumwrap">
  <button class="summary-bar" id="sumbar" type="button" title="Show or hide the counters"><span class="sb-chips" id="chips"></span><span class="sb-toggle"><span class="t-closed">Details</span><span class="t-open">Hide</span> <span class="chev">&#9660;</span></span></button>
  <div id="summary">
    <div class="row" id="row-sensors"></div><div class="sub" id="sub-sensors"></div>
    <div class="row" id="row-paths"></div><div class="sub" id="sub-paths"></div>
    <div id="list"></div>
  </div>
</div>
<div class="mapwrap" id="mapwrap"><div id="maphead"></div><div id="map"></div><div id="sp" role="dialog" aria-label="Details"></div></div>
<div class="foot" id="foot"></div>
"""

_JS = r"""
<script>
(function(){
var C = __CTX__, URL_ = "__URL__", EVERY = __EVERY__;
var live = null, err = null, lastOk = null, nextAt = Date.now() + EVERY * 1000, open = null, sel = null, SHOW = 40, busy = false;
var KIND = {down:'Down now', new:'Just went down', recovered:'Back up', never:'Never worked', quiet:'Often silent'};
function el(tag, cls, text){ var e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; }
function nicDay(ms){ return new Date(ms).toLocaleDateString('en-GB', {timeZone:'Asia/Nicosia', day:'2-digit', month:'2-digit'}); }
function clock(d){ var ms = d ? new Date(d).getTime() : NaN; if (isNaN(ms)) return '-'; var t = new Date(ms).toLocaleTimeString('en-GB', {timeZone:'Asia/Nicosia', hour:'2-digit', minute:'2-digit'}); return nicDay(ms) === nicDay(Date.now()) ? t : nicDay(ms) + ' ' + t; }
function ago(ms){ var m = Math.round((Date.now() - ms) / 60000); return m < 1 ? 'just now' : m < 60 ? m + ' min ago' : m < 2880 ? Math.round(m / 60) + ' h ago' : Math.round(m / 1440) + ' days ago'; }
function ttext(s){ if (s == null) return ''; s = Math.round(s); var r = s % 60; return Math.floor(s / 60) + ':' + (r < 10 ? '0' : '') + r + ' min'; }
function reading(q){ return q ? (Math.round(q.speed_kmh * 10) / 10) + ' km/h' + (q.travel_time_s != null ? ', ' + ttext(q.travel_time_s) : '') : ''; }
function tip(title, sub){ var s = document.createElement('span'); s.className = 'tip'; var a = document.createElement('div'); a.appendChild(document.createElement('b')).textContent = title; s.appendChild(a); if (sub){ var b = document.createElement('div'); b.textContent = sub; s.appendChild(b); } return s; }
function reporting(p){ return p && p.speed_kmh != null && p.speed_kmh >= 0; }

/* ---- VMS: from the Worker's snapshot when it carries signs ---- */
function cyprusMs(naive){
  var m = /^(\d{4})-(\d\d)-(\d\d)[T ](\d\d):(\d\d):(\d\d)/.exec(naive || ''); if (!m) return NaN;
  var asUtc = Date.UTC(+m[1], m[2] - 1, +m[3], +m[4], +m[5], +m[6]);
  var f = new Intl.DateTimeFormat('en-US', {timeZone:'Asia/Nicosia', hourCycle:'h23', year:'numeric', month:'2-digit', day:'2-digit', hour:'2-digit', minute:'2-digit', second:'2-digit'});
  function off(ms){ var q = {}; f.formatToParts(new Date(ms)).forEach(function(x){ q[x.type] = x.value; }); return Date.UTC(+q.year, q.month - 1, +q.day, +q.hour, +q.minute, +q.second) - ms; }
  return asUtc - off(asUtc - off(asUtc));
}
function vmsLive(){
  if (!live || !Array.isArray(live.vms)) return null;
  if (!live.vms.some(function(s){ return s.status != null || s.updated_at != null; })) return null;
  var m = {}; live.vms.forEach(function(s){ m[String(s.id)] = s; }); return m;
}
function vmsLiveState(lv){
  if (!lv || lv.status == null) return ['down', 'No status reported'];
  var t = cyprusMs(lv.updated_at);
  if (!isNaN(t) && Date.now() - t > 3600000) return ['down', 'Status is stale (last update ' + clock(t) + ')'];
  return lv.status === 'working' ? ['up', 'Working'] : ['down', 'VMS not working'];
}

/* ---- the model: what the live snapshot says, joined with stored history ---- */
function model(){
  var byId = {}; (live.paths || []).forEach(function(p){ byId[String(p.id)] = p; });
  var U = {}, L = {sensors:{down:[],new:[],recovered:[],never:[],quiet:[]}, paths:{down:[],new:[],recovered:[],never:[],quiet:[]}};
  var pathUnits = {};
  Object.keys(C.paths).forEach(function(id){
    var c = C.paths[id], live1 = byId[id], rep = reporting(live1);
    var u = {key:'P|'+id, id:id, kind:'path', glabel:'Bluetooth path', name:c.n, c:c, rep:rep, live:live1 || null,
             state: rep ? 'up' : (c.nv ? 'never' : 'down'), reason: rep ? reading(live1) : 'No speed or travel time'};
    U[u.key] = u; pathUnits[id] = u;
    if (!rep){
      if (c.fl && !c.nv) { L.paths.quiet.push(u.key); u.quiet = true; return; }
      L.paths.down.push(u.key);
      if (c.nv) L.paths.never.push(u.key);
      else if (c.st === 'up') { L.paths.new.push(u.key); u.isNew = true; }
    } else if (c.st !== 'up' && !c.fl) { L.paths.recovered.push(u.key); u.isRec = true; }
  });
  var vlive = vmsLive(), noEv = [];
  Object.keys(C.bt).forEach(function(sid){
    var c = C.bt[sid], ps = c.p.map(function(id){ return pathUnits[id]; }).filter(Boolean);
    if (c.v === 'no_evidence' || !ps.length){ noEv.push(sid); return; }
    var alive = ps.some(function(q){ return q.rep; }), faulty = /^faulty/.test(c.v || ''), allNever = ps.every(function(q){ return q.c.nv; });
    var u = {key:'S|'+sid, id:sid, kind:'bt', glabel:'Bluetooth sensor', name:c.n, c:c, ps:ps, alive:alive, lat:c.lat, lon:c.lon,
             state: alive ? 'up' : (allNever ? 'never' : 'down'),
             reason: alive ? ps.filter(function(q){ return q.rep; }).length + ' of ' + ps.length + ' paths report' : 'None of its ' + ps.length + ' path(s) report'};
    U[u.key] = u;
    if (!alive){ L.sensors.down.push(u.key); if (allNever) L.sensors.never.push(u.key); if (!faulty) { L.sensors.new.push(u.key); u.isNew = true; } }
    else if (faulty){ L.sensors.recovered.push(u.key); u.isRec = true; }
  });
  L.noEv = noEv; L.vmsLive = !!vlive;
  C.vms.forEach(function(v){
    var st = v.st, why = v.why, isNew = v.new, isRec = v.rec, lat = v.lat, lon = v.lon, label = 'VMS (last check)';
    if (vlive){
      var lv = vlive[String(v.id)], r = vmsLiveState(lv); label = 'VMS';
      st = r[0] === 'up' ? 'up' : (v.st === 'never' ? 'never' : 'down'); why = r[1];
      isNew = r[0] !== 'up' && v.st === 'up'; isRec = r[0] === 'up' && v.st !== 'up';
      if (lv && lv.lat != null && lv.lon != null){ lat = lv.lat; lon = lv.lon; }
    }
    var u = {key:'V|'+v.id, id:v.id, kind:'vms', glabel:label, name:v.n, c:v, state:st, reason:why, lat:lat, lon:lon, isNew:isNew, isRec:isRec};
    U[u.key] = u;
    if (st === 'down' || st === 'never') L.sensors.down.push(u.key);
    if (st === 'never') L.sensors.never.push(u.key);
    if (isNew) L.sensors.new.push(u.key);
    if (isRec) L.sensors.recovered.push(u.key);
  });
  ['sensors','paths'].forEach(function(s){ L[s].down.sort(function(a, b){
    var x = U[a], y = U[b]; return ((y.isNew?1:0) - (x.isNew?1:0)) || ((y.c.d||0) - (x.c.d||0)) || (x.name < y.name ? -1 : 1); }); });
  L.totals = {paths:Object.keys(pathUnits).length, reporting:Object.keys(pathUnits).filter(function(i){ return pathUnits[i].rep; }).length,
              bt:Object.keys(U).filter(function(k){ return U[k].kind === 'bt'; }).length};
  return {U:U, L:L};
}
var M = null;

/* ---- drawing ---- */
var BADGE_TIP = {
  never: {path: 'This path has never reported a reading since monitoring began. It may be new, switched off, or never configured properly.',
          sensor: 'This has never worked since monitoring began. It may be new, not yet powered on, or never set up properly.',
          bt: 'Every path through this sensor has failed since monitoring began, so there is no proof it ever worked.'},
  down: {path: 'No speed reading in the live feed right now. If the number of days is shown, it has been failing at every stored check since then.',
         sensor: 'Its latest check failed. The number of days is how long it has been failing.',
         bt: 'Judged faulty from how its paths behave: it has no working path while its failing paths lead to sensors that are proven alive.'},
  new: {path: 'This path worked at the last stored check ({check}) and has no speed reading now. That check is up to 6 hours old, so it may have stopped at any time since.',
        sensor: 'This worked at the last stored check ({check}) and is failing now. That check is up to 6 hours old.',
        bt: 'None of the paths of this sensor reports now, and it was not judged faulty at the last stored check ({check}).'},
  up: {path: 'The live feed gives this path a speed reading, so it is working.',
       sensor: 'Its latest check passed.',
       bt: 'At least one of its paths works, which proves the sensor itself is alive.'},
  quiet: {path: 'This path goes silent on a regular pattern (for example every night, when there is little traffic), so it is probably not a fault. It is left out of the counters.',
          sensor: '', bt: ''}
};
function badgeTip(u, cls, label){
  var kind = u.kind === 'path' ? 'path' : (u.kind === 'bt' ? 'bt' : 'sensor');
  var t = (BADGE_TIP[cls] || {})[kind] || '';
  if (cls === 'up' && label === 'Back up') t = 'It was down at the last stored check ({check}) and is working again now.';
  if (cls === 'down' && label === 'Silent') t = 'No speed reading in the live feed right now. It has not failed for long enough, or has not failed at the stored checks, to count the days.';
  return t.replace('{check}', C.built);
}
function tipBadge(u, b){ var s = el('span', 'abadge ' + b[0], b[1]); var tp = badgeTip(u, b[0], b[1]); if (tp) s.title = tp; return s; }
var LEGEND_TIP = {
  'Reporting': 'The live feed gives this path a speed right now, or the sign or sensor is working.',
  'Silent': 'A path with no speed reading right now.',
  'Never worked': 'Has never reported since monitoring began.',
  'Often silent': 'A path that goes quiet on a regular pattern, for example every night. Probably not a fault.',
  'Not enough evidence': 'A Bluetooth sensor with too few usable paths to judge. Not counted.'
};
var DL_TIP = {
  'Now': 'The latest state, from the live feed or the last stored check.',
  'Compared with': 'The last stored check this state is compared with. It runs every 6 hours, so the change may have happened at any time since.',
  'Speed': 'Current average speed on the path, from the live feed.',
  'Travel time': 'Current time to drive the whole path.',
  'Typical now': 'The usual speed on this path at this time of day. Compare it with the speed to see if traffic is unusual.',
  'Free flow': 'The speed on this path when the road is empty.',
  'Down since': 'When the stored checks began to fail without a good one in between.',
  'Since': 'When the stored checks began to fail without a good one in between.',
  'Pattern': 'Why this path is not counted as a fault.',
  'Contract': 'The project responsible for this sensor, from QA Locations.xlsx. Shown on this PC only.',
  'Lifetime': 'The stability tier: the share of all stored checks that passed since monitoring began.',
  'Triangulation': 'How the sensor was judged from its paths: a working path proves both end sensors alive.'
};
function badge(u, k){
  if (k === 'new') return ['new', 'Just went down']; if (k === 'recovered') return ['up', 'Back up']; if (k === 'quiet') return ['quiet', 'Often silent'];
  if (u.state === 'never') return ['never', 'Never worked'];
  if (u.state === 'down') return ['down', u.c.d ? 'Down ' + u.c.d + 'd' : (u.kind === 'path' && !u.isNew ? 'Silent' : 'Down')];
  return ['up', 'Working'];
}
function drawBar(){
  var bar = document.getElementById('bar'); bar.textContent = '';
  var ok = live && !err, stale = live && (Date.now() - Date.parse(live.common_measurement_timestamp || live.generated_at)) > 20 * 60 * 1000;
  var pill = el('span', 'pill' + (err ? ' err' : (stale ? ' warn' : '')));
  pill.appendChild(el('i')); pill.appendChild(document.createTextNode(err ? 'Live feed unreachable' : (live ? (stale ? 'Feed measurement is old' : 'Live') : 'Loading...')));
  bar.appendChild(pill);
  if (live){
    var t = el('span'); t.appendChild(document.createTextNode('Updated ')); t.appendChild(el('b', null, clock(lastOk)));
    t.appendChild(document.createTextNode(' - feed measured ')); t.appendChild(el('b', null, clock(live.common_measurement_timestamp || live.generated_at)));
    bar.appendChild(t);
  }
  bar.appendChild(el('span', null, 'next refresh in ' + Math.max(0, Math.round((nextAt - Date.now()) / 1000)) + 's'));
  var act = document.getElementById('act'); act.textContent = '';
  var b = el('button', 'dm-btn', busy ? 'Refreshing...' : 'Refresh now'); b.onclick = function(){ poll(); }; act.appendChild(b);
  var dark = document.documentElement.getAttribute('data-theme') !== 'light';
  var tb = el('button', 'dm-btn'); tb.appendChild(el('i', 'ti ' + (dark ? 'ti-sun' : 'ti-moon'))); tb.appendChild(document.createTextNode(' ' + (dark ? 'Light' : 'Dark')));
  tb.title = 'Switch between light and dark mode';
  tb.onclick = function(){
    var next = dark ? 'light' : 'dark'; document.documentElement.setAttribute('data-theme', next);
    try { localStorage.setItem('live-theme', next); } catch (e) {}
    drawBar();
  };
  act.appendChild(tb);
}
function drawNotes(){
  var n = document.getElementById('notes'); n.textContent = '';
  if (err){ var e = el('div', 'note err'); e.textContent = "Couldn't reach the live feed" + (lastOk ? ' - showing the data from ' + clock(lastOk) : '') + '. Retrying every ' + (EVERY / 60) + ' minutes.'; n.appendChild(e); }
  if (live){
    var lag = Math.round((Date.now() - Date.parse(live.common_measurement_timestamp || live.generated_at)) / 60000);
    if (lag > 20){ var w = el('div', 'note'); w.textContent = "The feed's own measurement time is " + lag + ' minutes old, so what you see may not be current.'; n.appendChild(w); }
  }
  if (C.projects_note){ var pn = el('div', 'note'); pn.textContent = C.projects_note; n.appendChild(pn); }
  C.feeds.forEach(function(f){
    var w = el('div', 'note'); var unit = f.group === 'Traffic Detection' ? 'loops' : (f.group === 'VMS' ? 'signs' : 'units');
    w.textContent = f.label + ' down ' + (f.days ? f.days + 'd' : 'under a day') + ' - ' + f.down + ' of ' + f.total + ' ' + unit + ' report no good data (as of the ' + C.built + ' check). Treated as one outage and not counted below.';
    n.appendChild(w);
  });
}
var TIPS = {
  sensors: {
    row: 'Traffic Detection loops, VMS signs and Bluetooth sensors. A Bluetooth sensor has no status of its own: it is judged from how its paths behave (triangulation), as of the last stored run.',
    down: 'Sensors failing right now: loops and signs whose latest check failed, and Bluetooth sensors judged faulty.',
    new: 'Sensors that were fine at the last stored check ({check}) and are down now. That check is up to 6 hours old, so this may have happened at any time since. Sensors that keep failing and recovering are not listed here.',
    recovered: 'Sensors that were down at the last stored check ({check}) and are working now.',
    never: 'Sensors that have never once worked since monitoring began. Also counted in Down now.'
  },
  paths: {
    row: 'Bluetooth paths, each on its own. A path is reporting when the live feed gives it a speed of 0 or more.',
    down: 'Paths with no speed reading in the live feed right now. Updated every 5 minutes.',
    new: 'Paths that worked at the last stored check ({check}) and have no speed reading now. That check is up to 6 hours old, so the path may have stopped at any time since. Not a new path. Paths that go quiet on a regular pattern are not listed here.',
    recovered: 'Paths that were silent at the last stored check ({check}) and are reporting now.',
    never: 'Paths that have never reported a reading. Also counted in Down now.'
  }
};
function checkAt(){ return clock(C.built_iso || null) !== '-' ? clock(C.built_iso) : C.built; }
function checkAge(){ var at = C.built_iso ? Date.parse(C.built_iso) : NaN; if (isNaN(at)) return ''; var m = Math.max(0, Math.round((Date.now() - at) / 60000)); return ' (' + (m < 90 ? m + ' min' : Math.round(m / 60) + ' h') + ' ago)'; }
function drawRow(sec, title, subtitle){
  var row = document.getElementById('row-' + sec); row.textContent = '';
  var lab = el('div', 'rowlabel', title); lab.appendChild(el('span', null, subtitle)); lab.title = TIPS[sec].row; row.appendChild(lab);
  [['down','Down now','bad','right now'],['new','Just went down','warn','since the ' + checkAt() + ' check'],['recovered','Back up','ok','since the ' + checkAt() + ' check'],['never','Never worked','dead','since monitoring began']].forEach(function(k){
    var n = M.L[sec][k[0]].length, b = el('button', 'ctr ' + k[2] + (n ? '' : ' zero') + (open && open.sec === sec && open.k === k[0] ? ' on' : ''));
    b.title = TIPS[sec][k[0]].replace('{check}', C.built); b.appendChild(el('div', 'n', String(n))); b.appendChild(el('div', 'l', k[1])); b.appendChild(el('div', 's', k[3]));
    b.onclick = function(){ toggle(sec, k[0]); }; row.appendChild(b);
  });
  var sub = document.getElementById('sub-' + sec); sub.textContent = '';
  var cmp = el('div', 'cmp'); cmp.appendChild(el('b', null, 'Just went down')); cmp.appendChild(document.createTextNode(' and ')); cmp.appendChild(el('b', null, 'Back up'));
  cmp.appendChild(document.createTextNode(' compare the live feed with the ' + checkAt() + ' check' + checkAge() + '. A change may have happened any time since.')); sub.appendChild(cmp);
  var more = el('div'); sub.appendChild(more); sub = more;
  var q = M.L[sec].quiet.length;
  if (sec === 'paths' && q){
    var qb = el('button', null, q + ' path' + (q > 1 ? 's are' : ' is') + ' often silent and not counted above - show');
    qb.title = 'These paths go silent on a regular pattern (for example every night), so they are probably not faults. They are left out of the counters.';
    qb.onclick = function(){ toggle('paths', 'quiet'); }; sub.appendChild(qb);
  } else if (sec === 'sensors'){
    sub.textContent = 'Bluetooth sensors are judged live from their paths (an inference). ' + (M.L.vmsLive ? 'VMS is live.' : 'VMS is as of the ' + C.built + ' check (the live feed does not carry signs yet).');
  } else sub.textContent = 'Judged live: a path with no speed reading is silent.';
}
function toggle(sec, k){ open = (open && open.sec === sec && open.k === k) ? null : {sec:sec, k:k}; draw(); }
function drawList(){
  var box = document.getElementById('list'); box.textContent = '';
  if (!open){ box.style.display = 'none'; return; }
  box.style.display = 'block';
  var keys = M.L[open.sec][open.k];
  var hd = el('div', 'hd'); hd.appendChild(el('span', null, (open.sec === 'paths' ? 'Bluetooth paths' : 'Sensors') + ' - ' + KIND[open.k] + ' (' + keys.length + ')'));
  var cl = el('button', null, 'Close'); cl.onclick = function(){ toggle(open.sec, open.k); }; hd.appendChild(cl); box.appendChild(hd);
  if (!keys.length) box.appendChild(el('div', 'more', 'Nothing here.'));
  keys.slice(0, SHOW).forEach(function(key){
    var u = M.U[key], b = badge(u, open.k), row = el('div', 'it');
    var nm = el('div', null, u.name), pj = projOf(u); if (pj){ var pe = el('div', 'pj', pj); pe.title = pj; nm.appendChild(pe); }
    row.appendChild(nm); row.appendChild(el('div', 'tag', u.glabel));
    var rs = el('div', 'rs', u.reason); rs.title = u.reason; row.appendChild(rs);
    row.appendChild(tipBadge(u, b)); row.onclick = function(){ openPanel(key); };
    var pin = el('span', 'pin', '\ud83d\udccd'); pin.title = 'Show on map';
    if (SHAPES[key]) pin.onclick = function(ev){ ev.stopPropagation(); showOnMap(u); }; else pin.style.visibility = 'hidden';
    row.appendChild(pin); box.appendChild(row);
  });
  if (keys.length > SHOW) box.appendChild(el('div', 'more', 'Showing the first ' + SHOW + ' of ' + keys.length + '.'));
}
function drawChips(){
  var c = document.getElementById('chips'); c.textContent = '';
  [['sensors', 'Sensors'], ['paths', 'Paths']].forEach(function(g){
    c.appendChild(el('span', 'grp', g[1]));
    [['down', 'down now', 'bad'], ['new', 'just went down', 'warn'], ['recovered', 'back up', 'ok'], ['never', 'never worked', 'bad']].forEach(function(k){
      var n = M.L[g[0]][k[0]].length, ch = el('span', 'sb-chip' + (n ? ' ' + k[2] : ''));
      ch.appendChild(el('b', null, String(n))); ch.appendChild(document.createTextNode(k[1])); c.appendChild(ch);
    });
  });
}
document.getElementById('sumbar').onclick = function(){
  document.getElementById('sumwrap').classList.toggle('collapsed');
  setTimeout(function(){ if (MAP) MAP.invalidateSize(); }, 50);
};
if (window.ResizeObserver) new ResizeObserver(function(){ if (MAP) MAP.invalidateSize(); }).observe(document.getElementById('map'));
function draw(){
  drawBar(); drawNotes();
  if (!live){ return; }
  M = model();
  var T = M.L.totals;
  drawRow('sensors', 'Sensors', T.bt + ' Bluetooth sensors, ' + C.vms.length + ' VMS');
  drawRow('paths', 'Bluetooth paths', T.reporting + ' of ' + T.paths + ' reporting');
  drawChips();
  drawMap();
  drawList();
  drawFoot();
  if (sel && M.U[sel]) openPanel(sel, true);
}
// The history behind the badges is the last stored 6-hourly check, not the live feed: say how old it is (the dashboard calls 9 h too old).
function drawFoot(){
  var at = C.built_iso ? Date.parse(C.built_iso) : NaN, age = isNaN(at) ? null : Date.now() - at;
  document.getElementById('foot').textContent = 'History context (never worked, often silent, last 20 runs) is from the 6-hourly check of ' + C.built +
    (age == null ? '' : ' (' + ago(at) + ')') + (age != null && age > 9 * 3600000 ? ' - older than the usual 6 hours, so the monitor may have stopped' : '') +
    '. Nothing is stored by this page.';
}

/* ---- map ---- */
var HOME = {center:[34.95, 33.15], zoom:9};
var HLKEY = null, EPS = null;
var MAP = null, LAY = {}, SHAPES = {}, ON = {paths:true, bt:true, vms:true}, SAT = null, STREETS = null, satOn = false;
var COL = {up:'#1d9e75', down:'#e24b4a', never:'#a32d2d', quiet:'#e58e0a', none:'#9ca3af'};
function colorOf(u){ return u.state === 'up' ? COL.up : (u.quiet ? COL.quiet : (u.state === 'never' ? COL.never : COL.down)); }
function vmsIcon(col){ return L.divIcon({className:'', iconSize:[16, 16], html:'<div style="width:16px;height:16px;background:' + col + ';border:2px solid #fff;border-radius:3px;box-shadow:0 0 0 1px rgba(0,0,0,.4)"></div>'}); }
function drawMapHead(){
  var h = document.getElementById('maphead'); h.textContent = '';
  [['paths','Bluetooth paths'],['bt','Bluetooth sensors'],['vms','VMS']].forEach(function(k){
    var b = el('button', ON[k[0]] ? 'on' : '', k[1]);
    b.onclick = function(){ ON[k[0]] = !ON[k[0]]; if (ON[k[0]]) LAY[k[0]].addTo(MAP); else MAP.removeLayer(LAY[k[0]]); drawMapHead(); };
    h.appendChild(b);
  });
  var sb = el('button', satOn ? 'on' : '', 'Satellite');
  sb.onclick = function(){ satOn = !satOn; if (satOn){ MAP.removeLayer(STREETS); SAT.addTo(MAP); } else { MAP.removeLayer(SAT); STREETS.addTo(MAP); if (STREETS.getContainer) STREETS.getContainer().style.zIndex = 0; } drawMapHead(); };
  h.appendChild(sb);
  [['Reporting', COL.up], ['Silent', COL.down], ['Never worked', COL.never], ['Often silent', COL.quiet], ['Not enough evidence', COL.none]].forEach(function(k){
    var s = el('span', 'lg'); var i = el('i', 'dot'); i.style.background = k[1]; s.appendChild(i); s.appendChild(document.createTextNode(k[0])); s.title = LEGEND_TIP[k[0]] || ''; h.appendChild(s);
  });
}
function toggleFull(){
  var w = document.getElementById('mapwrap'), on = document.fullscreenElement || document.webkitFullscreenElement;
  if (!on) (w.requestFullscreen || w.webkitRequestFullscreen || function(){}).call(w);
  else (document.exitFullscreen || document.webkitExitFullscreen || function(){}).call(document);
}
function onFull(){
  var on = document.fullscreenElement || document.webkitFullscreenElement, i = document.querySelector('#map .ti-arrows-maximize, #map .ti-arrows-minimize');
  if (i) i.className = 'ti ' + (on ? 'ti-arrows-minimize' : 'ti-arrows-maximize');
  setTimeout(function(){ if (MAP) MAP.invalidateSize(); }, 150);
}
document.addEventListener('fullscreenchange', onFull); document.addEventListener('webkitfullscreenchange', onFull);
function initMap(){
  if (MAP || typeof L === 'undefined') return;
  MAP = L.map('map', {zoomControl:false, doubleClickZoom:false}).setView(HOME.center, HOME.zoom); MAP.setMaxZoom(19);
  function btn(icon, title, fn){
    var Ctl = L.Control.extend({options:{position:'topright'}, onAdd: function(){
      var box = L.DomUtil.create('div', 'leaflet-bar leaflet-control'), a = L.DomUtil.create('a', 'mapbtn', box);
      a.href = '#'; a.title = title; var i = document.createElement('i'); i.className = 'ti ' + icon; a.appendChild(i);
      L.DomEvent.disableClickPropagation(box); L.DomEvent.on(a, 'click', function(e){ L.DomEvent.preventDefault(e); fn(a); }); return box; }});
    MAP.addControl(new Ctl());
  }
  MAP.on('click', function(){ if (HLKEY) highlight(null); });   // a click on empty map clears the highlighted path
  btn('ti-arrows-maximize', 'Toggle full screen', toggleFull);
  btn('ti-crosshair', 'Reset the map view', function(){ MAP.setView(HOME.center, HOME.zoom); });
  L.control.zoom({position:'topright'}).addTo(MAP);
  try {
    STREETS = L.maplibreGL({style:'https://tiles.openfreemap.org/styles/liberty', attribution:'<a href="https://openfreemap.org" target="_blank">OpenFreeMap</a> &copy; OpenMapTiles, data &copy; OpenStreetMap'});
    STREETS.addTo(MAP); STREETS.getContainer().style.zIndex = 0;
  } catch (e) {
    STREETS = L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {attribution:'&copy; OpenStreetMap contributors', maxZoom:19}).addTo(MAP);
  }
  SAT = L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}', {attribution:'Imagery &copy; Esri', maxZoom:19});
  ['paths','bt','vms'].forEach(function(k){ LAY[k] = L.layerGroup().addTo(MAP); });
  drawMapHead();
}
function drawMap(){
  if (!live) return; initMap(); if (!MAP) return;
  Object.keys(M.U).forEach(function(key){
    var u = M.U[key], s = SHAPES[key], col = colorOf(u);
    if (u.kind === 'path'){
      var src = u.live && Array.isArray(u.live.coords) && u.live.coords.length > 1 ? u.live.coords : null;
      if (!s && src){ s = L.polyline(src, {weight:3, bubblingMouseEvents:false}); s.on('click', function(){ openPanel(key); }); s.bindTooltip(tip(u.name), {sticky:true}); s.addTo(LAY.paths); SHAPES[key] = s; }
      if (s) s.setTooltipContent(tip(u.name, u.rep ? reading(u.live) : (u.quiet ? 'No reading (often silent)' : 'No reading')));
      if (s){ if (key === HLKEY) s.setStyle({color:'#facc15', weight:7, opacity:1, dashArray:null}); else s.setStyle({color:col, weight: u.state === 'up' ? 3 : 5, opacity: u.state === 'up' ? 0.7 : 0.95, dashArray: u.state === 'never' ? '6,6' : null}); if (u.state !== 'up' && key !== HLKEY) s.bringToFront(); }
    } else if (u.kind === 'bt' && u.lat != null){
      if (!s){ s = L.circleMarker([u.lat, u.lon], {radius:7, weight:2, color:'#fff', fillOpacity:1, bubblingMouseEvents:false}); s.on('click', function(){ openPanel(key); }); s.bindTooltip(tip(u.name)); s.addTo(LAY.bt); SHAPES[key] = s; }
      s.setTooltipContent(tip(u.name, u.reason));
      s.setStyle({fillColor:col});
    } else if (u.kind === 'vms' && u.lat != null){
      if (!s){ s = L.marker([u.lat, u.lon], {icon:vmsIcon(col)}); s.on('click', function(){ openPanel(key); }); s.bindTooltip(tip(u.name)); s.addTo(LAY.vms); SHAPES[key] = s; }
      else s.setIcon(vmsIcon(col));
      s.setTooltipContent(tip(u.name, u.reason));
    }
  });
  M.L.noEv.forEach(function(sid){
    var c = C.bt[sid], key = 'N|' + sid; if (SHAPES[key] || c.lat == null) return;
    var s = L.circleMarker([c.lat, c.lon], {radius:6, weight:2, color:'#fff', fillColor:COL.none, fillOpacity:1});
    s.bindTooltip(tip(c.n, 'Not enough evidence')); s.addTo(LAY.bt); SHAPES[key] = s;
  });
}
function endMark(latlng, letter){ return L.marker(latlng, {icon:L.divIcon({className:'', html:'<div class="ep">' + letter + '</div>', iconSize:[22, 22], iconAnchor:[11, 11]}), interactive:false, keyboard:false}); }
// Highlight one path like the dashboard does: thick yellow line on top, A and B at its ends. null clears.
function highlight(key){
  HLKEY = key; if (EPS && MAP){ MAP.removeLayer(EPS); } EPS = null;
  drawMap();
  var s = key && SHAPES[key]; if (!s || !MAP) return;
  s.bringToFront();
  var pts = s.getLatLngs(); if (pts.length > 1){ EPS = L.layerGroup([endMark(pts[0], 'A'), endMark(pts[pts.length - 1], 'B')]).addTo(MAP); }
}
function showOnMap(u){
  closePanel(); drawMap(); if (!MAP) return;
  var layer = u.kind === 'path' ? 'paths' : (u.kind === 'bt' ? 'bt' : 'vms');
  if (!ON[layer]){ ON[layer] = true; LAY[layer].addTo(MAP); drawMapHead(); }
  setTimeout(function(){
    MAP.invalidateSize(); var s = SHAPES[u.key]; if (!s) return; var centre;
    if (s.getBounds){ MAP.fitBounds(s.getBounds(), {maxZoom:15, padding:[40, 40]}); highlight(u.key); }
    else {
      highlight(null); centre = s.getLatLng(); MAP.flyTo(centre, 15, {duration:0.8});
      var ring = L.circleMarker(centre, {radius:20, color:'#facc15', weight:5, fill:false, interactive:false}).addTo(MAP);
      setTimeout(function(){ MAP.removeLayer(ring); }, 3000);
    }
  }, 450);
}

/* ---- panel ---- */
function dl(p, label, v){ if (v == null || v === '') return; var dt = el('dt', null, label); var tp = DL_TIP[label] || (/^Last \d+ checks$/.test(label) ? 'Green is a good check, red a failed one. Oldest on the left, newest on the right.' : ''); if (tp) dt.title = tp; p.appendChild(dt); var d = el('dd'); if (v.nodeType) d.appendChild(v); else d.textContent = v; p.appendChild(d); }
// The contract (project) line. Only the Control Room's local copy switches C.show_projects on; the published page never has it.
function projOf(u){
  if (!C.show_projects) return null;
  function one(x){ return x.proj ? x.proj + (x.acct === 'out_of_support' ? ' (contract ended)' : '') : 'no contract on record'; }
  if (u.kind !== 'path') return one(u.c);
  var e = u.c.projs || []; if (!e.length) return null;
  var same = e.every(function(x){ return one(x) === one(e[0]); });
  return same ? one(e[0]) + (e.length > 1 ? ' (both ends)' : '') : e.map(function(x){ return (x.c || x.n) + ': ' + one(x); }).join(' / ');
}
function summary(u){
  var s = u.glabel + ' ' + u.name + (u.state === 'up' ? ' is reporting' : ' has no live reading') + ' (' + u.reason + ') as of ' + clock(lastOk);
  if (u.state === 'never') s += '; it has never worked since monitoring began';
  var pj = C.show_projects ? projOf(u) : (u.c.proj || null);
  if (pj) s += ' - contract: ' + pj;
  return s;
}
function copy(text, btn){
  var done = function(){ var t = btn.textContent; btn.textContent = 'Copied'; setTimeout(function(){ btn.textContent = t; }, 1200); };
  if (navigator.clipboard && navigator.clipboard.writeText) navigator.clipboard.writeText(text).then(done, function(){ window.prompt('Copy:', text); });
  else window.prompt('Copy:', text);
}
function openPanel(key, keep){
  var u = M && M.U[key]; if (!u) return; sel = key;
  if (MAP && !keep) highlight(u.kind === 'path' ? key : null);
  var p = document.getElementById('sp'); p.textContent = '';
  var x = el('button', 'x', '\u00d7'); x.setAttribute('aria-label', 'Close'); x.onclick = closePanel; p.appendChild(x);
  p.appendChild(el('h3', null, u.name)); p.appendChild(el('div', 'sub2', u.glabel + ' - id ' + u.id));
  var b = badge(u, u.isNew ? 'new' : (u.quiet ? 'quiet' : null)); p.appendChild(tipBadge(u, b));
  var d = el('dl'); dl(d, 'Now', u.reason);
  if (u.isNew) dl(d, 'Compared with', 'Worked at the ' + C.built + ' check (up to 6 hours ago), no reading now');
  if (u.isRec) dl(d, 'Compared with', 'Was down at the ' + C.built + ' check (up to 6 hours ago), reporting now');
  if (u.kind === 'path'){
    if (u.rep){
      dl(d, 'Speed', (Math.round(u.live.speed_kmh * 10) / 10) + ' km/h');
      dl(d, 'Travel time', ttext(u.live.travel_time_s));
      if (u.live.typical_kmh != null) dl(d, 'Typical now', Math.round(u.live.typical_kmh) + ' km/h at this time of day');
      if (u.live.free_flow_kmh != null) dl(d, 'Free flow', Math.round(u.live.free_flow_kmh) + ' km/h');
    }
    if (!u.rep) dl(d, 'Down since', u.c.sn ? u.c.sn + ' (stored check)' : null);
    dl(d, 'Contract', projOf(u));
    if (u.quiet) dl(d, 'Pattern', 'Goes silent on a regular pattern, so this is probably not a fault');
    if (u.c.t) dl(d, 'Lifetime', u.c.t + ' (' + u.c.g + ' of ' + u.c.tot + ' runs good)');
    if (u.c.r){ var bars = el('div', 'bars'); u.c.r.split('').forEach(function(ch){ var i = el('i'); i.style.background = ch === '1' ? '#1d9e75' : '#e24b4a'; bars.appendChild(i); }); dl(d, 'Last ' + u.c.r.length + ' checks', bars); }
  } else if (u.kind === 'bt'){
    dl(d, 'Triangulation', u.c.why); dl(d, 'Contract', projOf(u));
  } else {
    dl(d, 'Since', u.c.sn); dl(d, 'Contract', C.show_projects ? projOf(u) : (u.c.proj || 'No contract on record'));
    if (u.c.t) dl(d, 'Lifetime', u.c.t + ' (' + u.c.g + ' of ' + u.c.tot + ' runs good)');
  }
  p.appendChild(d);
  if (u.kind === 'bt'){
    p.appendChild(el('div', 'sub2', 'Paths ending here:')); var pl = el('div', 'pl');
    u.ps.forEach(function(q){ pl.appendChild(el('div', null, q.name + ' - ' + (q.rep ? 'reporting' : 'silent') + (q.c.nv ? ' (never worked)' : ''))); }); p.appendChild(pl);
  }
  var btns = el('div', 'btns');
  if (MAP && SHAPES[key]){ var mb = el('button', null, 'Show on map'); mb.onclick = function(){ showOnMap(u); }; btns.appendChild(mb); }
  var c1 = el('button', null, 'Copy summary'); c1.onclick = function(){ copy(summary(u), c1); }; btns.appendChild(c1);
  var c2 = el('button', null, 'Copy link'); c2.onclick = function(){ copy(location.href.split('#')[0] + '#s=' + encodeURIComponent(key), c2); }; btns.appendChild(c2);
  p.appendChild(btns); p.classList.add('open');
  if (!keep) try { history.replaceState(null, '', '#s=' + encodeURIComponent(key)); } catch (e) {}
}
function closePanel(){ document.getElementById('sp').classList.remove('open'); sel = null; try { history.replaceState(null, '', location.pathname + location.search); } catch (e) {} }
document.addEventListener('keydown', function(e){ if (e.key === 'Escape' && sel) closePanel(); });

/* ---- polling ---- */
function poll(){
  if (busy) return; busy = true; drawBar();
  var ctl = new AbortController(), t = setTimeout(function(){ ctl.abort(); }, 25000);
  fetch(URL_, {cache:'no-store', signal:ctl.signal}).then(function(r){ if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); })
    .then(function(j){ if (!j || !Array.isArray(j.paths)) throw new Error('unexpected data'); live = j; lastOk = Date.now(); err = null; })
    .catch(function(e){ err = e; })
    .then(function(){ clearTimeout(t); busy = false; nextAt = Date.now() + EVERY * 1000; draw();
      var m = /#s=(.+)$/.exec(location.hash); if (m && !sel && M){ var k = decodeURIComponent(m[1]); if (M.U[k]) openPanel(k); } });
}
setInterval(function(){ if (Date.now() >= nextAt) poll(); else { drawBar(); if (live) drawFoot(); } }, 1000);
document.addEventListener('visibilitychange', function(){ if (!document.hidden && Date.now() >= nextAt) poll(); });
draw(); poll();
})();
</script>
"""


def render(ctx):
    """The whole Live page as one HTML string. ctx comes from attention.live_context()."""
    blob = json.dumps(ctx, default=str).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
    js = _JS.replace("__CTX__", blob).replace("__URL__", LIVE_FEED_URL).replace("__EVERY__", str(REFRESH_SECONDS))
    return ('<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>{html.escape("Live - ITS Infrastructure Health")}</title>{_THEME_HEAD}{_CSS}{_MAP_HEAD}</head><body>{_BODY}{js}</body></html>')
