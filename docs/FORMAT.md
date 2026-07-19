# The Supernote `.note` file format

Reverse-engineering notes for the Ratta Supernote X-series notebook format, as
implemented by the `snformat` library. Sources: the Apache-2.0
[supernote-tool](https://github.com/jya-dev/supernote-tool) project, plus
byte-level auditing of 19 real device files spanning file versions
`20220011`–`20260016` (firmware Chauvet 2.5 through 3.28, devices A5X, A6X,
Nomad/N6, Manta/N5). Everything below was verified empirically against those
files; items marked **[new]** are not documented in supernote-tool and were
discovered during this audit.

## Container layout

```
offset 0   : "note" or "mark"                        (4 bytes ASCII)
offset 4   : "SN_FILE_VER_" + 8 digits               (20 bytes ASCII)
offset 24  : blocks...                                (any order)
EOF-8      : "tail"                                   (4 bytes, see note)
EOF-4      : footer block address                     (uint32 LE)
```

Every block is `[uint32 LE payload length][payload]`. All cross-references
are **absolute byte offsets** of a block's length field, written as ASCII
decimal inside metadata. Address `0` means "absent".

**Log-structured writes.** Devices append updated blocks and re-point the
footer instead of rewriting files, so real files usually contain stale
orphan blocks (old headers, old page metadata, old bitmaps) that nothing
references — 7 of our 19 fixtures were fully compacted, the rest carried
dead blocks. A parser must therefore *follow references only*, never scan
sequentially, and must not assume the footer is the last block (files exist
with the live header *after* the footer). A full rewrite that drops orphans
is exactly what the device itself produces for a fresh file.

The `tail` marker precedes the trailing address in every device file we have
seen; one community-generated fixture omits it and still parses everywhere
(readers only use the last 4 bytes).

## Metadata blocks

Metadata payloads are UTF-8 text: `<KEY1:VALUE1><KEY2:VALUE2>...`. Keys may
repeat (parsers must preserve duplicates and order). Values cannot contain
`<` or `>`; JSON-valued keys (`LAYERINFO`) replace `:` with `#` to survive
this framing, and are occasionally Base64-encoded instead.

### Footer

| key | value |
|---|---|
| `FILE_FEATURE` | address of the header metadata block |
| `PAGE1`…`PAGEn` | addresses of page metadata blocks (1-based, contiguous) |
| `COVER_0:0` / `COVER_1` / `COVER_2` | cover image address (0 = none) |
| `KEYWORD_ppppXXXX`, `TITLE_ppppXXXX`, `LINKO_/LINKI_ppppXXXX` | addresses of keyword/title/link metadata blocks; `pppp` = 4-digit page number, suffix encodes position; keys can repeat |
| `STYLE_<name>` | address of a shared background/template block |
| `PDFSTYLELIST` | **[new]** address of a block listing PDF-template styles |
| `DIRTY` | flag, `0` in every file seen; preserved verbatim |

### Header

Device/file properties (`APPLY_EQUIPMENT`, `DEVICE_DPI`, `FILE_ID`,
`FILE_RECOGN_TYPE`, `FINALOPERATION_PAGE`, `PDFSTYLE`, …). No addresses.
`APPLY_EQUIPMENT:N5` (Manta) means 1920x2560 pages; every other observed
equipment (A5X, A6X, N6) is 1404x1872.

### Page metadata

Address keys: `MAINLAYER`, `LAYER1..3`, `BGLAYER` (→ layer metadata),
`TOTALPATH` (stroke/trail data), `RECOGNFILE`, `RECOGNTEXT` (handwriting
recognition caches), and **[new]** `IDTABLE`, `EXTERNALLINKINFO` — the last
two carry real block addresses in modern files and dangle if a rewriter
doesn't know them.

Value keys: `PAGESTYLE` (template name), `PAGESTYLEMD5`, `LAYERSEQ`
(comma-separated compositing order, **topmost first**), `LAYERINFO`
(JSON: layerId/isBackgroundLayer/isVisible/…), `ORIENTATION`
(`1000`=portrait, `1090`/`1270`=landscape-stored rasters, `1180`=portrait
raster), `PAGEID` (`P` + 20-digit timestamp + 12 alphanumerics),
`RECOGNSTATUS`, `RECOGNTYPE`, `RECOGNLANGUAGE`, `RECOGNFILESTATUS`,
`THUMBNAILTYPE`, and (newer firmware) `PAGETEXTBOX`, `DISABLE`, `FIVESTAR`,
`LAYERSWITCH`.

### Layer metadata

`LAYERNAME`, `LAYERTYPE` (`NOTE`/`MARK`), `LAYERPROTOCOL` (`RATTA_RLE`),
`LAYERPATH`, and addresses `LAYERBITMAP`, `LAYERVECTORGRAPH`, `LAYERRECOGN`
(the last two are 0 in every file seen, but are treated as addresses).
`BGLAYER`'s `LAYERBITMAP` points at the **shared** footer `STYLE_*` block
for the page's template — background pixels are deduplicated across pages.
For `user_*` styles (custom templates and PDF templates) that shared block
is a PNG file; for built-in styles it is RATTA_RLE.

Keyword/title/link metadata blocks each carry one content address
(`KEYWORDSITE` — the keyword's UTF-8 text; `TITLEBITMAP` / `LINKBITMAP` —
RLE bitmaps) plus positioning values preserved verbatim.

## Bitmaps: RATTA_RLE

A raster is a byte per pixel, row-major. Byte values are *color codes*, not
gray levels:

| code | meaning |
|---|---|
| `0x61` | black |
| `0x62` | transparent background |
| `0x65` | white ink |
| `0x63`/`0x64` | dark gray / gray (files < 20230015; "compat" shades on newer devices) |
| `0x9D`/`0xC9` | dark gray / gray (files ≥ 20230015) |
| `0x66`, `0x67`/`0x68`, `0x9E`/`0xCA` | marker/highlighter variants of the above |
| anything else | **direct 8-bit grayscale** (files ≥ 20230015 use this for antialiased ink) |

Compression is run-length pairs `(code, length_byte)`:

- `0x00–0x7F` → run of `length+1` pixels (1–128);
- `0xFF` → run of `0x4000` pixels (`0x400` when decoding the special "blank
  white template" block, recognized by BGLAYER + `style_white` + a block of
  exactly `0x140E` bytes);
- high bit set (`0x80–0xFE`) → *held pair*: if the next pair has the same
  code, the run is `1 + next_length + ((held & 0x7F) + 1) * 128`; on a
  different code the held pair alone contributes `((held & 0x7F) + 1) * 128`
  pixels; at end-of-stream the held run is clipped by halving until it fits
  the expected pixel count.

The decoded pixel count must equal exactly `width*height`.

**Writing strategy:** this library's encoder emits only the single-pair and
*complete* two-pair forms (max `0x4000` px per unit) and never emits `0xFF`,
so its output is unambiguous under both decode modes and independent of the
tail-clipping heuristic, at a cost of a few hundred bytes per blank layer.
Device decoders accept this (devices themselves emit two-pair forms; every
rewrite is validated by decode-back plus an independent parser).

## Rewriting files safely

The writer rebuilds the file from the reference graph in the device's
canonical order (header, cover, keywords/titles/links, styles, pages
(bitmap → layer meta → aux blocks → page meta), footer, tail, address), so
all references point backward and addresses are final on first placement.
Invariants enforced by `snformat`:

1. every metadata block must be fully expressible as `<K:V>` pairs (parse
   rejects files with residue rather than silently dropping bytes);
2. unedited blocks are emitted byte-for-byte; only layers whose raster the
   user actually changed are re-encoded;
3. a save must re-parse and re-decode cleanly and match the model
   (`verify_equivalent`) *before* the destination file is touched;
4. preserved numeric values that happen to equal a source block address are
   reported (defense against future firmware adding unknown address keys);
5. compacted round-trips of orphan-free device files are byte-identical.

### Derived data on edited pages

`TOTALPATH` stores per-stroke vector data (used by the device's
stroke-eraser/lasso), and `RECOGNFILE`/`RECOGNTEXT` cache handwriting
recognition. After a bitmap edit these describe ink that no longer exists,
so the library clears them for edited pages and resets `RECOGNSTATUS` — the
same state as a freshly written page (`TOTALPATH:0` occurs naturally in
device files). Consequence on-device: on pages edited here, "erase entire
stroke" treats old ink as bitmap ink rather than individual strokes, and
recognition re-runs. Unedited pages keep all three verbatim.
