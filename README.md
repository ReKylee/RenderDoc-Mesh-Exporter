# RenderDoc Mesh Exporter

RenderDoc 1.46+ extension for source-space mesh extraction, UVs, texture snapshots, texture co-occurrence groups, and filesystem-backed model organization.

## Install

Run `install.ps1`, then load **Mesh Exporter** from **Tools > Manage Extensions** in RenderDoc. Export from **Tools > Mesh Exporter > Export meshes and textures...**.

The native path works on conventional non-Xenia captures through RenderDoc's cross-API vertex/index-buffer state. The Xenia adapter additionally reconstructs guest Xbox source-space streams from translated DXBC shader metadata. Surface strips/fans are normalized to explicit non-degenerate triangle lists for GLB/OBJ export so DCC tools do not import degenerate strip-stitch edges; the original captured topology/index stream remains preserved in metadata and raw source indices. Unsupported vertex pulling, packed layouts, missing semantics, mesh-shader topology, or other ambiguous cases are reported instead of guessed.

## Open an export

Every generated folder includes `viewer.cmd`, `viewer.bat`, and `viewer.ps1` at its root. Use whichever launcher fits your shell. Each starts the same localhost-only Python standard-library server and opens the viewer in your default browser. Firefox, Edge, Chrome, and other normal browsers work; no browser-specific filesystem API is used.

Close the command window when you are finished.

## Model browser

- single click: select
- Ctrl-click: toggle selection
- Shift-click: range select
- Ctrl+A: select everything in the current folder and current filters
- drag on empty grid space: box select
- double-click a card: open the textured Three.js viewer
- drag selected cards onto a folder: move them
- use the Move dropdown for large selections without scrolling to the target
- New folder / Rename folder edit the real `meshes\` hierarchy
- Rename with one model selected renames the model folder and its GLB/OBJ/MTL files

The sidebar reflects `meshes\` directly. External Explorer changes are detected automatically. No catalog, database, or JSON file stores organization state. Moving and renaming models repairs OBJ/MTL references and texture-relative paths.

## Textures and materials

Textures sampled together by one draw are retained as a texture co-occurrence group. Where shader evidence supports it, UV associations are preserved. Base-color and normal-map roles remain editable preview assignments. Three.js supports textured previews, orbit/pan/zoom, UV selection, V flipping, normal-green flipping, wireframe, preview PNG export, and assigned GLB export.

Texture-role choices are browser-local. They do not create organization files.

## Output

`meshes\` is the actual model hierarchy. `textures\` contains extracted texture images. `viewer\` contains viewer/runtime files, including the extraction report embedded in its runtime catalog. Organization is the directory tree itself; no organization manifest or JSON file is created.

## Tests

```powershell
python .\tests\test_exporter.py
python .\tests\test_organizer.py
npm ci
npm run build
node .\tests\test_browser.cjs D:\path\to\export
python .\tests\validate_export.py D:\path\to\export
```

The packaged viewer does not need Node.js at runtime.
