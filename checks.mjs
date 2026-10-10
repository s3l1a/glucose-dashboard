import assert from 'node:assert/strict';
import { readFileSync, readdirSync, statSync } from 'node:fs';
import { dirname, resolve, sep } from 'node:path';
import { fileURLToPath } from 'node:url';
import { Script } from 'node:vm';

const root = dirname(fileURLToPath(import.meta.url));
const web = resolve(root, 'web');
const html = readFileSync(resolve(web, 'index.html'), 'utf8');
const sw = readFileSync(resolve(web, 'sw.js'), 'utf8');
const manifest = JSON.parse(readFileSync(resolve(web, 'manifest.json'), 'utf8'));
const libraries = new Set([
  'https://cdn.jsdelivr.net/npm/chart.js@4.5.1',
  'https://cdn.jsdelivr.net/npm/chartjs-plugin-annotation@3.0.1'
]);

function checkReference(reference) {
  if (libraries.has(reference)) return;
  assert(!/^(?:[a-z][a-z\d+.-]*:|\/\/|\/|\\)/i.test(reference), `Not relative: ${reference}`);
  const path = decodeURIComponent(reference.split(/[?#]/)[0]);
  if (!path || path === './glucose_data.json' || path === 'glucose_data.json') return;
  const target = resolve(web, path);
  assert(target === web || target.startsWith(web + sep), `Outside web: ${reference}`);
  assert(statSync(target), `Missing local asset: ${reference}`);
}

let inlineCount = 0;
for (const match of html.matchAll(/<script\b([^>]*)>([\s\S]*?)<\/script\s*>/gi)) {
  if (/\bsrc\s*=/.test(match[1])) continue;
  new Script(match[2], { filename: `index.html:inline-${++inlineCount}` });
}
assert(inlineCount > 0, 'No inline JavaScript checked');
new Script(sw, { filename: 'sw.js' });

assert.equal(manifest.start_url, './');
checkReference(manifest.start_url);
assert(Array.isArray(manifest.icons) && manifest.icons.length > 0, 'Manifest icons missing');
for (const icon of manifest.icons) {
  assert(!libraries.has(icon.src), 'Icon must be local');
  checkReference(icon.src);
  assert.equal(icon.type, 'image/svg+xml');
}
for (const match of html.matchAll(/\b(?:src|href)\s*=\s*(["'])(.*?)\1/gi)) {
  checkReference(match[2]);
}
for (const library of libraries) {
  assert(html.includes(`src="${library}"`), `Missing pinned script: ${library}`);
  assert(sw.includes(`'${library}'`), `Missing cached script: ${library}`);
}

const markup = html.replace(/<script\b[^>]*>[\s\S]*?<\/script\s*>/gi, '')
  .replace(/<style\b[^>]*>[\s\S]*?<\/style\s*>/gi, '');
assert(/Not medical advice\. Not for treatment\./.test(markup), 'Permanent disclaimer missing');
assert(markup.includes('Synthetic demonstrations are not medical data.'), 'Permanent demo wording missing');
assert(/id="dataStatus"[^>]*>No data loaded<\//.test(markup), 'Default data banner missing');
assert(html.includes('SYNTHETIC DEMONSTRATION - NOT MEDICAL DATA'), 'Synthetic banner missing');
assert(html.includes('Private live data - do not share'), 'Live banner missing');
assert(html.includes('data.synthetic === true'), 'Explicit synthetic check missing');
assert(html.includes("fetch('./glucose_data.json', { cache: 'no-store' })"), 'Data fetch must bypass cache');
assert(sw.includes("fetch(event.request, { cache: 'no-store' })"), 'Worker data fetch must bypass cache');
assert(html.includes("register('./sw.js').catch("), 'Relative worker registration or rejection handling missing');
assert(html.includes('!lastSynthetic'), 'Synthetic sound suppression missing');
assert(markup.includes('Enable sound'), 'Sound opt-in missing');
assert(markup.includes('Local engine'), 'Generic footer missing');
assert(!/alarm\.mp3|<audio\b|tailscale|dexcom|pi engine|flaticon/i.test(html + sw + JSON.stringify(manifest)), 'Excluded branding or audio asset');

const allowed = new Set(['index.html', 'manifest.json', 'sw.js', 'icon.svg']);
for (const name of readdirSync(web)) {
  if (name === 'glucose_data.json') continue;
  assert(allowed.has(name), `Unexpected export asset: ${name}`);
  assert(statSync(resolve(web, name)).isFile(), `Unexpected export directory: ${name}`);
  assert(!/[^\x00-\x7f]/.test(readFileSync(resolve(web, name), 'utf8')), `Non-ASCII source: ${name}`);
}
assert(!/[^\x00-\x7f]/.test(readFileSync(fileURLToPath(import.meta.url), 'utf8')), 'Non-ASCII checks source');
console.log('Checks passed: JavaScript syntax, manifest, local assets, pinned libraries, safety wording, ASCII-only clean export.');
