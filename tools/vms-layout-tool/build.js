// Bundles src/*.js into one script and writes dist/install.html with a
// drag-to-bookmarks-bar link. Run: node build.js
const fs = require('fs');
const path = require('path');

const read = (f) => fs.readFileSync(path.join(__dirname, 'src', f), 'utf8');
const code = `(function(){\n${read('geometry.js')}\n${read('api.js')}\n${read('ui.js')}\n})();`;

fs.mkdirSync(path.join(__dirname, 'dist'), { recursive: true });
fs.writeFileSync(path.join(__dirname, 'dist', 'layout-tool.js'), code);

const href = 'javascript:' + encodeURIComponent(code);
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
  <li>Log in to OMNIA, then click the bookmark. The editor opens on top of OMNIA and uses your existing session.</li>
</ol>
<p>If your browser blocks bookmarklets, paste the script below into the developer console (F12) on the OMNIA page instead.</p>
<textarea readonly onclick="this.select()">${esc(code)}</textarea>
<p>Rebuild after changing <code>src/</code>: <code>node build.js</code></p>
`;
fs.writeFileSync(path.join(__dirname, 'dist', 'install.html'), html);
console.log(`built dist/layout-tool.js (${code.length} bytes) and dist/install.html`);
