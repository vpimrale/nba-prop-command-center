# Vendored: tesseract.js 5.1.1 (main browser bundle only)

Why vendored: v6.0 loaded `https://cdn.jsdelivr.net/npm/tesseract.js@5/...` — a
floating major version with no integrity check, so any new 5.x publish (or a CDN
compromise) changed the code this app runs. v6.1 pins the exact file in-repo.

| Item | Value |
|---|---|
| Package | `tesseract.js@5.1.1` (npm) — Apache-2.0 (see `LICENSE.md`) |
| npm tarball integrity (verified on download) | `sha512-lzVl/Ar3P3zhpUT31NjqeCo1f+D5+YfpZ5J62eo2S14QNVOmHBTtbchHm/YAbOOOzCegFnKf4B3Qih9LuldcYQ==` |
| File | `dist/tesseract.min.js` copied unmodified |
| sha256 | `a8e29918d098b2b06e1012bdaeffb4aec0445c5d5654709023e0bd1f442a80e8` |
| sha384 (SRI form) | `sha384-GJqSu7vueQ9qN0E9yLPb3Wtpd7OrgK8KmYzC8T1IysG1bcvxvIO4qtYR/D3A991F` |

`tests/unit/vendor-integrity.test.js` fails if this file's sha256 ever changes.

What is still fetched from a CDN at OCR time (by tesseract.js itself, NOT
floating — each URL is derived from this exact build's own version numbers):

- worker: `https://cdn.jsdelivr.net/npm/tesseract.js@v5.1.1/dist/worker.min.js`
- core (WASM): `https://cdn.jsdelivr.net/npm/tesseract.js-core@v5.1.1`
- English data: `https://cdn.jsdelivr.net/npm/@tesseract.js-data/eng/4.0.0_best_int`

The worker is deliberately left on its default CDN path: tesseract.js loads it
through a blob worker with `importScripts`, which cannot load a `file://` URL, so
vendoring it would break OCR for anyone opening `index.html` directly as a file.

To upgrade: `npm pack tesseract.js@X.Y.Z`, verify the tarball against
`npm view tesseract.js@X.Y.Z dist.integrity`, copy `dist/tesseract.min.js`, update
this file, the folder name, the loader path in `app.js`, `sw.js`, and the expected
hash in the integrity test.
