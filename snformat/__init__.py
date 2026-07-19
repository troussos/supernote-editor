"""snformat — native Supernote .note file reading and writing.

Reverse-engineered format support; see docs/FORMAT.md. Derived from the
Apache-2.0 supernote-tool project plus original byte-level auditing of real
device files (firmware file versions 20220011..20260016).
"""
from .constants import HIGHRES_GRAYSCALE_VERSION, TESTED_VERSIONS
from .kv import Meta, MetadataError
from .model import SnFooterEntry, SnLayer, SnNotebook, SnPage
from .parse import ParseError, parse_bytes, parse_file
from .rle import RleError, decode as rle_decode, encode as rle_encode
from .safe_io import SaveError, roundtrip_check, save_notebook
from .verify import verify_equivalent
from .write import WriteError, serialize

__all__ = [
    "HIGHRES_GRAYSCALE_VERSION", "TESTED_VERSIONS",
    "Meta", "MetadataError",
    "SnFooterEntry", "SnLayer", "SnNotebook", "SnPage",
    "ParseError", "parse_bytes", "parse_file",
    "RleError", "rle_decode", "rle_encode",
    "SaveError", "roundtrip_check", "save_notebook",
    "verify_equivalent", "serialize",
]
__version__ = "1.0.0"
