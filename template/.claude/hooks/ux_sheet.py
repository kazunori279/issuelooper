#!/usr/bin/env python3
"""Contact sheets for the tester's UX run: many screenshots in a few small images.

    python3 "$SHARED_REPO_PATH/.claude/hooks/ux_sheet.py" DIR [--per 12] [--out OUT]
            [--captions FILE.json]

Reads every .png/.jpg under DIR (sorted by path), and writes OUT/sheet-01.jpg, ...
(default OUT = DIR/sheets), each a grid of --per screenshots at reduced size with the
file name (or its caption from FILE.json) under each. Prints one line per sheet.
Run it with a Python that has Playwright and its Chromium installed (the project's own
environment when it tests with Playwright).

Why: a UX run that opens every screenshot puts all of them into one conversation, and a
tester soon went over the model's request size limit that way. Twelve
screenshots on one JPEG sheet cost about as much as one full-size screenshot.
"""

import argparse
import html
import json
import pathlib
import sys

COLS = 4
CELL_W = 470


def sheets(files, per):
    for i in range(0, len(files), per):
        yield files[i : i + per]


def page(batch, captions, title):
    cells = []
    for f in batch:
        cap = captions.get(f.name) or captions.get(str(f)) or f.stem
        cells.append(
            f'<figure><img src="{f.resolve().as_uri()}"><figcaption>{html.escape(cap)[:160]}'
            "</figcaption></figure>"
        )
    return f"""<!doctype html><meta charset="utf-8"><style>
body {{ margin: 8px; font: 13px sans-serif; background: #fff; color: #111; }}
h1 {{ font-size: 14px; margin: 0 0 6px; }}
main {{ display: grid; grid-template-columns: repeat({COLS}, {CELL_W}px); gap: 8px; align-items: start; }}
figure {{ margin: 0; border: 1px solid #bbb; }}
img {{ width: {CELL_W}px; max-height: 700px; object-fit: contain; object-position: top; display: block; background: #eee; }}
figcaption {{ padding: 3px 4px; background: #f4f4f4; }}
</style><h1>{html.escape(title)}</h1><main>{"".join(cells)}</main>"""


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("dir")
    ap.add_argument("--per", type=int, default=12)
    ap.add_argument("--out")
    ap.add_argument("--captions")
    a = ap.parse_args(argv)
    root = pathlib.Path(a.dir)
    out = pathlib.Path(a.out) if a.out else root / "sheets"
    files = sorted(
        p
        for p in root.rglob("*")
        if p.suffix.lower() in (".png", ".jpg", ".jpeg") and out not in p.parents
    )
    if not files:
        print(f"ux_sheet: no screenshots under {root}", file=sys.stderr)
        return 1
    captions = json.loads(pathlib.Path(a.captions).read_text()) if a.captions else {}
    out.mkdir(parents=True, exist_ok=True)
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch()
        pg = browser.new_page(
            viewport={"width": COLS * (CELL_W + 8) + 16, "height": 800}
        )
        batches = list(sheets(files, a.per))
        for n, batch in enumerate(batches, 1):
            html_file = out / f"sheet-{n:02d}.html"
            html_file.write_text(
                page(batch, captions, f"{root.name}: sheet {n} of {len(batches)}")
            )
            pg.goto(html_file.resolve().as_uri())
            pg.wait_for_load_state("load")
            jpg = out / f"sheet-{n:02d}.jpg"
            pg.screenshot(path=str(jpg), full_page=True, type="jpeg", quality=70)
            html_file.unlink()
            print(
                f"{jpg}  ({len(batch)} screenshots: {batch[0].name} .. {batch[-1].name}, "
                f"{jpg.stat().st_size // 1024} KB)"
            )
        browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
