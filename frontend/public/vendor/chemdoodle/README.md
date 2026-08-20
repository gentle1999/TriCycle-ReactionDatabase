# ChemDoodle Web Components

- Version: `11.0.0`
- Upstream: <https://web.chemdoodle.com/downloads/ChemDoodleWeb-11.0.0.zip>
- Archive SHA-256: `23bd18e8d32f2b287d63dccb0976070e06f24df6a0f2c81ad8b995341fa9e505`
- License: GPL-3.0-or-later; the upstream license is preserved in `COPYING.txt`.
- Included files: `ChemDoodleWeb.js`, `ChemDoodleWeb.css`, `ChemDoodleWeb-uis.js`, and
  `COPYING.txt`.

The ChemDoodle files are copied without modification from the official distribution. Version
11's UI bundle implements `SketcherCanvas` without jQuery, jQuery UI, or Touch Punch. Those
legacy compatibility files are deliberately absent from both the sandboxed editor and the
main Vue application.

Exact file hashes, version markers, source URLs, and licenses are recorded in `manifest.json`.
Run `uv run --frozen python scripts/audit_vendored_assets.py` from the repository root to
verify the local assets and query OSV for every manifest component that has an OSV package
identity. ChemDoodle is distributed as a direct archive rather than an OSV ecosystem package,
so its executable assets are controlled by the upstream archive hash and per-file hashes.
