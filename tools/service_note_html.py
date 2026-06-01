from __future__ import annotations

import html
import mimetypes
import os
from datetime import date, datetime
from pathlib import Path
from typing import Any


def _clean(value: Any, fallback: str = "-") -> str:
    if value is None:
        return fallback
    if isinstance(value, list):
        parts = [_clean(item, "") for item in value]
        parts = [part for part in parts if part]
        return ", ".join(parts) if parts else fallback
    text = str(value).strip()
    return text or fallback


def _esc(value: Any, fallback: str = "-") -> str:
    return html.escape(_clean(value, fallback))


def _money(value: Any) -> str:
    try:
        return f"Rs. {float(value or 0):,.2f}"
    except (TypeError, ValueError):
        return "Rs. 0.00"


def _safe_date(value: Any):
    if value is None:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    text = str(value).strip()
    if not text:
        return None
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%Y/%m/%d"):
        try:
            return datetime.strptime(text[:10], fmt).date()
        except Exception:
            continue
    return None


def _format_date(value: Any) -> str:
    parsed = _safe_date(value)
    if parsed:
        return parsed.strftime("%d-%m-%Y")
    return _clean(value)


def _is_incomplete_reg_exec(value: Any) -> bool:
    text = str(value or "").strip().lower()
    return text in {"incomplete_reg_exec", "incomplete reg exec"}


def _is_manual_hcb_slip(value: Any) -> bool:
    text = str(value or "").strip().lower()
    return text in {"manual hcb slip", "manual_hcb_slip", "manual hc slip", "manual_hc_slip", "manual_slip", "manual-slip", "hcb_slip", "hcb-slip"}


def _dispatch_note(report_delivery: Any) -> str:
    text = str(report_delivery or "").strip()
    if not text:
        return "-"
    lower = text.lower()
    if "courier" in lower:
        return "Report via courier will be deliver after 3-4 working day of report completion."
    return "-"


def _uploads_root() -> Path:
    env_base = str(os.getenv("PATIENT_DOCUMENTS_UPLOAD_BASE") or "").strip()
    if env_base:
        return Path(env_base).expanduser().resolve().parent
    env_path = Path(__file__).resolve().parents[1] / ".env"
    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8", errors="ignore").splitlines():
            key, _, value = line.partition("=")
            if key.strip().upper() == "PATIENT_DOCUMENTS_UPLOAD_BASE" and value.strip():
                return Path(value.strip().strip("'\"")).expanduser().resolve().parent
    return Path("/home/arpraandhc/Desktop/arpra neo/arpra-neo/app/static/uploads")


def _logo_uri() -> str:
    candidates = [
        Path(__file__).resolve().parents[1] / "Logo1.png",
        Path(__file__).resolve().parents[1] / "logo1.png",
    ]
    for candidate in candidates:
        try:
            if candidate.exists():
                return candidate.resolve().as_uri()
        except Exception:
            continue
    return ""


def _resolve_document_path(reference: str) -> Path | None:
    if not reference:
        return None
    raw = str(reference).strip()
    if not raw:
        return None
    candidate = Path(raw)
    if candidate.is_absolute() and candidate.exists():
        return candidate

    project_root = Path(__file__).resolve().parents[1]
    uploads_root = _uploads_root()
    rel = raw.lstrip("/")
    candidates = []
    if raw.startswith("app/static/uploads/"):
        candidates.append(project_root / raw)
    if raw.startswith("/static/uploads/"):
        candidates.append(uploads_root / raw.removeprefix("/static/uploads/"))
    if raw.startswith("static/uploads/"):
        candidates.append(uploads_root / raw.removeprefix("static/uploads/"))
    if raw.startswith("prescriptions/") or raw.startswith("patient_documents/") or raw.startswith("booking_patient_documents/") or raw.startswith("payment_shot/") or raw.startswith("hc_slip/"):
        candidates.append(uploads_root / raw)
    if "/" in raw:
        candidates.append(uploads_root / "prescriptions" / raw)
        candidates.append(uploads_root / "patient_documents" / raw)
        candidates.append(uploads_root / "payment_shot" / raw)
        candidates.append(uploads_root / "hc_slip" / raw)
        candidates.append(uploads_root / raw)
    else:
        candidates.append(uploads_root / "prescriptions" / raw)
        candidates.append(uploads_root / "patient_documents" / raw)
        candidates.append(uploads_root / "payment_shot" / raw)
        candidates.append(uploads_root / "hc_slip" / raw)

    for item in candidates:
        try:
            if item.exists():
                return item.resolve()
        except Exception:
            continue
    return None


def _is_image_file(path: Path | None, reference: str) -> bool:
    ext = (path.suffix if path else Path(str(reference)).suffix).lower()
    return ext in {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp"}


def _patient_name_map(payload: dict) -> dict[int, dict]:
    out: dict[int, dict] = {}
    for item in payload.get("sample_collection_pick_patients") or []:
        try:
            pid = int(item.get("patient_id") or item.get("id") or item.get("sourcePatientId") or 0)
        except Exception:
            pid = 0
        if pid <= 0:
            continue
        out[pid] = item
    return out


def _tests_map(payload: dict) -> dict[int, dict]:
    out: dict[int, dict] = {}
    for item in payload.get("tests_payload") or []:
        try:
            pid = int(item.get("patient_id") or 0)
        except Exception:
            pid = 0
        if pid <= 0:
            continue
        out[pid] = item
    return out


def _updates(payload: dict) -> list[dict]:
    return list(payload.get("patient_updates") or [])


def _patient_heading(patient_meta: dict, update: dict) -> str:
    return _clean(update.get("patient_name"), _clean(patient_meta.get("name")))


def _gender_age(patient_meta: dict) -> str:
    gender = _clean(patient_meta.get("gender"), "")
    age = patient_meta.get("age_years")
    age_text = f"{age} Years" if age not in (None, "") else ""
    if gender and age_text:
        return f"{gender} / {age_text}"
    return gender or age_text or "-"


def _address_text(address: dict) -> str:
    parts = [
        address.get("address_type"),
        address.get("house_flat_no"),
        address.get("floor"),
        address.get("block_tower_no"),
        address.get("street_line"),
        address.get("landmark"),
        address.get("colony_name_snapshot") or address.get("colony_name"),
        address.get("city"),
    ]
    text = ", ".join(str(x).strip() for x in parts if str(x or "").strip())
    pincode = _clean(address.get("pincode_snapshot") or address.get("pincode"), "")
    if pincode:
        text = f"{text} - {pincode}" if text else pincode
    access_notes = _clean(address.get("access_notes"), "")
    if access_notes:
        text = f"{text}. Access Notes: {access_notes}" if text else f"Access Notes: {access_notes}"
    return text or "-"


def _panel_companies(tests_payload: dict, patient_meta: dict) -> str:
    names = []
    for panel in tests_payload.get("panels") or []:
        name = _clean(panel.get("panel_company"), "")
        if name and name not in names:
            names.append(name)
    if not names:
        fallback = _clean(patient_meta.get("panel_company"), "")
        if fallback:
            names.append(fallback)
    return ", ".join(names) if names else "-"


def _two_col_test_rows(test_names: list[str]) -> str:
    if not test_names:
        return '<tr><td colspan="2">No tests available</td></tr>'
    rows = []
    for idx in range(0, len(test_names), 2):
        left = _esc(test_names[idx], "")
        right = _esc(test_names[idx + 1], "") if idx + 1 < len(test_names) else ""
        rows.append(f"<tr><td>{left}</td><td>{right}</td></tr>")
    return "".join(rows)


def _attachment_documents(update: dict) -> tuple[str, list[dict[str, str]]]:
    apk_tbs = update.get("apk_tbs") or update.get("test_booking_status")
    docs = list(update.get("documents") or [])
    if _is_manual_hcb_slip(apk_tbs):
        refs = []
        booking_code = str(update.get("booking_code") or "").strip()
        try:
            patient_id = int(update.get("patient_id") or 0)
        except Exception:
            patient_id = 0
        if booking_code and patient_id > 0:
            hc_dir = _uploads_root() / "hc_slip" / booking_code / f"PT{patient_id}"
            if hc_dir.exists():
                for item in sorted(hc_dir.iterdir()):
                    if item.is_file():
                        refs.append({"type": "manual_hcb_slip", "file": f"hc_slip/{booking_code}/PT{patient_id}/{item.name}"})
        return "Manual HC Slip Attachment", refs
    if _is_incomplete_reg_exec(apk_tbs):
        refs = []
        for value in (update.get("prescription_files") or []):
            ref = str(value or "").strip()
            if ref:
                refs.append({"type": "prescription", "file": ref})
        if refs:
            return "Prescription Attachment", refs
        return "Prescription Attachment", [doc for doc in docs if str(doc.get("type") or "").strip().lower() == "prescription"]
    return "", []


def _document_sections(patient_name: str, update: dict) -> str:
    heading, docs = _attachment_documents(update)
    blocks = [f'<section class="section attachment-section"><h2 class="section-title">{_esc(heading or "Attached Documents")} - {_esc(patient_name)}</h2>']
    if not docs:
        blocks.append('<div class="attachment-empty">No attached documents.</div></section>')
        return "".join(blocks)

    seen: set[str] = set()
    for doc in docs:
        ref = str(doc.get("file") or "").strip()
        if not ref or ref in seen:
            continue
        seen.add(ref)
        resolved = _resolve_document_path(ref)
        if _is_image_file(resolved, ref) and resolved is not None:
            blocks.append(
                f'<div class="attachment-block"><img class="attachment-image" src="{resolved.as_uri()}" alt="{_esc(ref)}"></div>'
            )
        else:
            label = resolved.as_uri() if resolved is not None else ref
            blocks.append(
                f'<div class="attachment-block"><div class="attachment-link">{_esc(label)}</div></div>'
            )
    blocks.append("</section>")
    return "".join(blocks)


def build_service_note_html(payload: dict, output_path: str | Path) -> Path:
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)

    patient_meta_map = _patient_name_map(payload)
    tests_map = _tests_map(payload)
    address = payload.get("address") or {}
    booking_label = _clean(payload.get("booking_id") or payload.get("booking_code") or payload.get("id"))
    booking_date = _format_date(payload.get("booking_date"))
    time_slot = _clean(payload.get("time_slot"))
    phlebo_name = _clean(payload.get("phlebo_name"))
    address_text = _address_text(address)
    referred_by = _clean(payload.get("referred_by"))
    logo_uri = _logo_uri()

    logo_img_html = f"<img class=\"brand-logo\" src=\"{logo_uri}\" alt=\"Dr Bhasin's Lab logo\">" if logo_uri else ""

    pages: list[str] = []
    updates = _updates(payload)
    for index, update in enumerate(updates, start=1):
        try:
            patient_id = int(update.get("patient_id") or 0)
        except Exception:
            patient_id = 0
        patient_meta = patient_meta_map.get(patient_id, {})
        tests_payload = tests_map.get(patient_id, {})
        patient_name = _patient_heading(patient_meta, update)
        is_manual = _is_manual_hcb_slip(update.get("apk_tbs") or tests_payload.get("test_booking_status"))
        is_incomplete = _is_incomplete_reg_exec(update.get("apk_tbs") or tests_payload.get("test_booking_status"))
        test_names = []
        if not is_manual and not is_incomplete:
            for panel in tests_payload.get("panels") or []:
                for test in panel.get("selected_tests") or []:
                    name = _clean(test.get("description"), "")
                    if name:
                        test_names.append(name)
        payment_mode = _clean(update.get("payment_mode"))
        patient_total = _money(update.get("payment_amount"))
        due_amount = _money(update.get("due_amount"))
        extra_amount = _money(update.get("extra_amount"))
        panel_names = _panel_companies(tests_payload, patient_meta)

        pages.append(
            f"""
  <main class="page">
    <header>
      <div>
        <div class="brand-head">
          {logo_img_html}
        </div>
        <h1>Home Collection Service Note</h1>
        <div class="subtitle">Patient copy for home sample collection and payment acknowledgement</div>
      </div>
      <div class="note-id">
        <div><strong>Booking ID</strong><span>{_esc(booking_label)}</span></div>
        <div><strong>Date</strong><span>{_esc(booking_date)}</span></div>
        <div><strong>Visit Slot</strong><span>{_esc(time_slot)}</span></div>
      </div>
    </header>

    <section class="section">
      <h2 class="section-title">Visit Details</h2>
      <div class="grid two">
        <div class="field"><span class="label">Collection Address</span><span class="value">{_esc(address_text)}</span></div>
        <div class="field"><span class="label">Phlebotomist</span><span class="value">{_esc(phlebo_name)}</span></div>
      </div>
    </section>

    <section class="section">
      <h2 class="section-title">Patient Details</h2>
      <div class="grid">
        <div class="field"><span class="label">Patient Name</span><span class="value">{_esc(patient_name)}</span></div>
        <div class="field"><span class="label">Gender / Age</span><span class="value">{_esc(_gender_age(patient_meta))}</span></div>
        <div class="field"><span class="label">Date of Birth</span><span class="value">{_esc(_format_date(patient_meta.get("date_of_birth")))}</span></div>
        <div class="field"><span class="label">Mobile</span><span class="value">{_esc(patient_meta.get("contact_mobile"))}</span></div>
        <div class="field"><span class="label">Report Schedule</span><span class="value">{_esc(update.get("report_schedule"))}</span></div>
        <div class="field"><span class="label">No. of Pricks</span><span class="value pricks">{_esc(update.get("no_of_pricks"))}</span></div>
      </div>
      <div class="notice prick-note" style="display:none; margin:10px;">
        Sorry: If more than one prick was required during sample collection, we sincerely apologize for the inconvenience caused.
      </div>
    </section>

    <section class="section">
      <h2 class="section-title">Report Delivery</h2>
      <div class="grid two">
        <div class="field"><span class="label">Delivery Mode</span><span class="value">{_esc(update.get("report_delivery"))}</span></div>
        <div class="field"><span class="label">Dispatch Note</span><span class="value">{_esc(_dispatch_note(update.get("report_delivery")))}</span></div>
      </div>
    </section>

    <section class="section">
      <div class="panel-info">
        <div><span class="label">Panel Companies</span><span class="value">{_esc(panel_names)}</span></div>
        <div><span class="label">Referred By</span><span class="value">{_esc(referred_by)}</span></div>
      </div>
      <table class="tests-table">
        <tbody>
          {('<tr><td colspan="2">As per prescription attached</td></tr>' if (is_manual or is_incomplete) else _two_col_test_rows(test_names))}
        </tbody>
      </table>
    </section>

    <section class="section">
      <h2 class="section-title">Payment Summary</h2>
      <div class="grid">
        <div class="field"><span class="label">Payment Method</span><span class="value">{_esc(payment_mode)}</span></div>
        <div class="field"><span class="label">Total Amount</span><span class="value">{_esc(patient_total)}</span></div>
      </div>
    </section>
  </main>
"""
        )
        attachment_sections = _document_sections(patient_name, update) if _attachment_documents(update)[1] else ""
        if attachment_sections:
            pages.append(
                f"""
  <main class="page">
    <section class="section compact-attachment-header">
      <h2 class="section-title">Attached Documents</h2>
      <div class="attachment-summary">Booking ID: {_esc(booking_label)} | Date: {_esc(booking_date)} | Visit Slot: {_esc(time_slot)}</div>
    </section>
    {attachment_sections}
  </main>
"""
            )

    html_output = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Home Collection Service Note - {_esc(booking_label)}</title>
  <style>
    :root {{ --ink:#1f2933; --muted:#5f6f7f; --line:#cfd8e3; --soft:#f5f8fb; --brand:#0f6b6e; --accent:#e7f4f2; }}
    * {{ box-sizing:border-box; }}
    body {{ margin:0; background:#eef2f5; color:var(--ink); font-family:Arial,Helvetica,sans-serif; font-size:14px; line-height:1.42; }}
    .page {{ width:210mm; min-height:297mm; margin:12px auto; padding:14mm; background:#fff; box-shadow:0 10px 30px rgba(15,23,42,.12); page-break-after:always; }}
    .page:last-of-type {{ page-break-after:auto; }}
    header {{ display:grid; grid-template-columns:1fr auto; gap:18px; align-items:start; padding-bottom:12px; border-bottom:2px solid var(--brand); }}
    .brand-head {{ display:flex; align-items:center; min-height:64px; }}
    .brand-logo {{ width:180px; max-height:72px; height:auto; object-fit:contain; }}
    h1 {{ margin:0 0 4px; color:var(--brand); font-size:28px; font-weight:700; }}
    .subtitle {{ color:var(--muted); font-size:14px; }}
    .note-id {{ min-width:195px; padding:11px 13px; background:var(--accent); border:1px solid #b8ded8; border-radius:6px; }}
    .note-id div {{ display:flex; justify-content:space-between; gap:12px; margin:2px 0; }}
    .section {{ margin-top:14px; border:1px solid var(--line); border-radius:6px; overflow:hidden; }}
    .section-title {{ margin:0; padding:7px 10px; background:var(--soft); border-bottom:1px solid var(--line); color:var(--brand); font-size:15px; font-weight:700; }}
    .grid {{ display:grid; grid-template-columns:repeat(3,1fr); }}
    .grid.two {{ grid-template-columns:repeat(2,1fr); }}
    .field {{ min-height:54px; padding:9px 11px; border-right:1px solid var(--line); border-bottom:1px solid var(--line); }}
    .field:nth-child(3n) {{ border-right:0; }}
    .grid.two .field:nth-child(2n) {{ border-right:0; }}
    .label {{ display:block; margin-bottom:3px; color:var(--muted); font-size:11.5px; font-weight:700; text-transform:uppercase; }}
    .value {{ font-size:15px; font-weight:600; }}
    table {{ width:100%; border-collapse:collapse; }}
    th,td {{ padding:9px 11px; border-bottom:1px solid var(--line); text-align:left; vertical-align:top; }}
    th {{ background:var(--soft); color:var(--muted); font-size:11.5px; text-transform:uppercase; }}
    .panel-info {{ display:grid; grid-template-columns:1fr 1fr; border-bottom:1px solid var(--line); background:#fbfdff; }}
    .panel-info div {{ padding:9px 11px; border-right:1px solid var(--line); }}
    .panel-info div:last-child {{ border-right:0; }}
    .tests-table td {{ width:50%; }}
    .tests-table td:first-child {{ border-right:1px solid var(--line); }}
    tr:last-child td {{ border-bottom:0; }}
    .notice {{ margin-top:14px; padding:10px 12px; border:1px solid #f0cf87; border-radius:6px; background:#fff8e6; color:#59430f; }}
    .attachment-summary {{ padding:12px; color:var(--muted); }}
    .compact-attachment-header {{ margin-top:0; }}
    .attachment-empty {{ padding:12px; color:var(--muted); }}
    .attachment-section {{ break-inside:auto; page-break-inside:auto; }}
    .attachment-block {{ padding:12px; border-top:1px solid var(--line); break-inside:auto; page-break-inside:auto; }}
    .attachment-label {{ font-weight:700; margin-bottom:8px; color:var(--brand); }}
    .attachment-image {{ max-width:100%; max-height:120mm; display:block; border:1px solid var(--line); border-radius:6px; background:#fff; break-inside:auto; page-break-inside:auto; object-fit:contain; }}
    .attachment-link {{ font-size:13px; color:#1f2933; word-break:break-all; }}
    @page {{ size:A4; margin:0; }}
    @media print {{
      html, body {{ width:210mm; min-height:297mm; background:#fff; }}
      body {{ -webkit-print-color-adjust:exact; print-color-adjust:exact; }}
      .page {{ width:210mm; min-height:297mm; margin:0; box-shadow:none; break-after:page; page-break-after:always; }}
      .page:last-of-type {{ break-after:auto; page-break-after:auto; }}
      header, .section, .notice {{ break-inside:avoid; page-break-inside:avoid; }}
      .attachment-section, .attachment-block, .attachment-image, .attachment-link {{ break-inside:auto; page-break-inside:auto; }}
      tr {{ break-inside:avoid; page-break-inside:avoid; }}
    }}
  </style>
</head>
<body>
{''.join(pages)}
<script>
  (function () {{
    var pages = document.querySelectorAll('.page');
    pages.forEach(function (page) {{
      var pricksEl = page.querySelector('.pricks');
      var noteEl = page.querySelector('.prick-note');
      if (!pricksEl || !noteEl) return;
      var pricks = parseInt((pricksEl.textContent || '').trim(), 10);
      noteEl.style.display = (!isNaN(pricks) && pricks > 1) ? 'block' : 'none';
    }});
  }})();
</script>
</body>
</html>
"""
    output.write_text(html_output, encoding="utf-8")
    return output
