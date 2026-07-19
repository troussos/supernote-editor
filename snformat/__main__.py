"""Command-line utility: python -m snformat <command> file.note

Commands:
  info FILE            print structure summary (version, pages, layers, sizes)
  verify FILE          full parse + decode + unedited-rewrite verification
  render FILE OUTDIR   render every page to PNG (for inspection/backup)
"""
import os
import sys


def cmd_info(path: str) -> int:
    from . import parse_file
    nb = parse_file(path)
    w, h = nb.page_size()
    print(f"{os.path.basename(path)}")
    print(f"  signature : {nb.signature}  (file type {nb.filetype})")
    print(f"  equipment : {nb.equipment}  page size {w}x{h}")
    print(f"  pages     : {len(nb.pages)}")
    print(f"  keywords/titles/links: {sum(1 for e in nb.entries if e.kind == 'KEYWORD')}/"
          f"{sum(1 for e in nb.entries if e.kind == 'TITLE')}/"
          f"{sum(1 for e in nb.entries if e.kind == 'LINK')}")
    print(f"  styles    : {', '.join(nb.styles) or '(none)'}")
    for i, page in enumerate(nb.pages):
        layers = ", ".join(
            f"{slot}({len(l.bitmap)}B)" if l.bitmap else slot
            for slot, l in page.layers.items())
        extras = [k for k, v in (("path", page.totalpath), ("recogn", page.recognfile),
                                 ("idtable", page.idtable)) if v]
        print(f"  page {i + 1:3d}: style={page.style} orient={page.orientation} "
              f"[{layers}]{' +' + '+'.join(extras) if extras else ''}")
    for wmsg in nb.warnings:
        print(f"  WARNING: {wmsg}")
    return 0


def cmd_verify(path: str) -> int:
    from . import roundtrip_check
    problems = roundtrip_check(path)
    if problems:
        print(f"ISSUES for {path}:")
        for p in problems:
            print("  -", p)
        return 1
    print(f"OK: {path} parses cleanly, every bitmap decodes, and an unedited "
          "rewrite is verified faithful.")
    return 0


def cmd_render(path: str, outdir: str) -> int:
    from PIL import Image

    from . import parse_file
    from .render import composite_page
    nb = parse_file(path)
    os.makedirs(outdir, exist_ok=True)
    base = os.path.splitext(os.path.basename(path))[0]
    for i, page in enumerate(nb.pages):
        out = os.path.join(outdir, f"{base}_p{i + 1:03d}.png")
        Image.fromarray(composite_page(nb, page)).save(out)
        print("wrote", out)
    return 0


def main(argv=None) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    if len(argv) >= 2 and argv[0] == "info":
        return cmd_info(argv[1])
    if len(argv) >= 2 and argv[0] == "verify":
        return cmd_verify(argv[1])
    if len(argv) >= 3 and argv[0] == "render":
        return cmd_render(argv[1], argv[2])
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
