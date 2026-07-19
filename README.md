# Supernote Editor

A Windows desktop app that **natively opens, edits, and writes Supernote
`.note` files** — no PDF/image export round-trips. The file format support
was reverse-engineered (see [docs/FORMAT.md](docs/FORMAT.md)) and is
validated against 19 real device files from firmware 2.5 through 3.28
(A5X, A6X, Nomad, Manta) plus the independent `supernotelib` parser.

## Run it

Double-click **`run_editor.cmd`**, or:

```
.venv\Scripts\python.exe -m supernote_editor [path\to\file.note]
```

(The bundled `.venv` was set up with Python 3.14 + PySide6. To recreate it:
`python -m venv .venv && .venv\Scripts\pip install -r requirements.txt`.)

## Tools

| Tool (key) | What it does |
|---|---|
| **Marquee (M)** | Drag to select. Drag inside the selection to move ink; `Ctrl+C/X/V` copy/cut/paste (paste floats — drag/arrow-keys to position, `Enter`/click-outside anchors, `Esc` cancels); `Del` clears; `Ctrl+A` select page. |
| **Pen (P)** | Draw in Black / Dark gray / Gray / White (device-native ink codes; pressure-sensitive with a pen tablet). `[` `]` change size. |
| **Highlighter (G)** | Draws with the device's marker ink codes. |
| **Eraser (E)** | Erases to transparent background (reveals the template, like the device eraser). |
| **Crop (C)** | Drag a region, press `Enter`: ink outside is cleared on the active layer or all ink layers. Page dimensions are fixed by the device, so crop clears rather than resizes. |
| **Text (T)** | Click to place a text box; type; click a box to re-edit; drag to move. Text stays a vector object during the session and is rasterized ("burned") into device-native ink on save. |
| **Pan (H)** | Drag to pan. Middle-drag or hold `Space` in any tool. `Ctrl+wheel` zooms, `Ctrl+0` fit, `Ctrl+1` 100%. |

Pages panel: click to jump; **Page menu**: duplicate / add blank (same
template) / delete. Layers panel: pick the active ink layer (MAINLAYER /
LAYER1–3) and toggle view-only visibility. Undo/redo: `Ctrl+Z` / `Ctrl+Y`
(page duplicate/delete clear the history).

## Why it won't corrupt your notes

Saving **never touches your file until the result is proven**:

1. The new file is built in memory, re-parsed, every bitmap re-decoded, and
   compared against the editing model (metadata pair-for-pair, untouched
   blocks byte-for-byte, edited rasters pixel-for-pixel). Any discrepancy
   aborts the save with your file untouched.
2. It is then written to a temp file, re-parsed **from disk**, and only then
   swapped in atomically — after a timestamped `*.bak-...` backup of the
   previous version is made next to it (last 5 kept).
3. Layers you didn't edit are copied byte-for-byte from the original;
   compacted device files round-trip **byte-identical**.
4. `File > Verify file integrity` runs the same check on demand, and
   `.venv\Scripts\python.exe -m snformat verify file.note` does it from the
   command line (`info` and `render` are also available).

Text boxes: `.note` has no native typed-text object, so saved text becomes
ink (exactly what the device shows). A small `*.note.snedit.json` sidecar
stores the editable text and the pixels underneath it — reopening the file
in this editor lifts your text boxes back into editable form (only if the
file wasn't modified elsewhere in between; the sidecar is ignored otherwise).
Delete the sidecar anytime; the `.note` file is complete without it.

### Device-sync notes

- On pages whose ink you edited, the stroke table and handwriting-recognition
  caches are reset (they described the old ink). On the device, its
  "erase entire stroke" mode won't single out strokes drawn before the edit
  on those pages — area erasing works as always — and recognition re-runs.
  Untouched pages keep everything verbatim.
- Files from any firmware ≥ Chauvet 2.5 (file version 20220011) are
  supported; newer unknown versions open with a warning and the same
  verified-save safety net.
- Real-time-recognition notebooks, PDF-template notebooks, links, titles,
  keywords, and custom templates are preserved.

## Development

```
.venv\Scripts\python.exe -m pytest        # 161 tests
```

`snformat/` — pure-Python format library (parser, RATTA_RLE codec, verified
writer). `supernote_editor/` — PySide6 app. `tests/fixtures/` — real .note
files from open-source projects (see ATTRIBUTION.md).

Format knowledge builds on [jya-dev/supernote-tool](https://github.com/jya-dev/supernote-tool)
(Apache-2.0); test files from [allenporter/supernote](https://github.com/allenporter/supernote)
(Apache-2.0) and [nelinory/SupernoteSharp](https://github.com/nelinory/SupernoteSharp) (MIT).
This project is unaffiliated with Ratta. **Always keep device backups** — the
app is careful, but e-ink devices are the final arbiter of compatibility.

## License

[Apache License 2.0](LICENSE) — free to use, modify, and redistribute, with an
explicit patent grant and an "as is" disclaimer of warranty and liability to
the author (see sections 7–8). Redistributions must keep the `LICENSE` and
`NOTICE` files and state any significant changes. Bundled third-party
components (format reference, test fixtures) retain their own permissive
licenses; see `NOTICE` and `tests/fixtures/ATTRIBUTION.md`.
