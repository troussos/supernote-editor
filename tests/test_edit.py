"""Editing + safe-save integration tests on real files."""
import io
import os
import shutil

import numpy as np
import pytest

from snformat import parse_file, save_notebook, serialize, verify_equivalent
from snformat.edit_ops import delete_page, duplicate_page, new_page_id, sync_page_after_ink_edit
from snformat.safe_io import SaveError
from tests.conftest import FIXTURE_DIR


@pytest.fixture
def work_note(tmp_path):
    """A multi-page fixture copied somewhere safe to edit."""
    src = os.path.join(FIXTURE_DIR, "1to10.note")
    dst = tmp_path / "work.note"
    shutil.copy(src, dst)
    return str(dst)


def draw_square(nb, page, slot="MAINLAYER", value=0x61):
    raster = nb.layer_raster(page, slot)
    raster[100:300, 100:300] = value
    nb.mark_ink_edited(page, slot)
    sync_page_after_ink_edit(page)


def test_edit_and_save_preserves_everything_else(work_note):
    nb = parse_file(work_note)
    before = [
        (pi, slot, l.bitmap)
        for pi, p in enumerate(nb.pages)
        for slot, l in p.layers.items()
    ]
    draw_square(nb, nb.pages[2])
    warnings = save_notebook(nb, work_note)
    assert warnings == []
    assert not nb.is_dirty()

    nb2 = parse_file(work_note)
    # edited page raster contains the square
    r = nb2.layer_raster(nb2.pages[2], "MAINLAYER")
    assert (r[100:300, 100:300] == 0x61).all()
    # sync policy applied
    assert nb2.pages[2].totalpath is None
    assert nb2.pages[2].meta.get("TOTALPATH") == "0"
    assert nb2.pages[2].meta.get("RECOGNSTATUS") == "0"
    # every untouched layer bitmap survives byte-for-byte
    for pi, slot, bitmap in before:
        if pi == 2 and slot == "MAINLAYER":
            continue
        assert nb2.pages[pi].layers[slot].bitmap == bitmap, f"page {pi} {slot} changed"


def test_saved_edit_renders_in_supernotelib(work_note):
    supernotelib = pytest.importorskip("supernotelib")
    from supernotelib.converter import ImageConverter

    nb = parse_file(work_note)
    draw_square(nb, nb.pages[0])
    save_notebook(nb, work_note)
    oracle = supernotelib.load_notebook(work_note)
    img = np.asarray(ImageConverter(oracle).convert(0).convert("L"))
    assert (img[100:300, 100:300] == 0x00).all()   # black square visible


def test_save_creates_backup_and_original_recoverable(work_note):
    original = open(work_note, "rb").read()
    nb = parse_file(work_note)
    draw_square(nb, nb.pages[0])
    save_notebook(nb, work_note)
    directory = os.path.dirname(work_note)
    backups = [f for f in os.listdir(directory) if ".bak-" in f]
    assert len(backups) == 1
    assert open(os.path.join(directory, backups[0]), "rb").read() == original


def test_failed_verification_never_touches_disk(work_note, monkeypatch):
    original = open(work_note, "rb").read()
    nb = parse_file(work_note)
    draw_square(nb, nb.pages[0])
    monkeypatch.setattr("snformat.safe_io.verify_equivalent", lambda a, b: ["injected failure"])
    with pytest.raises(SaveError):
        save_notebook(nb, work_note)
    assert open(work_note, "rb").read() == original
    assert not [f for f in os.listdir(os.path.dirname(work_note)) if ".bak-" in f]


def test_duplicate_page(work_note):
    nb = parse_file(work_note)
    src_id = nb.pages[4].page_id
    new = duplicate_page(nb, 4)
    assert len(nb.pages) == 11
    assert new.page_id != src_id and new.page_id.startswith("P")
    save_notebook(nb, work_note)
    nb2 = parse_file(work_note)
    assert len(nb2.pages) == 11
    r_src = nb2.layer_raster(nb2.pages[4], "MAINLAYER")
    r_new = nb2.layer_raster(nb2.pages[5], "MAINLAYER")
    assert (r_src == r_new).all()


def test_delete_page(work_note):
    nb = parse_file(work_note)
    keep_id = nb.pages[3].page_id
    delete_page(nb, 2)
    save_notebook(nb, work_note)
    nb2 = parse_file(work_note)
    assert len(nb2.pages) == 9
    assert nb2.pages[2].page_id == keep_id


def test_delete_page_renumbers_entries():
    path = os.path.join(FIXTURE_DIR, "A5X_TestNote_Links.note")
    nb = parse_file(path)
    entry_keys_before = [e.footer_key for e in nb.entries]
    assert any(k.startswith(("LINKO_0002", "LINKI_0002", "KEYWORD_0002", "TITLE_0002"))
               for k in entry_keys_before)
    delete_page(nb, 0)
    for e in nb.entries:
        page_digits = e.footer_key.split("_", 1)[1][:4]
        assert page_digits == "0001"
    data, _, _ = serialize(nb)
    assert verify_equivalent(nb, data) == []


def test_delete_last_page_forbidden():
    nb = parse_file(os.path.join(FIXTURE_DIR, "manta.note"))
    with pytest.raises(ValueError):
        delete_page(nb, 0)


def test_edit_highres_file_with_direct_grayscale(tmp_path):
    src = os.path.join(FIXTURE_DIR, "sn2md_20260618.note")
    dst = str(tmp_path / "hr.note")
    shutil.copy(src, dst)
    nb = parse_file(dst)
    raster = nb.layer_raster(nb.pages[0], "MAINLAYER")
    raster[50:60, 50:600] = 0x9D            # X2 dark gray stroke
    raster[70:80, 50:600] = 0x30            # direct grayscale value
    nb.mark_ink_edited(nb.pages[0], "MAINLAYER")
    sync_page_after_ink_edit(nb.pages[0])
    save_notebook(nb, dst)
    nb2 = parse_file(dst)
    r2 = nb2.layer_raster(nb2.pages[0], "MAINLAYER")
    assert (r2[50:60, 50:600] == 0x9D).all()
    assert (r2[70:80, 50:600] == 0x30).all()


def test_page_id_format():
    pid = new_page_id()
    assert pid.startswith("P") and len(pid) == 33
    assert pid[1:21].isdigit()


def test_bglayer_not_editable():
    nb = parse_file(os.path.join(FIXTURE_DIR, "manta.note"))
    with pytest.raises(ValueError):
        nb.mark_ink_edited(nb.pages[0], "BGLAYER")
