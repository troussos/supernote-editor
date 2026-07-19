"""Constants for the Supernote .note file format.

Format knowledge derived from the Apache-2.0 licensed supernote-tool project
(https://github.com/jya-dev/supernote-tool) plus byte-level auditing of real
device files across firmware versions 20220011..20260016. See docs/FORMAT.md.
"""

TYPE_NOTE = "note"
TYPE_MARK = "mark"

SIGNATURE_PREFIX = "SN_FILE_VER_"
SIGNATURE_LEN = 20          # "SN_FILE_VER_" + 8 digits
FILETYPE_LEN = 4            # "note" / "mark"
HEADER_PREFIX_LEN = FILETYPE_LEN + SIGNATURE_LEN  # blocks start after this

ADDRESS_SIZE = 4
LENGTH_FIELD_SIZE = 4
TAIL_MARKER = b"tail"

# Known signature versions (from supernote-tool; all verified against fixtures
# except the pre-2022 ones, which predate every file we have seen).
KNOWN_VERSIONS = [
    20200001, 20200005, 20200006, 20200007, 20200008,
    20210009, 20210010, 20220011, 20220013, 20230014, 20230015, 20260016,
]
# Versions we have real-file round-trip coverage for.
TESTED_VERSIONS = [20220011, 20220013, 20230014, 20230015, 20260016]
# Firmware generation switch: >= this version uses X2 color codes and permits
# direct 8-bit grayscale bytes in rasters (antialiasing).
HIGHRES_GRAYSCALE_VERSION = 20230015

# Page geometry by equipment (header APPLY_EQUIPMENT).
EQUIPMENT_SIZES = {
    "N5": (1920, 2560),     # A5 X2 "Manta"
}
DEFAULT_PAGE_SIZE = (1404, 1872)  # A5X, A6X, N6 "Nomad", A6 X2 ...

# Page orientation values (page ORIENTATION key). 1090/1270 store rotated
# (landscape) rasters; 1000/1180 store portrait rasters.
ORIENT_PORTRAIT = "1000"
ORIENT_LANDSCAPE_90 = "1090"
ORIENT_PORTRAIT_180 = "1180"
ORIENT_LANDSCAPE_270 = "1270"
LANDSCAPE_ORIENTATIONS = {ORIENT_LANDSCAPE_90, ORIENT_LANDSCAPE_270}

# --- RATTA_RLE ---
COLOR_BLACK = 0x61
COLOR_BACKGROUND = 0x62     # transparent
COLOR_DARK_GRAY = 0x63      # X1-era code
COLOR_GRAY = 0x64           # X1-era code
COLOR_WHITE = 0x65
COLOR_MARKER_BLACK = 0x66
COLOR_MARKER_DARK_GRAY = 0x67
COLOR_MARKER_GRAY = 0x68
# X2-era codes (firmware >= 20230015)
COLOR_DARK_GRAY_X2 = 0x9D
COLOR_MARKER_DARK_GRAY_X2 = 0x9E
COLOR_GRAY_X2 = 0xC9
COLOR_MARKER_GRAY_X2 = 0xCA

RLE_SPECIAL_LENGTH_MARKER = 0xFF
RLE_SPECIAL_LENGTH = 0x4000
RLE_SPECIAL_LENGTH_FOR_BLANK = 0x400
# BGLAYER blocks of exactly this size for style_white pages decode with the
# blank special length (supernote-tool ImageConverter.SPECIAL_WHITE_STYLE_BLOCK_SIZE).
SPECIAL_WHITE_STYLE_BLOCK_SIZE = 0x140E

# Display grayscale values (8-bit) used when compositing for screen.
DISPLAY_BLACK = 0x00
DISPLAY_DARK_GRAY = 0x9D
DISPLAY_GRAY = 0xC9
DISPLAY_WHITE = 0xFE
DISPLAY_TRANSPARENT = 0xFF
DISPLAY_DARK_GRAY_COMPAT = 0x30   # what an X2 device shows for legacy 0x63
DISPLAY_GRAY_COMPAT = 0x50        # what an X2 device shows for legacy 0x64

LAYER_KEYS = ("MAINLAYER", "LAYER1", "LAYER2", "LAYER3", "BGLAYER")
INK_LAYER_KEYS = ("MAINLAYER", "LAYER1", "LAYER2", "LAYER3")

# --- Address-bearing metadata keys ---
# Values of these keys are byte offsets of blocks inside the file. The writer
# must rewrite them after relayout; everything else is preserved verbatim.
# IDTABLE / EXTERNALLINKINFO / PDFSTYLELIST are not documented in
# supernote-tool; they were identified empirically (their values match real
# block offsets in device files).
PAGE_ADDRESS_KEYS = (
    "MAINLAYER", "LAYER1", "LAYER2", "LAYER3", "BGLAYER",
    "TOTALPATH", "RECOGNFILE", "RECOGNTEXT", "IDTABLE", "EXTERNALLINKINFO",
)
LAYER_ADDRESS_KEYS = ("LAYERBITMAP", "LAYERVECTORGRAPH", "LAYERRECOGN")
FOOTER_ADDRESS_EXACT = ("FILE_FEATURE", "PDFSTYLELIST")
FOOTER_ADDRESS_PREFIXES = ("PAGE", "COVER_", "KEYWORD_", "TITLE_", "LINKO_", "LINKI_", "STYLE_")
KEYWORD_ADDRESS_KEYS = ("KEYWORDSITE",)
TITLE_ADDRESS_KEYS = ("TITLEBITMAP",)
LINK_ADDRESS_KEYS = ("LINKBITMAP",)

PROTOCOL_RLE = "RATTA_RLE"
