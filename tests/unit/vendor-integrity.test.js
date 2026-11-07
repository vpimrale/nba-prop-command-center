// The OCR library is pinned by CONTENT: this fails if the vendored bundle changes at all,
// and if app.js ever goes back to loading a floating/unpinned CDN copy.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('fs');
const path = require('path');
const crypto = require('crypto');

const ROOT = path.join(__dirname, '..', '..');
const EXPECTED_SHA256 = 'a8e29918d098b2b06e1012bdaeffb4aec0445c5d5654709023e0bd1f442a80e8'; // tesseract.js@5.1.1 dist/tesseract.min.js

test('vendored tesseract.js 5.1.1 bundle matches its recorded hash', () => {
  const buf = fs.readFileSync(path.join(ROOT, 'vendor/tesseract.js-5.1.1/tesseract.min.js'));
  assert.equal(crypto.createHash('sha256').update(buf).digest('hex'), EXPECTED_SHA256);
});

test('app.js loads the vendored bundle and never a floating CDN version', () => {
  const app = fs.readFileSync(path.join(ROOT, 'app.js'), 'utf8');
  assert.match(app, /TESSERACT_SCRIPT_PATH = 'vendor\/tesseract\.js-5\.1\.1\/tesseract\.min\.js'/);
  assert.doesNotMatch(app, /tesseract\.js@5\//);
  assert.doesNotMatch(app, /cdn\.jsdelivr\.net\/npm\/tesseract/);
});

test('service worker shell lists every script index.html loads', () => {
  const html = fs.readFileSync(path.join(ROOT, 'index.html'), 'utf8');
  const sw = fs.readFileSync(path.join(ROOT, 'sw.js'), 'utf8');
  const scripts = [...html.matchAll(/<script src="([^"]+)"/g)].map(m => m[1]);
  assert.ok(scripts.includes('game-binding.js'));
  for (const s of scripts) assert.ok(sw.includes(`'./${s}'`), `sw.js shell is missing ${s}`);
  assert.ok(sw.includes("'./vendor/tesseract.js-5.1.1/tesseract.min.js'"));
});
