"""
extractor.py - read a payslip/bill PDF and turn it into an editable template (JSON).

Every piece of the page becomes an "element" with an exact position (in PDF points,
origin top-left):

  text   fixed text such as labels, headings, notes
  field  a value that changes per worker, shown as {{key}}
  line   solid / dotted / dashed line
  rect   box or shaded area
  image  logo or other picture (stored inside the JSON as a data URL)

Command line:
  python extractor.py payslip.pdf            -> payslip_template.json
"""

import base64
import json
import re
import sys
import uuid
from pathlib import Path

import fitz  # PyMuPDF

# ----------------------------------------------------------------------------
# Field detection (which text is a value that changes per worker)
# ----------------------------------------------------------------------------

AMOUNT_RE = re.compile(r"^[₹$€£]?\s?-?\d[\d,]*(\.\d+)?$")
DATE_RE = re.compile(
    r"\b\d{1,4}[-/.]\d{1,2}[-/.]\d{1,4}\b"
    r"|\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*[ ,.-]+\d{2,4}\b",
    re.I,
)
INLINE_AMOUNT_RE = re.compile(r"(?<![\w.{])[₹$€£]?\d[\d,]*\.\d{2}(?![\w}])")

# Labels that often appear in the middle of a line after a value.
# Add your own if a field is not split correctly.
KNOWN_LABELS = [
    "UAN NO", "UAN", "PF NO", "PF", "ESI NO", "ESI", "A/C NO", "AC NO", "ACCOUNT NO",
    "BANK", "BANK NAME", "IFSC", "PAN", "PAN NO", "DESIGNATION", "DEPARTMENT", "DEPT",
    "LOCATION", "GENDER", "DOJ", "DATE OF JOINING", "DOB", "MODE", "DAYS", "PAID DAYS",
    "LOP", "LOP DAYS", "WORKING DAYS", "GRADE", "EMP CODE", "EMP ID", "EMPLOYEE ID",
    "EMPLOYEE CODE", "NAME", "EMPLOYEE NAME", "DATE", "MONTH", "TOTAL", "NET PAY",
    "GROSS", "GROSS PAY", "GROSS SALARY",
]
KNOWN = sorted({k.upper() for k in KNOWN_LABELS}, key=len, reverse=True)
STOP_WORDS = {"for", "the", "of", "and", "in", "to"}


def to_key(label):
    words = [w for w in re.split(r"[^a-z0-9]+", label.lower()) if w]
    short = [w for w in words if w not in STOP_WORDS] or words
    if len(short) > 3:
        short = short[:1] + short[-2:]
    return "_".join(short) or "field"


def guess_type(value):
    v = value.strip()
    if DATE_RE.search(v):
        return "date"
    if AMOUNT_RE.match(v):
        return "number"
    return "text"


class FieldRegistry:
    def __init__(self):
        self.fields = {}

    def add(self, label, value):
        key = base = to_key(label)
        n = 2
        while key in self.fields:
            key = f"{base}_{n}"
            n += 1
        self.fields[key] = {"key": key, "label": label.strip(), "example": value.strip(),
                            "type": guess_type(value)}
        return key


def split_label_from_value(chunk):
    """'1025 UAN NO.' -> ('1025', 'UAN NO.')"""
    up = chunk.upper().rstrip(" .")
    for lbl in KNOWN:
        if up.endswith(lbl) and len(up) > len(lbl):
            cut = len(chunk.rstrip(" .")) - len(lbl)
            if chunk[cut - 1] in " \t":
                return chunk[:cut].rstrip(), chunk[cut:].strip()
    words = chunk.split()
    i = len(words)
    while i > 1 and re.fullmatch(r"[A-Z][A-Z/.()&#]+|NO\.?|No\.?", words[i - 1]):
        i -= 1
    if i < len(words):
        return " ".join(words[:i]), " ".join(words[i:])
    return chunk, ""


def find_value_spans(text, reg):
    """Return [(start, end, key)] for every value in a line of text.
    Wide gaps between words are written as two spaces, which separates columns."""
    spans = []
    colons = list(re.finditer(r"\s*:\s*", text))
    pending = ""
    for idx, m in enumerate(colons):
        v_start = m.end()
        v_end = colons[idx + 1].start() if idx + 1 < len(colons) else len(text)
        chunk = text[v_start:v_end]
        if idx == 0:
            label = re.split(r"\s{2,}", text[:m.start()])[-1].strip()
            if re.search(r"\d", label):
                label = split_label_from_value(label)[1] or label
        else:
            label = pending
        if idx + 1 < len(colons):
            parts = re.split(r"(\s{2,})", chunk)
            if len(parts) > 1:
                value, pending = "".join(parts[:-2]), parts[-1].strip()
            else:
                value, pending = split_label_from_value(chunk)
        else:
            # value ends at a wide gap (next column without a colon)
            value, pending = re.split(r"\s{2,}", chunk)[0], ""
        value = value.strip()
        if label and value:
            start = v_start + chunk.index(value)
            spans.append((start, start + len(value), reg.add(label, value)))

    # amounts like 15000.00 without "label :" in front (typical in tables)
    for m in INLINE_AMOUNT_RE.finditer(text):
        if any(a <= m.start() < b for a, b, _ in spans):
            continue
        before = re.findall(r"[A-Za-z][A-Za-z ./]*", text[:m.start()])
        label = before[-1].strip() if before else "amount"
        spans.append((m.start(), m.end(), reg.add(label or "amount", m.group())))
    return spans


# ----------------------------------------------------------------------------
# Reading the PDF
# ----------------------------------------------------------------------------

def new_id():
    return uuid.uuid4().hex[:8]


def int_to_hex(c):
    return f"#{(c >> 16) & 255:02x}{(c >> 8) & 255:02x}{c & 255:02x}"


def rgb_to_hex(c):
    if not c:
        return None
    if len(c) == 1:
        c = (c[0], c[0], c[0])
    if len(c) == 4:  # CMYK
        k = c[3]
        c = ((1 - c[0]) * (1 - k), (1 - c[1]) * (1 - k), (1 - c[2]) * (1 - k))
    return "#" + "".join(f"{max(0, min(255, round(v * 255))):02x}" for v in c[:3])


def read_words(page):
    """Words with position and style: dicts with x0,x1,top,base,size,bold,color,text,line."""
    words = []
    raw = page.get_text("rawdict")
    for b_no, block in enumerate(raw["blocks"]):
        if block.get("type") != 0:
            continue
        for l_no, line in enumerate(block["lines"]):
            if abs(line["dir"][1]) > 0.1:       # skip rotated text
                continue
            for span in line["spans"]:
                bold = bool(span["flags"] & 16) or "bold" in span["font"].lower()
                style = {"size": round(span["size"], 1), "bold": bold,
                         "color": int_to_hex(span["color"]), "line": (b_no, l_no)}
                cur = None
                for ch in span["chars"]:
                    c = ch["c"]
                    if c.isspace():
                        if cur:
                            words.append(cur)
                        cur = None
                        continue
                    x0, y0, x1, y1 = ch["bbox"]
                    if cur is None:
                        cur = dict(style, text=c, x0=x0, x1=x1, base=ch["origin"][1])
                    else:
                        cur["text"] += c
                        cur["x1"] = x1
                if cur:
                    words.append(cur)
    return words


def group_lines(words):
    """Group words into visual rows (same baseline), sorted left to right."""
    rows = []
    for w in sorted(words, key=lambda w: (round(w["base"]), w["x0"])):
        for row in rows:
            if abs(row[0]["base"] - w["base"]) < 2:
                row.append(w)
                break
        else:
            rows.append([w])
    return [sorted(r, key=lambda w: w["x0"]) for r in rows]


def text_elements_for_row(row, reg, page_no):
    """Split one row into fixed text elements and field elements."""
    # build the row text; a wide gap becomes two spaces (column break)
    text, offsets = "", []
    for i, w in enumerate(row):
        if i:
            gap = w["x0"] - row[i - 1]["x1"]
            text += "  " if gap > w["size"] * 0.9 else " "
        offsets.append((len(text), len(text) + len(w["text"])))
        text += w["text"]

    spans = find_value_spans(text, reg)
    word_key = [None] * len(row)
    for s, e, key in spans:
        for i, (a, b) in enumerate(offsets):
            if a < e and b > s:
                word_key[i] = key

    elements, run = [], []

    def flush(kind, key=None):
        if not run:
            return
        first = run[0]
        el = {"id": new_id(), "page": page_no, "type": kind,
              "x": round(first["x0"], 2), "y": round(first["base"], 2),
              "size": first["size"], "bold": first["bold"], "color": first["color"]}
        if kind == "field":
            el["key"] = key
        else:
            el["text"] = " ".join(w["text"] for w in run)
        elements.append(el)
        run.clear()

    current = ("text", None)
    for i, w in enumerate(row):
        kind = ("field", word_key[i]) if word_key[i] else ("text", None)
        big_gap = i and (w["x0"] - row[i - 1]["x1"]) > w["size"] * 0.9
        style_change = run and (w["size"] != run[0]["size"] or w["bold"] != run[0]["bold"])
        if run and (kind != current or big_gap or style_change):
            flush(*current)
        current = kind
        run.append(w)
    flush(*current)
    return elements


def drawing_elements(page, page_no):
    """Lines, boxes and dotted lines from the page's vector drawings."""
    elements, pieces = [], []
    for d in page.get_drawings():
        rect = d.get("rect")
        stroke = rgb_to_hex(d.get("color"))
        fill = rgb_to_hex(d.get("fill"))
        width = d.get("width") or 1.0
        dashes = (d.get("dashes") or "").strip()
        dashed = bool(dashes) and not dashes.startswith("[] ")
        style = ("dotted" if width <= 1.5 else "dashed") if dashed else "solid"

        # tiny drawing = one dot of a dotted line
        if rect is not None and rect.width < 6 and rect.height < 4:
            pieces.append(((rect.y0 + rect.y1) / 2, rect.x0, rect.x1, fill or stroke or "#000000"))
            continue

        for item in d["items"]:
            kind = item[0]
            if kind == "l":
                a, b = item[1], item[2]
                if stroke and abs(a.x - b.x) + abs(a.y - b.y) > 2:
                    elements.append({"id": new_id(), "page": page_no, "type": "line",
                                     "x1": round(a.x, 2), "y1": round(a.y, 2),
                                     "x2": round(b.x, 2), "y2": round(b.y, 2),
                                     "width": round(width, 2), "color": stroke, "style": style})
            elif kind == "re":
                r = item[1]
                if r.width < 1 and r.height < 1:
                    continue
                if r.height <= 2.5 or r.width <= 2.5:   # a thin filled rectangle = a line
                    color = fill or stroke or "#000000"
                    if r.height <= 2.5:
                        y = (r.y0 + r.y1) / 2
                        line = (r.x0, y, r.x1, y, max(r.height, 0.5))
                    else:
                        x = (r.x0 + r.x1) / 2
                        line = (x, r.y0, x, r.y1, max(r.width, 0.5))
                    elements.append({"id": new_id(), "page": page_no, "type": "line",
                                     "x1": round(line[0], 2), "y1": round(line[1], 2),
                                     "x2": round(line[2], 2), "y2": round(line[3], 2),
                                     "width": round(line[4], 2), "color": color, "style": style})
                else:
                    if fill and fill.lower() == "#ffffff" and not d.get("color"):
                        continue                          # white background box, skip
                    elements.append({"id": new_id(), "page": page_no, "type": "rect",
                                     "x": round(r.x0, 2), "y": round(r.y0, 2),
                                     "w": round(r.width, 2), "h": round(r.height, 2),
                                     "stroke": stroke if d.get("type") in ("s", "fs") else None,
                                     "fill": fill if d.get("type") in ("f", "fs") else None,
                                     "width": round(width, 2)})

    # group tiny dots on the same row into one dotted line
    pieces.sort()
    group = []
    for p in pieces + [(1e9, 0, 0, None)]:
        if group and abs(p[0] - group[0][0]) > 1.5:
            if len(group) >= 5:
                y = sum(g[0] for g in group) / len(group)
                elements.append({"id": new_id(), "page": page_no, "type": "line",
                                 "x1": round(min(g[1] for g in group), 2), "y1": round(y, 2),
                                 "x2": round(max(g[2] for g in group), 2), "y2": round(y, 2),
                                 "width": 1.0, "color": group[0][3], "style": "dotted"})
            group = []
        group.append(p)
    return elements


def image_elements(doc, page, page_no):
    elements = []
    for info in page.get_image_info(xrefs=True):
        xref = info.get("xref")
        if not xref:
            continue
        try:
            pix = fitz.Pixmap(doc, xref)
            smask = doc.xref_get_key(xref, "SMask")
            if smask[0] == "xref":
                mask = fitz.Pixmap(doc, int(smask[1].split()[0]))
                pix = fitz.Pixmap(pix, mask)
            if pix.n - pix.alpha >= 4:
                pix = fitz.Pixmap(fitz.csRGB, pix)
            png = pix.tobytes("png")
        except Exception:
            continue
        x0, y0, x1, y1 = info["bbox"]
        elements.append({"id": new_id(), "page": page_no, "type": "image",
                         "x": round(x0, 2), "y": round(y0, 2),
                         "w": round(x1 - x0, 2), "h": round(y1 - y0, 2),
                         "src": "data:image/png;base64," + base64.b64encode(png).decode()})
    return elements


def extract(pdf_bytes_or_path):
    if isinstance(pdf_bytes_or_path, (bytes, bytearray)):
        doc = fitz.open(stream=pdf_bytes_or_path, filetype="pdf")
    else:
        doc = fitz.open(str(pdf_bytes_or_path))

    reg = FieldRegistry()
    pages, elements = [], []
    for page_no, page in enumerate(doc):
        pages.append({"width": round(page.rect.width, 2), "height": round(page.rect.height, 2)})
        # order = drawing order in the editor: boxes, images, lines, then text on top
        drawings = drawing_elements(page, page_no)
        elements += [e for e in drawings if e["type"] == "rect"]
        elements += image_elements(doc, page, page_no)
        elements += [e for e in drawings if e["type"] == "line"]
        for row in group_lines(read_words(page)):
            elements += text_elements_for_row(row, reg, page_no)

    if not any(e["type"] in ("text", "field") for e in elements):
        raise ValueError("No text found in this PDF. Scanned PDFs need OCR first.")

    return {"version": 1, "pages": pages, "fields": list(reg.fields.values()),
            "elements": elements}


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit(__doc__)
    src = Path(sys.argv[1])
    tpl = extract(src)
    out = src.with_name(src.stem + "_template.json")
    out.write_text(json.dumps(tpl, indent=1), encoding="utf-8")
    print(f"Found {len(tpl['fields'])} fields and {len(tpl['elements'])} elements -> {out}")
    for f in tpl["fields"]:
        print(f"  {{{{{f['key']}}}}}  ({f['type']})  e.g. {f['example']}")
