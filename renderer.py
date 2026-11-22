"""
renderer.py - draw a template (JSON) filled with one worker's data into a PDF.
"""

import base64
import csv
import io
import re
import zipfile
from pathlib import Path

import fitz  # PyMuPDF

DASHES = {"solid": None, "dotted": "[1 2] 0", "dashed": "[4 3] 0"}

# A Unicode font so symbols like ₹ print correctly. The first one found is used.
# Arial/Liberation come first because they have the same widths as Helvetica.
FONT_CANDIDATES = [
    ("C:/Windows/Fonts/arial.ttf", "C:/Windows/Fonts/arialbd.ttf"),
    ("/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
     "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"),
    ("/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
     "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf"),
    ("/Library/Fonts/Arial.ttf", "/Library/Fonts/Arial Bold.ttf"),
    ("/System/Library/Fonts/Supplemental/Arial.ttf", "/System/Library/Fonts/Supplemental/Arial Bold.ttf"),
]


def _find_fonts():
    for regular, bold in FONT_CANDIDATES:
        if Path(regular).exists():
            return regular, bold if Path(bold).exists() else regular
    return None, None


FONT_REGULAR, FONT_BOLD = _find_fonts()


def hex_to_rgb(h, default=(0, 0, 0)):
    if not h:
        return default
    h = h.lstrip("#")
    if len(h) != 6:
        return default
    return tuple(int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))


def draw_text(page, x, y, text, size, bold, color):
    if not text:
        return
    kwargs = {"fontsize": size, "color": hex_to_rgb(color)}
    if FONT_REGULAR:
        kwargs["fontname"] = "F1" if bold else "F0"
        kwargs["fontfile"] = FONT_BOLD if bold else FONT_REGULAR
    else:
        kwargs["fontname"] = "hebo" if bold else "helv"
        text = text.replace("₹", "Rs.")          # built-in fonts have no ₹
    page.insert_text((x, y), str(text), **kwargs)


def render_pdf(template, data=None):
    """Return PDF bytes. data = {field_key: value}; missing keys use the example value."""
    examples = {f["key"]: f.get("example", "") for f in template.get("fields", [])}
    values = dict(examples, **(data or {}))

    doc = fitz.open()
    pages = [doc.new_page(width=p["width"], height=p["height"]) for p in template["pages"]]

    for el in template["elements"]:
        page = pages[el.get("page", 0)]
        t = el["type"]
        if t == "rect":
            page.draw_rect(fitz.Rect(el["x"], el["y"], el["x"] + el["w"], el["y"] + el["h"]),
                           color=hex_to_rgb(el.get("stroke"), None) if el.get("stroke") else None,
                           fill=hex_to_rgb(el.get("fill"), None) if el.get("fill") else None,
                           width=el.get("width", 1))
        elif t == "line":
            page.draw_line((el["x1"], el["y1"]), (el["x2"], el["y2"]),
                           color=hex_to_rgb(el.get("color")), width=el.get("width", 1),
                           dashes=DASHES.get(el.get("style", "solid")))
        elif t == "image":
            raw = base64.b64decode(el["src"].split(",", 1)[1])
            page.insert_image(fitz.Rect(el["x"], el["y"], el["x"] + el["w"], el["y"] + el["h"]),
                              stream=raw, keep_proportion=False)
        elif t == "text":
            draw_text(page, el["x"], el["y"], el["text"], el["size"], el.get("bold"), el.get("color"))
        elif t == "field":
            draw_text(page, el["x"], el["y"], values.get(el["key"], ""), el["size"],
                      el.get("bold"), el.get("color"))

    out = doc.tobytes(garbage=3, deflate=True)
    doc.close()
    return out


def safe_name(text, fallback):
    s = re.sub(r"[^A-Za-z0-9_-]+", "_", str(text or "")).strip("_")
    return s or fallback


def render_zip_from_csv(template, csv_text):
    """One PDF per CSV row, returned together as a ZIP (bytes)."""
    rows = list(csv.DictReader(io.StringIO(csv_text)))
    if not rows:
        raise ValueError("The CSV has no worker rows.")
    keys = {f["key"] for f in template.get("fields", [])}
    missing = sorted(keys - set(rows[0].keys()))

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        used = set()
        for n, row in enumerate(rows, start=1):
            data = {k: (v or "") for k, v in row.items() if k}
            name_key = next((k for k in data if "name" in k.lower()), None)
            name = safe_name(data.get(name_key), f"worker_{n}")
            while name in used:
                name += f"_{n}"
            used.add(name)
            z.writestr(f"{name}.pdf", render_pdf(template, data))
    return buf.getvalue(), len(rows), missing


def csv_sheet(template):
    """A CSV with one column per field and the example values as the first row."""
    buf = io.StringIO()
    w = csv.writer(buf)
    fields = template.get("fields", [])
    w.writerow([f["key"] for f in fields])
    w.writerow([f.get("example", "") for f in fields])
    return buf.getvalue()
