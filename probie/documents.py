"""Invoice documents: each case's invoice as the page the vendor actually sent.

In document mode a model gets this image instead of clean invoice fields, the way
an AP clerk gets a PDF, a scan or a phone photo. Each vendor has its own layout,
and each invoice arrives in one of three conditions:

  clean  a digital PDF, rendered crisply
  scan   grey, slightly rotated, a little soft, with scanner grain
  photo  taken on a desk at an angle, with uneven light and a warm cast

Rendering needs Playwright (pip install playwright, then playwright install
chromium). Everything else in Probie works without it.
"""

from __future__ import annotations

import base64
import hashlib
import html
import json
import os
from pathlib import Path

from probie.money import fmt

DOCUMENT_VERSION = 2
# Parts of the page a model can zoom into, and the elements that hold them.
REGIONS = {"header": ".lh", "lines": ".lines", "totals": ".totals", "remit_to": ".remit"}
ZOOM_SCALE = 2
MANIFEST = "manifest.json"
QUALITIES = ("clean", "scan", "photo")
JPEG_QUALITY = 84

COMPANY = ("Kestrel Ridge Foods", "1200 Larkin Avenue, Dock 4", "Fernvale")

# Fictional streets and towns for the vendors' letterheads.
_STREETS = ("Orchard Way", "Millrace Road", "Tanner Street", "Quarry Lane", "Bellwether Drive", "Kiln Road",
            "Copper Street", "Ferry Landing", "Granary Row", "Saltworks Road", "Linden Court", "Harrow Lane",
            "Vintner Street", "Foundry Road")
_TOWNS = ("Ashcombe", "Brierley", "Calder Vale", "Dunmore", "Eastwick Hollow", "Fallow Bay", "Greywater",
          "Hollins Cross", "Ivybridge Falls", "Juniper Flats", "Kestwick", "Lowmere", "Marrow Point", "Northam Glen")
_COLORS = ("#2f6b3a", "#1f4e79", "#6b2f5f", "#8a3b12", "#7a5c12", "#2b5d6b", "#5a3825", "#7d1f2b",
           "#3f4a8a", "#2e6b5e", "#6b6b2f", "#1f6b8a", "#4b2f6b", "#5d6b2b")
_LAYOUTS = {"V01": 1, "V02": 0, "V03": 1, "V04": 2, "V05": 0, "V06": 3, "V07": 3,
            "V08": 2, "V09": 1, "V10": 2, "V11": 0, "V12": 3, "V13": 2, "V14": 1}


class DocumentError(RuntimeError):
    pass


def _hash(text: str) -> int:
    return int(hashlib.sha256(text.encode("utf-8")).hexdigest()[:12], 16)


def quality_for(case_id: str) -> str:
    """Half the invoices arrive clean, three in ten scanned, two in ten photographed."""
    bucket = _hash("quality:" + case_id) % 100
    return "clean" if bucket < 50 else "scan" if bucket < 80 else "photo"


def layout_for(vendor_id: str) -> int:
    return _LAYOUTS.get(vendor_id, _hash(vendor_id) % 4)


def _vendor_number(vendor_id: str) -> int:
    digits = "".join(c for c in vendor_id if c.isdigit())
    return int(digits) if digits else _hash(vendor_id)


def letterhead(vendor_id: str) -> dict:
    n = _vendor_number(vendor_id)
    return {
        "street": f"{100 + (n * 37) % 880} {_STREETS[n % len(_STREETS)]}",
        "town": _TOWNS[(n * 5) % len(_TOWNS)],
        "color": _COLORS[n % len(_COLORS)],
    }


def _e(value: object) -> str:
    return html.escape(str(value), quote=True)


def _initials(name: str) -> str:
    words = [w for w in name.replace("&", " ").split() if w[:1].isalpha()]
    return "".join(w[0] for w in words[:2]).upper() or "?"


_BASE_CSS = """
* { box-sizing: border-box; }
html, body { margin: 0; padding: 0; background: #fff; }
#stage { display: inline-block; position: relative; }
.sheet { position: relative; width: 820px; min-height: 1060px; background: #fff; color: #1b1b1b; padding: 56px 60px 48px;
  font-size: 14px; line-height: 1.45; overflow: hidden; }
.sheet table { border-collapse: collapse; }
.lh { display: flex; justify-content: space-between; align-items: flex-start; gap: 24px; margin-bottom: 34px; }
.brand { display: flex; gap: 16px; align-items: center; }
.mark { width: 58px; height: 58px; border-radius: 50%; background: var(--brand); color: #fff; display: grid;
  place-items: center; font-weight: 700; font-size: 22px; letter-spacing: .02em; flex: none; }
.vname { font-size: 23px; font-weight: 700; color: var(--brand); }
.addr { font-size: 12.5px; color: #444; }
.doc { text-align: right; }
.title { font-size: 30px; font-weight: 700; letter-spacing: .04em; margin-bottom: 8px; }
.meta td { padding: 2px 0 2px 16px; text-align: right; }
.meta td:first-child { color: #555; }
.parties { display: flex; gap: 60px; margin-bottom: 30px; }
.lbl { font-size: 11.5px; text-transform: uppercase; letter-spacing: .08em; color: #666; margin-bottom: 3px; }
.lines { width: 100%; margin-bottom: 22px; }
.lines th { text-align: left; font-size: 12px; padding: 8px 8px; border-bottom: 2px solid #222; }
.lines td { padding: 9px 8px; border-bottom: 1px solid #d8d8d8; vertical-align: top; }
.n { text-align: right; white-space: nowrap; }
.totals { display: flex; justify-content: flex-end; margin-bottom: 30px; }
.totals td { padding: 4px 8px 4px 40px; text-align: right; }
.totals tr.due td { font-weight: 700; font-size: 17px; border-top: 2px solid #222; padding-top: 8px; }
.remit { border: 1.5px solid #333; padding: 14px 18px; width: 440px; margin-bottom: 30px; }
.remit div { margin: 2px 0; }
.foot { font-size: 12px; color: #555; border-top: 1px solid #ccc; padding-top: 10px; }
/* Layout 0: classic letterhead */
.l0 { font-family: "Bitstream Charter", "Caladea", Georgia, "DejaVu Serif", serif; }
.l0 .lh { flex-direction: column; align-items: center; text-align: center; border-bottom: 3px double #333; padding-bottom: 16px; }
.l0 .brand { flex-direction: column; gap: 8px; }
.l0 .doc { text-align: center; }
.l0 .meta { margin: 0 auto; }
/* Layout 1: colour band */
.l1 { font-family: "Carlito", Calibri, "Helvetica Neue", Arial, "DejaVu Sans", sans-serif; padding-top: 0; }
.l1 .lh { margin: 0 -60px 34px; padding: 30px 60px; background: var(--brand); color: #fff; }
.l1 .vname, .l1 .addr, .l1 .meta td, .l1 .meta td:first-child { color: #fff; }
.l1 .mark { background: #fff; color: var(--brand); }
.l1 .lines th { background: color-mix(in srgb, var(--brand) 14%, white); border-bottom: 0; }
/* Layout 2: old billing system printout */
.l2 { font-family: "Courier 10 Pitch", "Courier New", Courier, "DejaVu Sans Mono", monospace; font-size: 13.5px; }
.l2 .vname { color: #1b1b1b; text-transform: uppercase; font-size: 20px; }
.l2 .mark { border-radius: 0; background: none; color: #1b1b1b; border: 2px solid #1b1b1b; }
.l2 .title { text-transform: uppercase; }
.l2 .lines th { border-bottom: 1px dashed #222; text-transform: uppercase; }
.l2 .lines td { border-bottom: 0; }
.l2 .lines tbody tr:nth-child(odd) td { background: #eef3ea; }
.l2 .remit { border-style: dashed; }
/* Layout 3: minimal */
.l3 { font-family: "FreeSans", "Helvetica Neue", Arial, "DejaVu Sans", sans-serif; padding: 70px 72px; }
.l3 .mark { display: none; }
.l3 .vname { color: #1b1b1b; font-weight: 400; font-size: 26px; }
.l3 .title { color: var(--brand); font-weight: 400; font-size: 40px; }
.l3 .lines th { border-bottom: 1px solid #222; font-weight: 400; color: #555; }
.l3 .totals tr.due td { font-size: 22px; border-top: 1px solid #222; }
.l3 .remit { border: 0; border-left: 4px solid var(--brand); }
"""

_QUALITY_CSS = {
    "clean": "",
    "scan": """
#stage { background: #e8e8e3; padding: 26px; }
.sheet { transform: rotate(var(--tilt)); filter: grayscale(1) contrast(1.22) brightness(1.03) blur(0.35px);
  box-shadow: 0 1px 3px rgba(0,0,0,.25); }
.grain { position: absolute; inset: 0; filter: url(#grain); opacity: .22; mix-blend-mode: multiply; pointer-events: none; }
""",
    "photo": """
#stage { background: radial-gradient(ellipse at 30% 20%, #8c7862 0%, #5a4838 55%, #33281f 100%); padding: 70px 80px; }
.sheet { transform: perspective(1500px) rotateX(5deg) rotateY(-4deg) rotate(var(--tilt));
  filter: sepia(.2) saturate(1.06) blur(0.5px); box-shadow: 0 40px 70px rgba(0,0,0,.55); }
.grain { position: absolute; inset: 0; filter: url(#grain); opacity: .12; mix-blend-mode: multiply; pointer-events: none; }
.light { position: absolute; inset: 0; pointer-events: none; mix-blend-mode: soft-light;
  background: linear-gradient(115deg, rgba(255,255,255,.55) 0%, rgba(255,255,255,0) 45%, rgba(0,0,0,.35) 100%); }
""",
}

_GRAIN_SVG = ('<svg width="0" height="0" style="position:absolute"><filter id="grain">'
              '<feTurbulence type="fractalNoise" baseFrequency="0.85" numOctaves="2" stitchTiles="stitch"/>'
              '<feColorMatrix type="saturate" values="0"/></filter></svg>')


def invoice_html(case: dict, quality: str | None = None) -> str:
    """The invoice as an HTML page. Only invoice fields appear; nothing from the answer key."""
    quality = quality or quality_for(case["id"])
    if quality not in QUALITIES:
        raise ValueError(f"unknown quality {quality!r}")
    if case.get("department") == "expenses":
        from probie import expenses
        return expenses.claim_html(case, quality, _BASE_CSS, _QUALITY_CSS, _GRAIN_SVG, _hash)
    invoice = case["invoice"]
    vendor_id = invoice["vendor_id"]
    head = letterhead(vendor_id)
    email = (case.get("vendor") or {}).get("email") or ""
    tilt = ((_hash("tilt:" + case["id"]) % 1000) / 1000 - 0.5) * (2.4 if quality == "scan" else 5.0 if quality == "photo" else 0)
    subtotal = sum(line["amount_cents"] for line in invoice["lines"])
    remit = invoice["remit_to"]
    rows = "".join(
        f"<tr><td>{_e(line['sku'])}</td><td>{_e(line['description'])}</td><td class='n'>{line['qty']}</td>"
        f"<td class='n'>{fmt(line['unit_cents'])}</td><td class='n'>{fmt(line['amount_cents'])}</td></tr>"
        for line in invoice["lines"]
    )
    overlays = ""
    if quality != "clean":
        overlays = "<div class='grain'></div>" + ("<div class='light'></div>" if quality == "photo" else "")
    return f"""<!doctype html><html><head><meta charset="utf-8"><style>{_BASE_CSS}{_QUALITY_CSS[quality]}</style></head>
<body>{_GRAIN_SVG}<div id="stage"><div class="sheet l{layout_for(vendor_id)}" style="--brand:{head['color']};--tilt:{tilt:.3f}deg">
<header class="lh">
  <div class="brand"><div class="mark">{_e(_initials(invoice['vendor_name']))}</div>
    <div><div class="vname">{_e(invoice['vendor_name'])}</div>
    <div class="addr">{_e(head['street'])}, {_e(head['town'])}<br>{_e(email)}</div></div></div>
  <div class="doc"><div class="title">INVOICE</div>
    <table class="meta">
      <tr><td>Invoice no.</td><td>{_e(invoice['number'])}</td></tr>
      <tr><td>Date</td><td>{_e(invoice['date'])}</td></tr>
      <tr><td>PO number</td><td>{_e(invoice['po_number'] or 'none')}</td></tr>
      <tr><td>Terms</td><td>Net 30</td></tr>
    </table></div>
</header>
<section class="parties">
  <div><div class="lbl">Bill to</div>{_e(COMPANY[0])}<br>{_e(COMPANY[1])}<br>{_e(COMPANY[2])}</div>
  <div><div class="lbl">Customer</div>{_e(COMPANY[0])}<br>Accounts payable</div>
</section>
<table class="lines"><thead><tr><th>Item</th><th>Description</th><th class="n">Qty</th><th class="n">Unit price</th><th class="n">Amount</th></tr></thead>
<tbody>{rows}</tbody></table>
<section class="totals"><table>
  <tr><td>Subtotal</td><td>{fmt(subtotal)}</td></tr>
  <tr><td>Freight</td><td>{fmt(invoice['freight_cents'])}</td></tr>
  <tr class="due"><td>Total due</td><td>{fmt(invoice['total_cents'])}</td></tr>
</table></section>
<section class="remit"><div class="lbl">Remit payment to</div>
  <div>Bank: {_e(remit['bank_name'])}</div><div>Routing number: {_e(remit['routing'])}</div><div>Account number: {_e(remit['account'])}</div>
</section>
<div class="foot">Thank you for your business. Questions about this invoice: {_e(email)}</div>
{overlays}</div></div></body></html>"""


def _html_sha(page_html: str) -> str:
    return hashlib.sha256(page_html.encode("utf-8")).hexdigest()


def _render_chunk(job: tuple[list[dict], str, dict]) -> dict:
    """Render a batch of invoices in one browser. Runs in a worker process."""
    from playwright.sync_api import sync_playwright
    cases, out_dir, previous = job
    out_dir = Path(out_dir)
    try:
        from PIL import Image
    except ImportError:
        Image = None
    entries: dict[str, dict] = {}
    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            page = browser.new_page(viewport={"width": 1000, "height": 1400}, device_scale_factor=ZOOM_SCALE)
            for case in cases:
                quality = quality_for(case["id"])
                page_html = invoice_html(case, quality)
                html_sha = _html_sha(page_html)
                old = previous.get(case["id"])
                if (old and old.get("html_sha") == html_sha and (out_dir / old["file"]).exists()
                        and all((out_dir / r["file"]).exists() for r in old.get("regions", {}).values())):
                    entries[case["id"]] = old
                    continue
                page.set_content(page_html, wait_until="load")
                stage = page.locator("#stage")
                stage_box = stage.bounding_box()
                if case.get("department") == "expenses":
                    from probie import expenses
                    selectors = expenses.DOCUMENT_REGIONS
                else:
                    selectors = REGIONS
                boxes = {region: page.locator(sel).first.bounding_box() for region, sel in selectors.items()}
                files: dict[str, bytes] = {}
                if Image is not None:
                    import io
                    big = Image.open(io.BytesIO(stage.screenshot(type="jpeg", quality=95, scale="device"))).convert("RGB")
                    main = big.resize((round(big.width / ZOOM_SCALE), round(big.height / ZOOM_SCALE)), Image.LANCZOS)
                    buffer = io.BytesIO()
                    main.save(buffer, "JPEG", quality=JPEG_QUALITY, optimize=True)
                    files[f"{case['id']}.jpg"] = buffer.getvalue()
                    for region, box in boxes.items():
                        if not box:
                            continue
                        pad = 10
                        left = max(0, round((box["x"] - stage_box["x"] - pad) * ZOOM_SCALE))
                        top = max(0, round((box["y"] - stage_box["y"] - pad) * ZOOM_SCALE))
                        right = min(big.width, round((box["x"] - stage_box["x"] + box["width"] + pad) * ZOOM_SCALE))
                        bottom = min(big.height, round((box["y"] - stage_box["y"] + box["height"] + pad) * ZOOM_SCALE))
                        buffer = io.BytesIO()
                        big.crop((left, top, right, bottom)).save(buffer, "JPEG", quality=JPEG_QUALITY, optimize=True)
                        files[f"{case['id']}.{region}.jpg"] = buffer.getvalue()
                else:
                    files[f"{case['id']}.jpg"] = stage.screenshot(type="jpeg", quality=JPEG_QUALITY, scale="css")
                    for region, box in boxes.items():
                        if not box:
                            continue
                        pad = 10
                        clip = {"x": max(box["x"] - pad, 0), "y": max(box["y"] - pad, 0),
                                "width": box["width"] + 2 * pad, "height": box["height"] + 2 * pad}
                        files[f"{case['id']}.{region}.jpg"] = page.screenshot(type="jpeg", quality=JPEG_QUALITY,
                                                                              clip=clip, full_page=True, scale="device")
                for name, data in files.items():
                    (out_dir / name).write_bytes(data)
                main_name = f"{case['id']}.jpg"
                entries[case["id"]] = {
                    "file": main_name, "quality": quality, "layout": layout_for(case["invoice"]["vendor_id"]),
                    "sha256": hashlib.sha256(files[main_name]).hexdigest(), "html_sha": html_sha,
                    "regions": {region: {"file": f"{case['id']}.{region}.jpg",
                                         "sha256": hashlib.sha256(files[f"{case['id']}.{region}.jpg"]).hexdigest()}
                                for region in selectors if f"{case['id']}.{region}.jpg" in files},
                }
        finally:
            browser.close()
    return entries


def render(cases: list[dict], out_dir: str | Path, *, progress=None, workers: int | None = None) -> dict:
    """Render every case's invoice to out_dir as JPEG, with zoom crops, and write the manifest.

    Invoices whose page hasn't changed since the last render are kept as they are.
    """
    try:
        import playwright.sync_api  # noqa: F401
    except ImportError:
        raise DocumentError("Rendering invoices needs Playwright: pip install playwright, then playwright install chromium") from None
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    previous = (load_manifest(out_dir) or {}).get("documents", {})
    workers = max(1, min(workers or (os.cpu_count() or 1), 8, max(1, len(cases) // 25)))
    chunks = [cases[i::workers] for i in range(workers)]
    entries: dict[str, dict] = {}
    try:
        if workers == 1:
            entries = _render_chunk((cases, str(out_dir), previous))
        else:
            from concurrent.futures import ProcessPoolExecutor
            with ProcessPoolExecutor(max_workers=workers) as pool:
                for part in pool.map(_render_chunk, [(chunk, str(out_dir), previous) for chunk in chunks]):
                    entries.update(part)
    except DocumentError:
        raise
    except Exception as exc:
        raise DocumentError(f"Couldn't render the invoices ({exc}). If Chromium is missing, run: playwright install chromium") from None
    if progress:
        progress(f"Rendered {len(entries)} invoices with {workers} worker{'s' if workers > 1 else ''}.")
    manifest = {"kind": "probie documents", "version": DOCUMENT_VERSION,
                "documents": {case["id"]: entries[case["id"]] for case in cases}}
    tmp = out_dir / (MANIFEST + ".tmp")
    tmp.write_text(json.dumps(manifest, indent=1), encoding="utf-8")
    os.replace(tmp, out_dir / MANIFEST)
    return manifest


def load_manifest(documents_dir: str | Path) -> dict | None:
    path = Path(documents_dir) / MANIFEST
    if not path.exists():
        return None
    try:
        manifest = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        raise DocumentError(f"{path} is damaged. Render the invoices again.") from None
    if manifest.get("kind") != "probie documents":
        raise DocumentError(f"{path} isn't a Probie documents manifest")
    return manifest


def manifest_fingerprint(manifest: dict, case_ids: list[str]) -> str:
    """Changes whenever any invoice image a model would see changes."""
    docs = manifest["documents"]
    material = json.dumps([[cid, docs[cid]["sha256"], sorted((r, v["sha256"]) for r, v in docs[cid].get("regions", {}).items())]
                           for cid in case_ids], separators=(",", ":"))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def missing_documents(documents_dir: str | Path, case_ids: list[str]) -> list[str]:
    manifest = load_manifest(documents_dir)
    if manifest is None:
        return list(case_ids)
    docs = manifest["documents"]
    return [cid for cid in case_ids if cid not in docs or not (Path(documents_dir) / docs[cid]["file"]).exists()]


def region_data_url(documents_dir: str | Path, case_id: str, region: str) -> str | None:
    """A sharper crop of one region of the invoice, or None if it wasn't rendered."""
    manifest = load_manifest(documents_dir)
    entry = (manifest or {}).get("documents", {}).get(case_id, {}).get("regions", {}).get(region)
    if not entry:
        return None
    data = (Path(documents_dir) / entry["file"]).read_bytes()
    return "data:image/jpeg;base64," + base64.b64encode(data).decode("ascii")


def image_data_url(documents_dir: str | Path, case_id: str) -> str:
    manifest = load_manifest(documents_dir)
    if manifest is None or case_id not in manifest["documents"]:
        raise DocumentError(f"No rendered invoice for {case_id}. Run: python -m probie render")
    data = (Path(documents_dir) / manifest["documents"][case_id]["file"]).read_bytes()
    return "data:image/jpeg;base64," + base64.b64encode(data).decode("ascii")
