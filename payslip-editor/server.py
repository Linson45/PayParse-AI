"""
server.py - local web server for the payslip template editor.

Run:
  pip install pymupdf fastapi uvicorn python-multipart
  python server.py
Then open http://localhost:8000 in your browser.
"""

import json
import re
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response

import extractor
import renderer

BASE = Path(__file__).parent
TEMPLATES = BASE / "templates"
TEMPLATES.mkdir(exist_ok=True)

app = FastAPI(title="Payslip template editor")


def template_path(name):
    safe = re.sub(r"[^A-Za-z0-9_-]+", "_", name).strip("_")
    if not safe:
        raise HTTPException(400, "Give the template a name using letters or numbers.")
    return TEMPLATES / f"{safe}.json"


@app.get("/")
def editor():
    return FileResponse(BASE / "static" / "index.html")


@app.post("/api/extract")
async def extract_pdf(file: UploadFile = File(...)):
    if not file.filename.lower().endswith(".pdf"):
        raise HTTPException(400, "Upload a PDF file.")
    data = await file.read()
    try:
        tpl = extractor.extract(data)
    except ValueError as e:
        raise HTTPException(422, str(e))
    except Exception as e:
        raise HTTPException(422, f"Could not read this PDF: {e}")
    tpl["name"] = Path(file.filename).stem
    return tpl


@app.get("/api/templates")
def list_templates():
    return sorted(p.stem for p in TEMPLATES.glob("*.json"))


@app.get("/api/templates/{name}")
def load_template(name: str):
    path = template_path(name)
    if not path.exists():
        raise HTTPException(404, f"No saved template called {name}.")
    return JSONResponse(json.loads(path.read_text(encoding="utf-8")))


@app.post("/api/templates/{name}")
async def save_template(name: str, template: dict):
    path = template_path(name)
    template["name"] = path.stem
    path.write_text(json.dumps(template), encoding="utf-8")
    return {"saved": path.stem}


@app.post("/api/preview")
async def preview(template: dict):
    """PDF of the template filled with the example values."""
    pdf = renderer.render_pdf(template)
    return Response(pdf, media_type="application/pdf",
                    headers={"Content-Disposition": "inline; filename=preview.pdf"})


@app.post("/api/csv-sheet")
async def csv_sheet(template: dict):
    name = template.get("name") or "template"
    return Response(renderer.csv_sheet(template), media_type="text/csv",
                    headers={"Content-Disposition": f"attachment; filename={name}_workers.csv"})


@app.post("/api/generate")
async def generate(template: str = Form(...), csv_file: UploadFile = File(...)):
    """One PDF per worker row in the CSV, downloaded as a ZIP."""
    tpl = json.loads(template)
    text = (await csv_file.read()).decode("utf-8-sig", errors="replace")
    try:
        zip_bytes, count, missing = renderer.render_zip_from_csv(tpl, text)
    except ValueError as e:
        raise HTTPException(422, str(e))
    headers = {"Content-Disposition": f"attachment; filename={tpl.get('name', 'payslips')}_payslips.zip",
               "X-Payslip-Count": str(count),
               "X-Missing-Columns": ",".join(missing),
               "Access-Control-Expose-Headers": "X-Payslip-Count, X-Missing-Columns"}
    return Response(zip_bytes, media_type="application/zip", headers=headers)


if __name__ == "__main__":
    import uvicorn
    print("Open http://localhost:8000 in your browser")
    uvicorn.run(app, host="127.0.0.1", port=8000)
