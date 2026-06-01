"""Standalone patient-wise complete-booking PDF generator.

This module is intentionally separate from the FastAPI app. It uses only the
Python standard library, so it can be copied or run without installing PDF
packages.
"""

from __future__ import annotations

import argparse
import json
import os
import textwrap
from datetime import date
from pathlib import Path
from typing import Any

try:
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import inch
    from reportlab.platypus import Image, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

    REPORTLAB_AVAILABLE = True
except Exception:
    REPORTLAB_AVAILABLE = False


PAGE_WIDTH = 595
PAGE_HEIGHT = 842
MARGIN_X = 42
MARGIN_TOP = 48
MARGIN_BOTTOM = 48
LINE_HEIGHT = 14
BLUE = (0.06, 0.24, 0.48)
TEAL = (0.0, 0.45, 0.42)
GREEN = (0.08, 0.48, 0.28)
LIGHT_BLUE = (0.88, 0.94, 1.0)
LIGHT_TEAL = (0.88, 0.97, 0.96)
LIGHT_GREEN = (0.9, 0.97, 0.92)
LIGHT_GRAY = (0.95, 0.96, 0.97)
DARK = (0.08, 0.1, 0.14)
MUTED = (0.38, 0.42, 0.48)


def _clean(value: Any, fallback: str = "") -> str:
    if value is None:
        return fallback
    if isinstance(value, list):
        return ", ".join(_clean(item) for item in value if _clean(item))
    text = str(value).strip()
    return text or fallback


def _money(value: Any) -> str:
    try:
        return f"Rs. {float(value or 0):.2f}"
    except (TypeError, ValueError):
        return "Rs. 0.00"


def _money_plain(value: Any) -> str:
    try:
        return f"{float(value or 0):.2f}"
    except (TypeError, ValueError):
        return "0.00"


def _escape_pdf_text(text: Any) -> str:
    value = _clean(text)
    value = value.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    return value.encode("latin-1", "replace").decode("latin-1")


class SimplePdf:
    def __init__(self, title: str = "Complete Booking Summary") -> None:
        self.title = title
        self.pages: list[list[str]] = []
        self.current: list[str] = []
        self.y = PAGE_HEIGHT - MARGIN_TOP
        self.add_page()

    def add_page(self) -> None:
        if self.current:
            self.pages.append(self.current)
        self.current = []
        self.y = PAGE_HEIGHT - MARGIN_TOP

    def _ensure_space(self, height: int = LINE_HEIGHT) -> None:
        if self.y - height < MARGIN_BOTTOM:
            self.add_page()

    @staticmethod
    def _rgb(color: tuple[float, float, float]) -> str:
        return f"{color[0]:.3f} {color[1]:.3f} {color[2]:.3f}"

    def fill_rect(
        self,
        x: int,
        y: int,
        width: int,
        height: int,
        color: tuple[float, float, float],
    ) -> None:
        self.current.append(f"{self._rgb(color)} rg {x} {y} {width} {height} re f")

    def text(
        self,
        value: Any,
        x: int = MARGIN_X,
        size: int = 10,
        bold: bool = False,
        color: tuple[float, float, float] = DARK,
    ) -> None:
        self._ensure_space(LINE_HEIGHT)
        font = "F2" if bold else "F1"
        escaped = _escape_pdf_text(value)
        self.current.append(f"{self._rgb(color)} rg BT /{font} {size} Tf {x} {self.y} Td ({escaped}) Tj ET")
        self.y -= LINE_HEIGHT

    def wrapped_text(
        self,
        value: Any,
        x: int = MARGIN_X,
        size: int = 10,
        width: int = 92,
        bold: bool = False,
        color: tuple[float, float, float] = DARK,
    ) -> None:
        lines = textwrap.wrap(_clean(value), width=width) or [""]
        for line in lines:
            self.text(line, x=x, size=size, bold=bold, color=color)

    def key_value(self, label: str, value: Any, x: int = MARGIN_X) -> None:
        self.wrapped_text(f"{label}: {_clean(value, '-')}", x=x, width=88, color=DARK)

    def gap(self, amount: int = 8) -> None:
        self.y -= amount
        self._ensure_space()

    def rule(self, color: tuple[float, float, float] = LIGHT_GRAY) -> None:
        self._ensure_space(10)
        y = self.y + 4
        self.current.append(f"{self._rgb(color)} RG {MARGIN_X} {y} m {PAGE_WIDTH - MARGIN_X} {y} l S")
        self.y -= 8

    def header_band(self, title: str, subtitle: str) -> None:
        band_y = PAGE_HEIGHT - 92
        self.fill_rect(0, band_y, PAGE_WIDTH, 92, BLUE)
        self.y = PAGE_HEIGHT - 36
        self.text(title, x=MARGIN_X, size=17, bold=True, color=(1, 1, 1))
        self.text(subtitle, x=MARGIN_X, size=10, color=(0.86, 0.93, 1.0))
        self.y = band_y - 24

    def section_title(self, value: str, color: tuple[float, float, float] = TEAL) -> None:
        self._ensure_space(30)
        y = self.y - 6
        self.fill_rect(MARGIN_X, y, PAGE_WIDTH - (MARGIN_X * 2), 22, LIGHT_TEAL)
        self.text(value, x=MARGIN_X + 10, size=12, bold=True, color=color)
        self.y -= 6

    def summary_card(
        self,
        label: str,
        value: Any,
        x: int,
        y: int,
        width: int,
        fill: tuple[float, float, float],
        accent: tuple[float, float, float],
    ) -> None:
        self.fill_rect(x, y, width, 48, fill)
        self.fill_rect(x, y, 5, 48, accent)
        self.current.append(f"{self._rgb(accent)} rg BT /F2 15 Tf {x + 14} {y + 25} Td ({_escape_pdf_text(value)}) Tj ET")
        self.current.append(f"{self._rgb(MUTED)} rg BT /F1 8 Tf {x + 14} {y + 11} Td ({_escape_pdf_text(label)}) Tj ET")

    def save(self, output_path: str | Path) -> Path:
        if self.current:
            self.pages.append(self.current)
            self.current = []

        output = Path(output_path)
        output.parent.mkdir(parents=True, exist_ok=True)

        objects: list[bytes] = []
        catalog_id = 1
        pages_id = 2
        font_regular_id = 3
        font_bold_id = 4
        first_page_id = 5
        page_ids: list[int] = []
        content_ids: list[int] = []

        next_id = first_page_id
        for _ in self.pages:
            page_ids.append(next_id)
            content_ids.append(next_id + 1)
            next_id += 2

        kids = " ".join(f"{page_id} 0 R" for page_id in page_ids)
        objects.append(b"<< /Type /Catalog /Pages 2 0 R >>")
        objects.append(f"<< /Type /Pages /Kids [{kids}] /Count {len(page_ids)} >>".encode("latin-1"))
        objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
        objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold >>")

        for page_id, content_id, commands in zip(page_ids, content_ids, self.pages):
            page_object = (
                f"<< /Type /Page /Parent {pages_id} 0 R "
                f"/MediaBox [0 0 {PAGE_WIDTH} {PAGE_HEIGHT}] "
                f"/Resources << /Font << /F1 {font_regular_id} 0 R /F2 {font_bold_id} 0 R >> >> "
                f"/Contents {content_id} 0 R >>"
            )
            stream = "\n".join(["0.6 w", *commands]).encode("latin-1", "replace")
            content_object = (
                f"<< /Length {len(stream)} >>\nstream\n".encode("latin-1")
                + stream
                + b"\nendstream"
            )
            object_index = page_id - 1
            while len(objects) < object_index:
                objects.append(b"")
            objects.append(page_object.encode("latin-1"))
            objects.append(content_object)

        pdf = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
        offsets = [0]
        for object_number, body in enumerate(objects, start=1):
            offsets.append(len(pdf))
            pdf.extend(f"{object_number} 0 obj\n".encode("latin-1"))
            pdf.extend(body)
            pdf.extend(b"\nendobj\n")

        xref_start = len(pdf)
        pdf.extend(f"xref\n0 {len(objects) + 1}\n".encode("latin-1"))
        pdf.extend(b"0000000000 65535 f \n")
        for offset in offsets[1:]:
            pdf.extend(f"{offset:010d} 00000 n \n".encode("latin-1"))
        pdf.extend(
            (
                "trailer\n"
                f"<< /Size {len(objects) + 1} /Root {catalog_id} 0 R >>\n"
                "startxref\n"
                f"{xref_start}\n"
                "%%EOF\n"
            ).encode("latin-1")
        )

        output.write_bytes(pdf)
        return output


def _patient_name(patient_id: Any, payload: dict[str, Any]) -> str:
    normalized_id = _clean(patient_id)
    for key in ("sample_collection_pick_patients", "sample_collection_easy_tough_patients"):
        for patient in payload.get(key) or []:
            ids = {
                _clean(patient.get("id")),
                _clean(patient.get("patientId")),
                _clean(patient.get("patient_id")),
                _clean(patient.get("sourcePatientId")),
            }
            if normalized_id in ids:
                return _clean(patient.get("name"), f"Patient {normalized_id}")
    return f"Patient {normalized_id}"


def _tests_by_patient(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        _clean(item.get("patient_id")): item
        for item in payload.get("tests_payload") or []
        if _clean(item.get("patient_id"))
    }


def _updates_by_patient(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        _clean(item.get("patient_id")): item
        for item in payload.get("patient_updates") or []
        if _clean(item.get("patient_id"))
    }


def _patient_ids(payload: dict[str, Any]) -> list[str]:
    ids: list[str] = []
    for source in (payload.get("patient_updates") or [], payload.get("tests_payload") or []):
        for item in source:
            patient_id = _clean(item.get("patient_id"))
            if patient_id and patient_id not in ids:
                ids.append(patient_id)
    return ids


def _is_manual_hcb_slip(value: Any) -> bool:
    text = _clean(value).lower().replace("_", " ").replace("-", " ")
    return text in {
        "manual hcb slip",
        "manual hc slip",
        "manual slip",
        "hcb slip",
        "hc slip",
    }


def _manual_slip_documents(update: dict[str, Any]) -> list[dict[str, Any]]:
    documents = update.get("documents") or []
    return [
        document
        for document in documents
        if _is_manual_hcb_slip(document.get("type"))
    ]


def _split_path_values(value: Any) -> list[str]:
    if isinstance(value, list):
        return [_clean(item) for item in value if _clean(item)]
    text = _clean(value)
    if not text:
        return []
    return [item.strip() for item in text.split(",") if item.strip()]


def _manual_slip_references(update: dict[str, Any]) -> list[str]:
    references = [_document_reference(document) for document in _manual_slip_documents(update)]
    for key in (
        "manual_slip_paths",
        "manual_hc_slip_paths",
        "hc_slip_paths",
        "payment_screenshot_paths",
        "payment_screenshots",
    ):
        references.extend(_split_path_values(update.get(key)))
    unique: list[str] = []
    for reference in references:
        if reference and reference not in unique:
            unique.append(reference)
    return unique


def _resolve_upload_path(reference: str) -> Path | None:
    if not reference:
        return None
    path = Path(reference)
    if path.is_absolute() and path.exists():
        return path

    shared_upload_root: Path | None = None
    env_upload_base = _clean(os.environ.get("PATIENT_DOCUMENTS_UPLOAD_BASE"))
    if env_upload_base:
        shared_upload_root = Path(env_upload_base).expanduser().resolve().parent
    env_file = Path.cwd() / ".env"
    if shared_upload_root is None and env_file.exists():
        for line in env_file.read_text(encoding="utf-8", errors="ignore").splitlines():
            key, _, value = line.partition("=")
            if key.strip().upper() == "PATIENT_DOCUMENTS_UPLOAD_BASE" and value.strip():
                shared_upload_root = Path(value.strip().strip("'\"")).expanduser().resolve().parent
                break

    candidates = [
        Path.cwd() / reference,
        Path.cwd() / "app" / "static" / "uploads" / reference,
        Path.cwd() / "app" / "static" / "uploads" / "patient_documents" / reference,
    ]
    if shared_upload_root is not None:
        candidates.extend(
            [
                shared_upload_root / reference,
                shared_upload_root / "patient_documents" / reference,
            ]
        )
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def _document_reference(document: dict[str, Any]) -> str:
    for key in (
        "path",
        "file_path",
        "filepath",
        "saved_path",
        "rel_path",
        "relative_path",
        "url",
        "uri",
        "name",
        "filename",
        "file_field",
    ):
        value = _clean(document.get(key))
        if value:
            return value
    return "manual slip upload"


def _payload_totals(payload: dict[str, Any]) -> dict[str, Any]:
    patients = _patient_ids(payload)
    tests = 0
    charge = 0.0
    manual_slips = 0
    updates = _updates_by_patient(payload)
    for item in payload.get("tests_payload") or []:
        patient_id = _clean(item.get("patient_id"))
        update = updates.get(patient_id, {})
        if _is_manual_hcb_slip(update.get("apk_tbs") or item.get("test_booking_status")):
            continue
        for panel in item.get("panels") or []:
            selected_tests = panel.get("selected_tests") or []
            tests += len(selected_tests)
            for test in selected_tests:
                try:
                    charge += float(test.get("charge") or 0)
                except (TypeError, ValueError):
                    pass
    for update in updates.values():
        if _is_manual_hcb_slip(update.get("apk_tbs")):
            manual_slips += 1
    return {
        "patient_count": len(patients),
        "test_count": tests,
        "total_charge": charge,
        "manual_slip_count": manual_slips,
    }


def _build_reportlab_pdf(
    payload: dict[str, Any],
    output_path: str | Path,
    *,
    booking_id: str | int | None,
    phlebo_name: str | None,
    booking_date: str | None,
    time_slot: str | None,
    total_amount: Any = None,
    logo_path: str | Path | None = None,
) -> Path:
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)

    doc = SimpleDocTemplate(
        str(output),
        pagesize=A4,
        rightMargin=28,
        leftMargin=28,
        topMargin=24,
        bottomMargin=28,
    )
    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "BillTitle",
        parent=styles["Title"],
        fontName="Helvetica-Bold",
        fontSize=16,
        leading=20,
        textColor=colors.HexColor("#0f4c5c"),
        spaceAfter=2,
    )
    small_style = ParagraphStyle(
        "Small",
        parent=styles["Normal"],
        fontSize=8,
        leading=10,
        textColor=colors.HexColor("#4b5563"),
    )
    normal_style = ParagraphStyle(
        "NormalBill",
        parent=styles["Normal"],
        fontSize=9,
        leading=11,
        textColor=colors.HexColor("#111827"),
    )
    section_style = ParagraphStyle(
        "Section",
        parent=styles["Heading3"],
        fontName="Helvetica-Bold",
        fontSize=11,
        leading=14,
        textColor=colors.white,
    )
    test_style = ParagraphStyle(
        "Test",
        parent=styles["Normal"],
        fontSize=8.5,
        leading=10.5,
        textColor=colors.HexColor("#111827"),
    )

    totals = _payload_totals(payload)
    final_amount = total_amount
    if final_amount is None:
        final_amount = payload.get("total_amount") or totals["total_charge"]

    story = []
    header_left = []
    logo = None
    if logo_path and Path(logo_path).exists():
        logo = Image(str(logo_path), width=2.25 * inch, height=0.72 * inch, kind="proportional")
    if logo:
        header_left.append(logo)
    else:
        header_left.append(Paragraph("Dr Bhasin's Lab", title_style))
        header_left.append(Paragraph("Trusted Quality & Service", small_style))

    header_right = [
        Paragraph("<b>HOME COLLECTION BILL SUMMARY</b>", title_style),
        Paragraph("Patient-wise completion summary", small_style),
    ]
    header = Table([[header_left, header_right]], colWidths=[210, 315])
    header.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#e8f7f4")),
                ("BOX", (0, 0), (-1, -1), 0.7, colors.HexColor("#78c6b7")),
                ("LEFTPADDING", (0, 0), (-1, -1), 12),
                ("RIGHTPADDING", (0, 0), (-1, -1), 12),
                ("TOPPADDING", (0, 0), (-1, -1), 10),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ]
        )
    )
    story.append(header)
    story.append(Spacer(1, 10))

    meta_rows = [
        ["Booking", _clean(booking_id or payload.get("booking_code") or payload.get("booking_id") or payload.get("id"), "N/A"),
         "Date", _clean(booking_date or date.today().isoformat(), "N/A")],
        ["Phlebo", _clean(phlebo_name, "N/A"), "Time Slot", _clean(time_slot, "N/A")],
        ["Patients", str(totals["patient_count"]), "Tests", str(totals["test_count"])],
        ["Final Amount", f"Rs. {_money_plain(final_amount)}", "Manual Slips", str(totals["manual_slip_count"])],
    ]
    meta = Table(meta_rows, colWidths=[78, 187, 78, 182])
    meta.setStyle(
        TableStyle(
            [
                ("GRID", (0, 0), (-1, -1), 0.45, colors.HexColor("#d1d5db")),
                ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#f3f4f6")),
                ("BACKGROUND", (2, 0), (2, -1), colors.HexColor("#f3f4f6")),
                ("TEXTCOLOR", (0, 0), (0, -1), colors.HexColor("#374151")),
                ("TEXTCOLOR", (2, 0), (2, -1), colors.HexColor("#374151")),
                ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
                ("FONTNAME", (2, 0), (2, -1), "Helvetica-Bold"),
                ("FONTNAME", (1, 3), (1, 3), "Helvetica-Bold"),
                ("TEXTCOLOR", (1, 3), (1, 3), colors.HexColor("#047857")),
                ("FONTSIZE", (0, 0), (-1, -1), 9),
                ("LEFTPADDING", (0, 0), (-1, -1), 8),
                ("RIGHTPADDING", (0, 0), (-1, -1), 8),
                ("TOPPADDING", (0, 0), (-1, -1), 6),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ]
        )
    )
    story.append(meta)
    story.append(Spacer(1, 12))

    tests_by_patient = _tests_by_patient(payload)
    updates_by_patient = _updates_by_patient(payload)

    for index, patient_id in enumerate(_patient_ids(payload), start=1):
        tests_payload = tests_by_patient.get(patient_id, {})
        update = updates_by_patient.get(patient_id, {})
        patient_name = _patient_name(patient_id, payload)

        section = Table([[Paragraph(f"Patient {index}: {_clean(patient_name)}", section_style)]], colWidths=[525])
        section.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#0f4c5c")),
                    ("LEFTPADDING", (0, 0), (-1, -1), 9),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 9),
                    ("TOPPADDING", (0, 0), (-1, -1), 6),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                ]
            )
        )
        story.append(section)

        detail_rows = [
            ["Report Schedule", _clean(update.get("report_schedule") or tests_payload.get("report_schedule"), "-"),
             "Report Delivery", _clean(update.get("report_delivery") or tests_payload.get("report_delivery_options"), "-")],
            ["No. of Pricks", _clean(update.get("no_of_pricks"), "-"),
             "Sample Collection", _clean(update.get("sample_collection_is"), "-")],
        ]
        details = Table(detail_rows, colWidths=[95, 165, 95, 170])
        details.setStyle(
            TableStyle(
                [
                    ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#d1d5db")),
                    ("BACKGROUND", (0, 0), (0, -1), colors.HexColor("#eff6ff")),
                    ("BACKGROUND", (2, 0), (2, -1), colors.HexColor("#eff6ff")),
                    ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
                    ("FONTNAME", (2, 0), (2, -1), "Helvetica-Bold"),
                    ("FONTSIZE", (0, 0), (-1, -1), 8.5),
                    ("LEFTPADDING", (0, 0), (-1, -1), 7),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 7),
                    ("TOPPADDING", (0, 0), (-1, -1), 5),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
                ]
            )
        )
        story.append(details)

        is_manual_slip = _is_manual_hcb_slip(update.get("apk_tbs") or tests_payload.get("test_booking_status"))
        if is_manual_slip:
            references_list = _manual_slip_references(update)
            references = ", ".join(references_list) if references_list else "upload reference not present"
            slip_table = Table(
                [[Paragraph(
                    f"<b>Manual HC Slip For:</b> {_clean(patient_name)}",
                    normal_style,
                )]],
                colWidths=[525],
            )
            slip_table.setStyle(
                TableStyle(
                    [
                        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#ecfdf5")),
                        ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#34d399")),
                        ("LEFTPADDING", (0, 0), (-1, -1), 8),
                        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
                        ("TOPPADDING", (0, 0), (-1, -1), 6),
                        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                    ]
                )
            )
            story.append(slip_table)

            for reference in references_list:
                image_path = _resolve_upload_path(reference)
                if image_path is None:
                    continue
                try:
                    slip_image = Image(str(image_path), width=3.9 * inch, height=4.8 * inch, kind="proportional")
                except Exception:
                    continue
                image_table = Table(
                    [
                        [Paragraph(f"<b>Manual HC Slip - {_clean(patient_name)}</b>", normal_style)],
                        [slip_image],
                    ],
                    colWidths=[525],
                )
                image_table.setStyle(
                    TableStyle(
                        [
                            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#f0fdfa")),
                            ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#d1d5db")),
                            ("ALIGN", (0, 0), (-1, -1), "CENTER"),
                            ("LEFTPADDING", (0, 0), (-1, -1), 8),
                            ("RIGHTPADDING", (0, 0), (-1, -1), 8),
                            ("TOPPADDING", (0, 0), (-1, -1), 8),
                            ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
                        ]
                    )
                )
                story.append(image_table)
                break

        test_names: list[str] = []
        if not is_manual_slip:
            for panel in tests_payload.get("panels") or []:
                for test in panel.get("selected_tests") or []:
                    name = _clean(test.get("description"))
                    if name:
                        test_names.append(name)

        if test_names:
            rows = [["#", "Test Name"]]
            rows.extend([str(i), Paragraph(name, test_style)] for i, name in enumerate(test_names, start=1))
            test_table = Table(rows, colWidths=[34, 491], repeatRows=1)
            test_table.setStyle(
                TableStyle(
                    [
                        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#f0fdfa")),
                        ("TEXTCOLOR", (0, 0), (-1, 0), colors.HexColor("#0f766e")),
                        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                        ("GRID", (0, 0), (-1, -1), 0.35, colors.HexColor("#d1d5db")),
                        ("FONTSIZE", (0, 0), (-1, -1), 8.5),
                        ("VALIGN", (0, 0), (-1, -1), "TOP"),
                        ("LEFTPADDING", (0, 0), (-1, -1), 6),
                        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                        ("TOPPADDING", (0, 0), (-1, -1), 4),
                        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                    ]
                )
            )
            story.append(test_table)
        elif not is_manual_slip:
            story.append(Paragraph("No tests available for this patient.", normal_style))

        story.append(Spacer(1, 12))

    doc.build(story)
    return output


def build_completion_patientwise_pdf(
    payload: dict[str, Any],
    output_path: str | Path,
    *,
    booking_id: str | int | None = None,
    phlebo_name: str | None = None,
    booking_date: str | None = None,
    time_slot: str | None = None,
    total_amount: Any = None,
    logo_path: str | Path | None = None,
) -> Path:
    """Create a patient-wise PDF summary for a complete-booking payload."""

    if REPORTLAB_AVAILABLE:
        return _build_reportlab_pdf(
            payload,
            output_path,
            booking_id=booking_id,
            phlebo_name=phlebo_name,
            booking_date=booking_date,
            time_slot=time_slot,
            total_amount=total_amount,
            logo_path=logo_path,
        )

    totals = _payload_totals(payload)
    pdf = SimplePdf()
    pdf.header_band(
        "Home Collection Completion Summary",
        "Patient-wise collection, reporting and test summary",
    )
    pdf.key_value("Booking ID", booking_id or payload.get("booking_id") or payload.get("id"))
    pdf.key_value("Phlebo Name", phlebo_name)
    pdf.key_value("Date", booking_date or date.today().isoformat())
    pdf.key_value("Time Slot", time_slot or payload.get("time_slot") or payload.get("complete_time"))
    pdf.gap(6)
    card_y = pdf.y - 48
    card_width = 154
    pdf.summary_card("Patients", totals["patient_count"], MARGIN_X, card_y, card_width, LIGHT_BLUE, BLUE)
    pdf.summary_card("Tests", totals["test_count"], MARGIN_X + 174, card_y, card_width, LIGHT_TEAL, TEAL)
    pdf.summary_card("Manual Slips", totals["manual_slip_count"], MARGIN_X + 348, card_y, card_width, LIGHT_GREEN, GREEN)
    pdf.y = card_y - 28

    tests_by_patient = _tests_by_patient(payload)
    updates_by_patient = _updates_by_patient(payload)

    for index, patient_id in enumerate(_patient_ids(payload), start=1):
        tests_payload = tests_by_patient.get(patient_id, {})
        update = updates_by_patient.get(patient_id, {})
        patient_name = _patient_name(patient_id, payload)

        pdf.section_title(f"Patient {index}: {patient_name}", color=BLUE)
        pdf.key_value("Report Schedule", update.get("report_schedule") or tests_payload.get("report_schedule"))
        pdf.key_value("Report Delivery", update.get("report_delivery") or tests_payload.get("report_delivery_options"))
        pdf.key_value("No. of Pricks", update.get("no_of_pricks"))
        pdf.key_value("Sample Collection Is", update.get("sample_collection_is"))

        is_manual_slip = _is_manual_hcb_slip(update.get("apk_tbs") or tests_payload.get("test_booking_status"))
        if is_manual_slip:
            references_list = _manual_slip_references(update)
            if references_list:
                references = ", ".join(references_list)
                pdf.wrapped_text(f"Manual HCB Slip Attached: {references}", bold=True, width=86, color=GREEN)
            else:
                pdf.wrapped_text("Manual HCB Slip Attached: upload reference not present in payload", bold=True, width=86, color=GREEN)

        panels = [] if is_manual_slip else tests_payload.get("panels") or []
        for panel in panels:
            pdf.gap(4)
            pdf.wrapped_text(
                f"Panel: {_clean(panel.get('panel_company'), '-')}",
                bold=True,
                color=TEAL,
                width=86,
            )
            selected_tests = panel.get("selected_tests") or []
            if not selected_tests:
                pdf.text("No tests selected.")
                continue

            for test_index, test in enumerate(selected_tests, start=1):
                pdf.wrapped_text(
                    f"{test_index}. {_clean(test.get('description'), '-')}",
                    x=MARGIN_X + 12,
                    width=82,
                )

        pdf.gap(10)

    if not _patient_ids(payload):
        pdf.text("No patient rows found in payload.")

    return pdf.save(output_path)


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate complete-booking patient-wise PDF.")
    parser.add_argument("payload_json", help="Path to complete-booking payload JSON file.")
    parser.add_argument("output_pdf", help="Path where PDF should be written.")
    parser.add_argument("--booking-id", default="", help="Booking ID to print in header.")
    parser.add_argument("--phlebo-name", default="", help="Phlebo name to print in header.")
    parser.add_argument("--date", default="", help="Booking/date label to print in header.")
    parser.add_argument("--time-slot", default="", help="Time slot to print in header.")
    parser.add_argument("--total-amount", default="", help="Final amount to print in bill summary.")
    parser.add_argument("--logo-path", default="", help="Optional company logo image path.")
    args = parser.parse_args()

    payload = json.loads(Path(args.payload_json).read_text(encoding="utf-8"))
    output = build_completion_patientwise_pdf(
        payload,
        args.output_pdf,
        booking_id=args.booking_id,
        phlebo_name=args.phlebo_name,
        booking_date=args.date,
        time_slot=args.time_slot,
        total_amount=args.total_amount,
        logo_path=args.logo_path,
    )
    print(output)


if __name__ == "__main__":
    main()
