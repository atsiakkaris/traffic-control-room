// Bundles src/*.js and writes:
//   dist/layout-tool-inpage.js  the tool itself (used by the offline harness)
//   dist/layout-tool.js         the launcher you carry: opens the tool in a NEW TAB from the OMNIA
//                               page, and falls back to an overlay if the browser blocks pop-ups
//   dist/install.html           drag-to-bookmarks-bar link for the launcher
// Run: node build.js
const fs = require('fs');
const path = require('path');

const read = (f) => fs.readFileSync(path.join(__dirname, 'src', f), 'utf8');
const tool = `(function(){\n${read('geometry.js')}\n${read('api.js')}\n${read('sync.js')}\n${read('ui.js')}\n})();`;

const launcher = `(function () {
  var TOOL = ${JSON.stringify(tool)};
  var inTab = !!window.__vltTabMode;
  var w = null;
  if (!inTab) { try { w = window.open('', '_blank'); } catch (e) { w = null; } }
  if (!w) {
    if (!inTab) window.__vltNote = 'Your browser blocked the new tab, so the tool opened on this page instead. Allow pop-ups for this site to get it in its own tab.';
    (0, eval)(TOOL);
    return;
  }
  w.__vltTabMode = true;
  try { w.document.title = 'VMS Layout Tool'; w.document.body.style.cssText = 'margin:0;background:#10151b'; } catch (e) { /* cosmetic */ }
  w.eval(TOOL);
})();`;

fs.mkdirSync(path.join(__dirname, 'dist'), { recursive: true });
fs.writeFileSync(path.join(__dirname, 'dist', 'layout-tool-inpage.js'), tool);
fs.writeFileSync(path.join(__dirname, 'dist', 'layout-tool.js'), launcher);

const href = 'javascript:' + encodeURIComponent(launcher);
const esc = (s) => s.replace(/&/g, '&amp;').replace(/</g, '&lt;');
const html = `<!doctype html>
<meta charset="utf-8">
<title>VMS Layout Tool – install</title>
<style>
  body{font:16px/1.5 system-ui,Segoe UI,sans-serif;max-width:720px;margin:40px auto;padding:0 16px;color:#1b2430}
  a.bm{display:inline-block;background:#1f6f5a;color:#fff;padding:10px 16px;border-radius:8px;text-decoration:none;font-weight:600}
  textarea{width:100%;height:120px;font:12px ui-monospace,Consolas,monospace}
  code{background:#eef2f5;padding:1px 5px;border-radius:4px}
</style>
<h1>VMS Layout Tool</h1>
<ol>
  <li>Show your bookmarks bar (Ctrl+Shift+B).</li>
  <li>Drag this button onto the bar: <a class="bm" href="${href.replace(/"/g, '&quot;')}">VMS Layout Tool</a></li>
  <li>Log in to OMNIA, then click the bookmark. The tool opens in a new tab and uses your existing OMNIA session.</li>
</ol>
<p>If the browser blocks the new tab, allow pop-ups for the OMNIA site; until then the tool opens on top of the OMNIA page. If it blocks bookmarklets, paste the script below into the developer console (F12) on the OMNIA page instead.</p>
<textarea readonly onclick="this.select()">${esc(launcher)}</textarea>
<p>Rebuild after changing <code>src/</code>: <code>node build.js</code></p>
`;
fs.writeFileSync(path.join(__dirname, 'dist', 'install.html'), html);
console.log(`built dist/layout-tool.js (${launcher.length} bytes, launcher), dist/layout-tool-inpage.js (${tool.length} bytes) and dist/install.html`);
