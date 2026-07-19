"""Safe save pipeline.

Order of operations (nothing can corrupt the original at any step):
  1. serialize in memory
  2. verify semantic equivalence of the serialized bytes (hard gate)
  3. write to a temp file in the destination directory, fsync
  4. re-parse the temp file from disk (catches I/O-level corruption)
  5. timestamped backup of the existing destination (rotated)
  6. atomic os.replace(temp, destination)
"""
from __future__ import annotations

import datetime as _dt
import os
import shutil

from .model import SnNotebook
from .parse import parse_bytes, parse_file
from .verify import verify_equivalent
from .write import serialize

BACKUP_SUFFIX = ".bak-"


class SaveError(RuntimeError):
    def __init__(self, message: str, problems: list[str] | None = None):
        super().__init__(message)
        self.problems = problems or []


def save_notebook(
    nb: SnNotebook,
    path: str,
    *,
    keep_backups: int = 5,
) -> list[str]:
    """Save notebook to path. Returns non-fatal warnings. Raises SaveError."""
    data, encoded, warnings = serialize(nb)

    problems = verify_equivalent(nb, data)
    if problems:
        raise SaveError(
            "save aborted — serialized file failed verification; the file on disk was NOT touched",
            problems,
        )

    directory = os.path.dirname(os.path.abspath(path)) or "."
    tmp_path = os.path.join(directory, f".{os.path.basename(path)}.sn-tmp")
    try:
        with open(tmp_path, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        reparsed = parse_file(tmp_path)   # full structural + raster validation from disk
        if os.path.exists(path):
            _make_backup(path, keep_backups)
        os.replace(tmp_path, path)
    except SaveError:
        raise
    except Exception as e:
        raise SaveError(f"save failed: {type(e).__name__}: {e}") from e
    finally:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass

    # Sync the in-memory model to the saved state.
    for (pi, slot), bitmap in encoded.items():
        layer = nb.pages[pi].layers[slot]
        layer.bitmap = bitmap
        layer.dirty = False
    for page in nb.pages:
        page.ink_dirty = False
    nb.source_path = path
    nb.source_size = len(data)
    nb.source_block_starts = reparsed.source_block_starts
    return warnings


def _make_backup(path: str, keep: int) -> None:
    stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = f"{path}{BACKUP_SUFFIX}{stamp}"
    if os.path.exists(backup):   # two saves within a second
        for i in range(1, 100):
            backup = f"{path}{BACKUP_SUFFIX}{stamp}-{i}"
            if not os.path.exists(backup):
                break
    shutil.copy2(path, backup)
    # rotate
    directory = os.path.dirname(os.path.abspath(path)) or "."
    base = os.path.basename(path) + BACKUP_SUFFIX
    backups = sorted(
        f for f in os.listdir(directory) if f.startswith(base)
    )
    for old in backups[:-keep] if keep > 0 else []:
        try:
            os.remove(os.path.join(directory, old))
        except OSError:
            pass


def roundtrip_check(path: str) -> list[str]:
    """Parse a file and verify an unedited rewrite is faithful (diagnostics)."""
    nb = parse_file(path)
    data, _, warnings = serialize(nb)
    problems = verify_equivalent(nb, data)
    return problems + warnings
