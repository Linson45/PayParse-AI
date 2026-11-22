# Payslip template editor

Turn an existing payslip or bill PDF into an editable template, fix it with drag and drop,
and generate one payslip per worker from a spreadsheet.

## Setup (once)

```
pip install -r requirements.txt
```

## Start

```
python server.py
```

Open http://localhost:8000 in your browser. Keep the terminal open while you work.

## Using the editor

1. **Open PDF** and choose your payslip. Values that change per worker (name, amounts, dates)
   become yellow fields like `{{name}}`.
2. Fix the template:
   - Click an item to select it, then press **Delete** to remove it (Ctrl+Z undoes).
   - Drag on an empty spot to select many items at once, for example unwanted lines.
   - Drag items to move them; arrow keys move by 1 point (Shift + arrow by 10).
   - Select the logo and press **Replace image** to change it, or drag its corners to resize.
   - **Add text / field / line / box / image** adds new items.
   - Select a field to rename it. The name is the column name in the worker sheet.
   - Tick **Show sample values** to see the template with the original values.
3. **Save** the template so you can open it again later with **Open saved**.
4. **Download worker sheet** gives a CSV with one column per field. Open it in Excel,
   add one row per worker, and save it as CSV.
5. **Generate payslips** and choose that CSV. You get a ZIP with one PDF per worker.

## Files

- `extractor.py`  reads the PDF and builds the template (text, fields, lines, boxes, images)
- `renderer.py`   draws a template with a worker's values into a PDF
- `server.py`     the local web server
- `static/index.html` the editor
- `templates/`    saved templates (JSON)
- `samples/`      a sample payslip to try

You can also extract from the command line: `python extractor.py payslip.pdf`

## Limits of this version

- Scanned PDFs (pictures of a payslip) have no text to read and need OCR first.
- Totals like net pay are not calculated; enter them in the worker sheet.
- If a field is split wrongly, add its label to `KNOWN_LABELS` in `extractor.py`,
  or fix it in the editor by deleting it and placing the right field.
